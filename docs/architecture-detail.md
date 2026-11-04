# Detailed architecture

Status: **Confirmed** (2026-11-03). Builds on D-001, D-010–D-013.
Decisions: agent loop (D-014, §5), answer contract (D-015, §5), ingestion via gateway (D-016, §2), tenant enforcement (D-017, §5), remaining sections (D-018), requirements-fit fixes (D-019), production-grade P0 axis (D-020, §7), P1 rest + P2 + production documents (D-021, §7).
Reviewed with cross-model consults on 2026-11-01 (2 rounds), 2026-11-02 (requirements fit, 2 rounds) and 2026-11-03 (production P0, 2 rounds); their fixes are applied below.

Priority order (D-020, strict): **Basic 13 items** → **P0** (implemented and proved by tests) → **P1** (if time) → **P2** (design doc only). Nothing in P0/P1 may displace a Basic item.

## 1. Repository layout

```
contracts/                  # the only package both services import: event + port schemas (Pydantic) → JSON Schema export
proto/                      # gRPC merchant API
services/gateway/src/gateway/
  accounts/                 # sign-up, login, TOTP, JWT issue (identity provider), RBAC policy table
  wallets/                  # balances, funds holds
  payments/                 # charge + P2P transfer use cases, history
    ledger/  fx/  tokenization/  idempotency/  routing/  providers/ (simulator adapter)  reconciler/
  refunds_disputes/
  risk/                     # sync scoring, real-time card-testing block, decline rules, dependency policy
  settlement/
  merchant_api/  rest/  grpc/
  ops_port/  query_api/  command_api/   # the D-013 port, gateway side
  outbox/                   # table + relay
  notifications/            # transactional (confirmations, "was this you?")
  audit/
  observability/            # logging + redaction, OTel, metrics
services/ops-ai/src/ops_ai/
  ingest/                   # stream consumers → projections
  taxonomy/                 # failure-reason classifier + fix playbook
  search/                   # FTS + pgvector + RRF
  investigation/            # deterministic tools (read-only)
  assistant/                # tool loop, LlmPort, mock, llm_input (DTO + redaction), guardrails, answer checker
  memory/
  fraud_watch/              # delayed card-testing detector
  alerts/  tickets/
  evaluation/               # golden set + DeepEval metrics
  observability/
web/src/features/{user,merchant,ops}/
scripts/                    # generate_dataset.py, import, replay, verify_audit, loadtest/
ops/                        # prometheus.yml, alert rules, otel-collector config, grafana provisioning + dashboards
services/*/migrations/      # Alembic, one history per service (D-021: from Day 1)
.github/workflows/ci.yml
```

Each feature keeps its tests in its own `__tests__/`. An import-rule test fails if `gateway` imports `ops_ai` or the reverse.

## 2. Data and ingestion

- The generator writes a **canonical scenario log**: amounts in minor units with the currency exponent, plus attempts, settlements, disputes and incidents with resolution notes.
  - Incidents also carry resolution-effectiveness fields (`resolved_at`, `recurrence_within_7d`, `fix_applied`), so incidents can later be reranked by recency and effectiveness.
- Two outputs are derived from that log:
  - the brief's 16-field CSV/JSON export (the only place amounts are `float`), and
  - the gateway import.
- Values the 16 fields cannot carry (journals, fees, routes, BINs) are labeled as synthetic-derived. The README documents the extensions.
- The **backfill importer** writes historical payments with balanced journals into the gateway, then emits outbox events with the original `occurred_at` and `origin=backfill`.
  - Projection consumers apply backfill events.
  - Side-effect consumers (notifications, alerts, tickets) skip them.
  - Historical incidents and tickets are imported as state.
- The dataset's `status=flagged` is an export view. Internally, the fraud flag is separate from the payment status.
- Why the data goes through the gateway: the gateway lookups for SQ2 and SQ4 must agree with the ops-ai projections. The diagram also shows the real path: dataset → ingestion → port → AI.

## 3. Gateway

### Ledger and locking (P0)
- Double-entry. Each owner gets one account per currency.
- Platform accounts: clearing (one per currency), FX, fees, reserve, provider receivable.
- Amounts are `Decimal` / minor units only. Float is never used inside the system.
- **Every journal balances per currency**, checked synchronously before commit. Proved by a Hypothesis property test over random sequences of charges, transfers, FX, refunds, chargebacks and holds.
- **Locking: pessimistic row locks.**
  - User accounts have a balance row. Every operation that reads a balance and then debits it (balance check → hold or journal) runs in one transaction under `SELECT … FOR UPDATE` on the user account rows, locked in ascending id order to avoid deadlocks.
  - Platform accounts have **no synchronous balance row**. They take append-only postings only, and their balances are aggregated afterwards. This keeps all payments from queueing on one hot row.
  - Why pessimistic rather than optimistic: wallet debits are short, contention is per wallet, and a retry loop on version conflicts would add latency and code paths without a benefit at this scale.
  - Test: N parallel debits on one wallet never overdraw it, and the final balance equals the sum of postings.

### Funds hold
- P2P transfers and wallet-funded merchant payments are internal. They settle in one transaction (balance check + journal under the user-account lock), so no hold is needed.
- A wallet debit that leaves through an external rail (`bank_transfer` / `upi` payout, cross-border via an external rail) takes a **hold in tx1**: `available −= amount`, `held += amount`.
  - Success → capture. Definitive failure → release. `unknown` → stays held.
- Invariants:
  - capture does not deduct from `available` a second time;
  - release only removes the hold;
  - hold, journal and status move in the same transaction;
  - duplicate capture or release is a no-op;
  - if the final amount differs from the held amount (FX, fees), the difference is posted as a separate line, and capture never takes more than was held.
- Card charges have no internal funds and take no hold.

### State machines (per payment method)

| Method | Flow |
|---|---|
| card | sale (authorize + capture) is synchronous → `succeeded \| declined \| failed \| unknown` |
| wallet / P2P | internal and synchronous |
| bank_transfer, upi | `pending` → the simulator settles or fails it asynchronously |
| refund | `requested` → `succeeded \| failed`, with a reversal journal |
| dispute | `opened → under_review → won \| lost`. `lost` posts the chargeback and a chargeback fee |

- Refunds and dispute losses are checked under the same `SELECT … FOR UPDATE` on the payment: refunded principal + chargeback principal must stay ≤ the captured amount. Fees are booked separately.
- **Who decides a dispute:** the simulator plays the card network and returns `won | lost` after evidence is submitted. An `ops_lead` can record the outcome manually. The merchant can only submit evidence.

### History
- `GET /v1/payments` (merchant, REST + gRPC `ListPayments`) and `GET /v1/me/transactions` (consumer): cursor-paginated, filtered by status, date range and method.
- The consumer sees only their own records; the merchant sees only its tenant.

### Sync charge path (T0, no LLM)
1. Authenticate the API key (stored hashed).
2. Validate (Pydantic / proto, Luhn, test-PAN allowlist). REST and gRPC call the same validation module.
3. Tokenize: the vault keeps the token, BIN, last4 and fingerprint. The PAN is never stored.
4. **Idempotency** (P0). REST takes the `Idempotency-Key` header; gRPC takes `idempotency-key` metadata.
   - Redis `SET NX PX` on `(merchant_id, key)` as an in-flight lock. It only suppresses contention. Correctness never depends on it: with Redis flushed, down, or the lock expired, the Postgres path below still yields one charge.
   - **tx1**: insert the idempotency row `(merchant_id, key, request_hash, lease_until)` under a unique constraint, and create the payment and the attempt in the same transaction. The provider key is the attempt id.
   - On conflict with a different hash: 422.
   - On conflict with the same hash: wait up to 5 s for the stored response, then return 409 `idempotency_in_progress` with `Retry-After`.
   - Async methods return a fixed 202 as their idempotent response. The current state is read from `GET`.
5. Risk score (rules → 0.0–1.0 + reason codes). Above the block threshold: decline and flag. See the risk dependency policy below.
6. Route selection, recording the config version.
7. Call the provider **outside** the transaction, with a timeout.
8. **tx2**: `UPDATE … WHERE state IN ('created','unknown')`. Only the winner writes the journal, the outbox rows, the audit row and the stored response.

**Reconciler**
- Picks up attempts still in `created` after their lease and looks them up at the provider by key.
- `not_found` before the provider's acceptance deadline means `unknown`, not `failed`. The reconciler re-queries with backoff and resends only with the same key.
- After the deadline, `not_found` means the request was never received, so `failed` is safe.
- **An `unknown` attempt is never retried with a new key.** The provider's idempotency retention window is declared (simulator: 24 h). If an attempt is still unresolved near the end of that window, automatic resends stop, the attempt stays `unknown` and its funds stay held, and it goes to manual reconciliation with a ticket.

**Guarantees we claim**
- The provider performs the charge at most once. This is proved at the provider: the simulator keeps its own charge ledger keyed by the provider key, persisted across gateway restarts.
- Once a request completes, retries get the stored response.
- The "20 concurrent → 20 identical bodies" test only holds when the provider finishes within the wait budget.

### Risk scoring and its dependency policy (P0: LLM-independent)
- Risk is rules only. No LLM is on the payment path, so an LLM outage cannot stop or weaken payments.
- Velocity per card fingerprint comes from Postgres: a time-bucketed count, or a capped fetch that stops at the threshold, over an index on `(fingerprint, ts)`.
- If only the velocity lookup fails, while persistence and the known-block check still work:
  - existing blocks stay in force;
  - a new fingerprint or a score above the review threshold → decline with `risk_unavailable`;
  - otherwise allow, marked `degraded_scoring` for later review. Having seen a fingerprint before does not by itself make a payment low-risk.
- If the database is down, no payment proceeds.
- An attempt whose provider result is unknown is never rerouted; it is resolved by lookup.

### P2P / international transfer (SQ2)
- Checks in order: KYC status → corridor (sender/receiver country) → per-transaction and daily cross-border limits by KYC tier → sanctions stub → balance.
- Every decline stores `{rule_id, rule_version, observed values, threshold}`.
- Cross-currency transfers go through FX clearing accounts with a fixed rate snapshot. The rate id is on the journal.

### Settlement (SQ4)
- A daily batch per merchant computes: gross − MDR − fixed fees − refunds − chargebacks − chargeback fees − rolling reserve ± FX = net.
- The result is stored as typed line items, with a check that the lines sum to the payout.
- "Expected" = gross − contract MDR, the number a merchant would naively compute.

### Provider simulator
- Two acquirers.
- Deterministic fault profiles per route, plus test-only fault hooks for crash injection (§7).
- Lookup by key, with a declared acceptance deadline and idempotency retention window.
- Its own persisted charge ledger.
- A scenario-control endpoint that only exists when `DEMO_MODE` is set.

### Routing
- Configs are versioned rows. Every change emits a `config.changed` event.

### Identity and RBAC (P0)
- `accounts` issues short-lived asymmetric JWTs carrying `user_id`, `roles` and `merchant_id`.
- Passwords use argon2. MFA is TOTP, and the TOTP secret is encrypted with a key from the environment.
- Roles, mapped to the four user-facing roles:

| User-facing role | Role(s) | Main rights |
|---|---|---|
| Consumer (利用者) | `consumer` | own wallets, payments, transfers, refund request, dispute |
| Merchant (加盟店) | `merchant_admin`, `merchant_support` | admin: API keys, refunds, settlements; support: read + assistant |
| Support agent (サポート担当) | `ops_agent` | read across tenants they are assigned to, assistant, tickets |
| Administrator (管理者) | `ops_lead` | everything an agent can do, plus approvals, dispute outcomes, routing override |

- One policy table defines `role × action`. Authorization is checked at three levels: role, tenant (`merchant_id`) and ownership (`user_id`).
- Tests: a matrix generated from the policy table, plus explicit tests for consumer ownership and merchant tenancy.

### Audit (P0)
- `audit_log` is append-only: `{actor_id, actor_role, action, target, before/after summary, correlation_id, occurred_at}`. It records logins and MFA changes, refunds, disputes, proposals, approvals, command executions and admin actions.
- The audit row is written **in the same transaction** as the operation it records. If the audit write fails, the operation fails.
- A DB trigger rejects `UPDATE` and `DELETE`, and the application role has no `UPDATE`/`DELETE` grant. What this guarantees: the application role cannot change or delete audit rows. We do not claim tamper detection.
- Cut-first option: a per-row hash chain (`prev_hash`, `row_hash`) with `scripts/verify_audit`. Only if it ships do we claim "in-place edits by a non-DBA are detected". An external checkpoint (WORM storage) is design-only.

### Notifications
- Gateway, transactional: payment confirmations, and a "was this you?" notice to the cardholder or merchant when its own risk rules flag a payment.
- ops-ai alerts (fraud_watch, failure-rate rules) go to ops staff and are not sent to consumers.
- Both are deduped by source event.

## 4. The port in detail

### Streams
- Topics: `gw.payments.v1`, `gw.risk.v1`, `gw.disputes.v1`, `gw.settlement.v1`, `gw.config.v1`, `gw.provider_health.v1`.
- The outbox is written in the same transaction as the state change. It is part of Basic (confirmations and the whole ops port depend on it), even though the production brief lists it as P1.
- The relay uses `FOR UPDATE SKIP LOCKED`, marks rows published, and keeps outbox rows. The demo keeps all of them. Production would add snapshots and a retention window.
- **Delivery is at least once.** A relay crash after `XADD` and before marking the row published re-sends it; this is expected.
- `scripts/replay` replays from an offset.

### Consumers
- One Redis consumer group per concern.
- Dedupe uses `processed_events(event_id)` with a unique constraint, written in the same transaction as the projection change. A duplicate delivery therefore changes the projection once.
- State projections apply the event with the highest `entity_version` (last writer wins).
- P1: retries with exponential backoff and jitter, and a dead-letter stream after N failures.

### Query API (`/internal/v1/query/...`)
- Two lookups: one payment's status with its decline detail, and one settlement's breakdown.
- Both require a service credential plus the forwarded user JWT. The gateway authorizes on both.

### Command API (routing override)
1. ops-ai creates a proposal: route, from/to provider, TTL, expected `config_version`.
2. An `ops_lead` approves it in the UI. The approval call goes to the gateway with the user's JWT.
3. The approval is bound to `proposal_id` + the SHA-256 of the canonical proposal, has a 10-minute expiry, and is single-use.
4. The gateway re-validates the current version and the permission, then applies the override.
5. When the TTL expires, the override is reverted only if the config is still at the version it set.

The LLM can propose. It never approves. This is the only command on the port. Nothing on the port moves money.

## 5. ops-ai

### Projections
- `payment_facts`: one row per payment, holding route, provider, BIN prefix, raw and normalized codes, taxonomy code, and risk.
- `search_documents`: payments, incidents and resolution notes, with a `tsvector` column. There is one embeddings table per embedder.
- `change_timeline`, `provider_health`, `card_testing_windows`.
- Aggregates are SQL `GROUP BY` over `payment_facts` at query time. There are no incremental counters, so reordered or replayed events cannot double-count.
  - In production this would be materialized views or continuous aggregates.

### Failure taxonomy (B4)
- Input: raw and normalized provider codes, plus the free-text `failure_reason` from backfill.
- Rules first. Text that no rule matches goes to an LLM fallback, which may only propose `{taxonomy_code, category, owner, confidence}` from a closed list. Low confidence → `unclassified`. The mock runs rules only.
- `retryable` is **not** chosen by the LLM. A deterministic policy derives it from the final payment state and the taxonomy code. Timeouts and `unknown` are never retryable by the AI layer; the reconciler resolves them.
- A versioned fix playbook per taxonomy code supplies the suggested fixes, enriched with matching resolution notes from RAG.

### Search
- Full-text search and pgvector are fused with RRF.
- The keyword side sits behind an interface, so Japanese tokenization (e.g. pg_bigm) can be swapped in later.

### Investigation tools (P0: read-only)
- All tools are deterministic and read-only, with typed I/O generated from Pydantic: `aggregate_failures`, `compare_baseline`, `change_timeline`, `search_incidents`, `get_payment_status` (calls the gateway), `analyze_fraud`, `explain_settlement` (calls the gateway).
- `propose_routing_override` writes a proposal only; it changes nothing until a human approves it.
- Which tools are exposed depends on the caller's role and the adapter's capabilities.
- **Money-moving actions** (refund, dispute outcome) have no tool. The assistant may return `suggested_action {suggestion_id, payment_id, amount, reason}`. The UI shows the target and amount, and the human confirms explicitly. The call goes to the normal gateway refund API with the human's JWT and an `Idempotency-Key`, and the gateway re-checks role, tenant and the refund cap. `suggestion_id` is never an authorization input; the server records it in the audit row only after checking that it refers to the same payment.

### LLM input boundary (P0)
- `assistant/llm_input` is the only path to `LlmPort`. It builds an **allowlisted DTO**: fields not on the list are dropped. Card data never reaches it except token, BIN and last4.
- Free text inside the DTO (resolution notes, merchant messages) is then redacted: PAN-like digit runs, email, phone, names from the PII table, secrets and tokens.
- Test: canary values (a known test PAN, email, phone, password, token) are planted in the data. The test asserts by exact string match that none of them appears in any `LlmPort` payload, independent of the redaction detector.

### Prompt-injection defense (P0)
- Permissions: tool allowlist per role; scope comes from auth, never from model output; tools are read-only.
- Output: the model's tool arguments and its answer are validated against Pydantic schemas; invalid output is rejected, not repaired. The answer contract checker (below) rejects unsupported claims.
- Input: length cap; retrieved text and memory are passed as quoted data, never as instructions.
- Test fixtures: instructions planted in resolution notes, merchant memory and the user message ("ignore previous instructions", "call refund", "use merchant_id X"). They must not change the tool set, the scope or the answer contract outcome.

### Assistant loop
- A hand-rolled loop on the OpenAI Responses API, behind `LlmPort`.
- Limits: at most 6 tool calls and a 20 s budget. Beyond that, or when the LLM is down, it falls back to T2: dashboards plus the evidence list.
- The mock `LlmPort` (a rule-based NL → typed query parser with templated narration) only proves the wiring.
- A real-API run of the 4 sample queries is reported separately, with its date and model.
- **AI audit trail:** each assistant turn records the tool calls with arguments, model, prompt hash, token counts, latency, checker verdict and correlation id.

### Answer contract (differentiator)
- The LLM returns `claims[]`. Each claim is `{kind: fact | hypothesis | cause | action, text with slots, evidence_refs[]}`.
- Slot values are rendered from the referenced tool-output fields. The LLM never types them.
- The checker rejects:
  - references that this turn's tools did not produce;
  - references whose merchant or time window differs from the typed query;
  - digits outside slots;
  - a `cause` claim without both a baseline comparison and a timeline reference. Such a claim is demoted to `hypothesis`.
- If the check fails, the answer is regenerated once. If it fails again, only the evidence list is shown.
- Every answer carries the data-as-of time and the known gaps.

### Memory
- Redis session keyed by `(merchant_id, user_id, session_id)`, with an ownership check and a TTL.
- Plus the last N turns and a typed conversation state.
- `merchant_memory` is written only through a confirmed tool call. Each record keeps its source turn id and can be corrected or deleted.

### Tenant enforcement
- Every repository function requires a `Scope` built from auth.
- Cross-tenant probe tests cover search, tools, session and memory.
- Postgres RLS is a stretch goal. If we do it, it comes with a non-owner DB role and `SET LOCAL` per transaction.

### fraud_watch
- Window features: distinct cards, small-amount share, decline ratio, BIN spread. Rules only; no LLM.
- Produces an alert plus a ticket in the `fraud` queue, deduped by window key.
- Detector thresholds can be tuned from human verdicts in the feedback set. Changes to gateway risk rules go to humans as tickets; there is no AI-to-gateway learning channel.

### Tickets
- A rules table maps each event to a queue (`fraud`, `payments_ops`, `settlement`, `compliance`) and a priority.
- Lifecycle: `open → assigned → resolved`. Tickets are deduped by source event.

## 6. Web and ops

- One Vite + React + TypeScript static SPA (no SSR) with three areas: user, merchant and ops console. A `web` nginx
  service serves the bundle and routes `/api/gw` and `/api/ops` on the same origin (no CORS), with CSP and `no-store`.
  Tokens and the merchant API key live in memory only. Production edge: GCP (Cloud CDN + Cloud Armor) as the
  reference, Cloudflare as a written alternative (design only). Details: `docs/specs/web.md`.
- The clock is injectable, with a fixed `DEMO_NOW`. Notifications are an in-app table plus a log adapter.

### Restart behavior

| Failure | Expected recovery |
|---|---|
| Relay crash | Unpublished outbox rows resume |
| ops-ai crash | The consumer group resumes from pending entries |
| Projection wipe | Rebuilt by replaying the retained outbox |
| Gateway crash between the provider call and tx2 | The reconciler resolves the attempt by its key |

## 7. Production readiness (D-020)

### P0 evidence map

| P0 item | Mechanism (section) | Proof |
|---|---|---|
| Idempotency: DB unique + Redis | §3 sync charge path | N=20 concurrent same key → 1 provider charge, 1 journal; repeated with Redis flushed, down, lock expired |
| Balanced ledger | §3 ledger | Hypothesis property test |
| Lock strategy | §3 ledger and locking | parallel-debit test; written rationale |
| Crash: no double charge, no loss | §3 reconciler, §4 streams | crash matrix below |
| LLM down → payments continue | §3 risk (rules only), §5 T2 fallback | test with `LlmPort` raising: charges succeed, fraud flags still raised, assistant shows evidence list |
| PII / card masking before LLM | §5 LLM input boundary | canary test |
| Read-only tools, human approval for money | §5 investigation tools | tool registry test (no write tool); refund only via gateway API with human JWT |
| Prompt-injection defense | §5 | injection fixtures |
| RBAC | §3 identity and RBAC | policy matrix + ownership/tenant tests |
| Append-only audit | §3 audit | UPDATE/DELETE rejected for the app role; audit row in the same tx (failure rolls back the operation) |
| No secrets / PII in logs | §7 observability | canary test over captured logs of a full E2E run |
| Correlation id + OTel traces | §7 observability | test: one trace id across API span, outbox event header, consumer span and LLM span |
| Metrics + alert rules | §7 observability | `/metrics` scrape test; `promtool test rules` on alert fixtures |
| CI with eval gate | §7 CI | workflow runs lint, types, tests, eval; gate fails on threshold |

### Crash matrix (P0)
Each failure point gets its own test and its own assertion. At least one runs as a real process kill and restart (`docker compose kill` / subprocess), not only an injected exception.

| # | Failure point | Assertion |
|---|---|---|
| C1 | Provider succeeded, gateway dies before tx2 | after restart the reconciler finalizes; simulator charge count = 1; one journal |
| C2 | tx2 committed, gateway dies before the HTTP response | client retry with the same key gets the stored response; no new provider call |
| C3 | Relay dies after `XADD`, before marking published | event is delivered again; projection changes once (dedupe) |
| C4 | Consumer dies after projection commit, before `XACK` | redelivery is a no-op; no event lost |
| C5 | Provider result stays unknown past the retention window | no new-key resend; funds stay held; manual-reconciliation ticket |

### Observability (P0)
- **Logs:** structured JSON. Every line carries `trace_id` (used as the correlation id), `span_id`, service and merchant scope. A redaction filter masks tokens, passwords, PAN-like strings, TOTP secrets, API keys and PII fields.
- **Traces:** OpenTelemetry SDK with FastAPI, SQLAlchemy, Redis and httpx instrumentation, plus manual spans for the tool loop, each tool call and each LLM call. The trace context travels in outbox event headers, so a trace spans API → worker → LLM. Export: OTLP → OpenTelemetry Collector in compose. A Jaeger UI is cut-first.
- **Metrics** (Prometheus `/metrics` on every process, scraped by Prometheus in compose):
  - technical: request count, error rate, latency histograms (p50/p95/p99 via `histogram_quantile`), outbox lag, consumer lag, reconciler `unknown` count;
  - business: payment success rate by method, failures by taxonomy code, fraud flags, declines by rule id;
  - LLM: tokens in/out, latency, cost (from a price table in env), fallback count, checker rejection rate, latest eval score. Numbers produced with the mock LLM are labeled `mock` and never presented as measured.
- **Alert rules** (Prometheus rule file, verified with `promtool test rules`; no notification delivery in the demo):
  - card failure rate over 10 min > 3 × its 7-day baseline for the same hour, with a minimum volume;
  - provider error rate or timeout rate above a threshold per route;
  - outbox lag or consumer lag above N seconds;
  - reconciler `unknown` count > 0 for longer than the acceptance deadline;
  - LLM fallback rate or checker rejection rate above a threshold.
- **Health:** `/healthz` (liveness) and `/readyz` (DB, Redis and stream reachability) on every process. Graceful shutdown is P1-6.
- **Dashboard (D-021):** Grafana in compose with a provisioned dashboard (payment success rate, failures by taxonomy code, card failure rate vs. baseline, latency p50/p95/p99, LLM tokens/cost/fallback) next to the Prometheus alerts page. No hand-written UI for metrics.

### CI (P0)
- `.github/workflows/ci.yml` and an identical local `make ci`: ruff, mypy strict, pytest (with Postgres and Redis service containers), tsc strict, eslint, then the eval.
- **Eval gate:** deterministic metrics are implemented as DeepEval custom `BaseMetric`s and always gate: taxonomy accuracy, evidence support rate, tool name + arguments per sample query, card-testing precision/recall.
  - Cross-tenant leakage is a hard gate: a single leak fails CI. It is reported as "0 observed in N probes", not as a guarantee.
  - LLM-judge metrics run only when an OpenAI key secret is present; their skip is reported, never counted as a pass.
- The GitHub remote does not exist yet, so CI is proved locally with `make ci` until it does.

### P1 (if time)
- Retry with exponential backoff and jitter, and a dead-letter stream for consumers.
- Provider timeout (already on the sync path) plus a circuit breaker per route.
- Rate limits per merchant API key and per user (Redis token bucket; fail-open with a metric if Redis is down).
- Load test (k6 or Locust) for `POST /v1/payments` (card, simulator latency profile normal) at a stated load, reporting p50/p95/p99 and error rate together. A 202 measures acceptance only.
- Health, readiness and graceful shutdown on SIGTERM (drain requests; consumer finishes and XACKs the current message; relay finishes its batch).
- Alembic migrations — pulled forward to Day 1, because the schema is created first anyway and moving from raw DDL later would be double work.
- Model and prompt pinning: dated model ids in env, versioned prompt files with their hash in the AI audit trail; fallback model → T2; daily token budget per merchant → T2 with a metric.
- Order: health/shutdown → model pinning + fallback → load test → circuit breaker → rate limits → retry/DLQ.

### P2 (design doc only)
Target platform is GCP. Written in `docs/design.md` (P2-1–P2-8 in requirements-traceability): multi-region active-active (Cloud Spanner vs. per-region Cloud SQL + reconciliation), 99.999 % (SPOF list, failover, error budget ≈ 26 s/month), PCI scope reduction (separate vault project / VPC, Cloud KMS / HSM), data residency + GDPR crypto-shredding, canary + feature flags + rollback, backup/restore with RTO/RPO, the 100k TPS topology, mTLS, WORM audit checkpoint.

Local → GCP mapping: compose services → Cloud Run / GKE; Postgres → Cloud SQL or Spanner; Redis → Memorystore; Redis Streams → Pub/Sub; secrets → Secret Manager + KMS; Prometheus / OTel → Managed Prometheus / Cloud Trace (unchanged instrumentation).

### Production documents (D-021)
- `docs/production-readiness.md`: per NFR, status (実装しテスト確認済み / 手元で検証済み / 設計のみ) + evidence. It becomes the single source for §8 below.
- `docs/runbooks/card-failure-spike.md` and `docs/runbooks/llm-provider-outage.md`.
- `docs/design.md`: each decision as Decision / Why / What we gave up / Alternatives.

## 8. Claims we make about the NFRs

Working copy; moves to `docs/production-readiness.md` in Phase 5 (DOC-1).

| NFR | What we say |
|---|---|
| P99 < 2 s | Applies to `POST /v1/payments` (card) at the stated local load, with error rate alongside. The AI assistant has its own SLO: p95 < 20 s, with fallback. |
| 100k TPS / 100M users | Production design only (§7 P2). Local load-test numbers are reported as measured, nothing more. |
| 99.999 % / cross-region failover | Multi-region design: one home region per account, single writer there; cross-region transfers via a saga with eventual consistency. RPO/RTO are design targets. No claim of lossless global failover. |
| "E2E encryption" | TLS in transit (nginx in the demo; service-to-service mTLS is design-only) plus field encryption for the TOTP secret and PII. It is not end-to-end encryption in the messaging sense. |
| Card tokenization / PCI-DSS | PAN never stored; test PANs only; vault keeps token, BIN, last4, fingerprint. PCI-DSS L1 is not claimed. |
| GDPR | A retention and erasure table per store (gateway PII table, search documents, conversations and memory, audit, outbox, backups). Events carry pseudonymous ids only; PII sits in one erasable table; free-text fields are crypto-shredded per user key; the ledger keeps pseudonymous postings for legal retention. Implemented: PII separation. The rest is design-only. |
| AML / KYC | KYC status and tier drive transfer limits; sanctions is a stub. Re-verification triggers are design-only. |
| Zero double charge | Proved at the provider by the idempotency and crash tests (§7). |
| Monitoring, audit, alerting | §7 observability and §3 audit. |

## 9. Requirement 2 fit
- Fits the current structure: multi-agent with role-scoped tool sets; LLM-as-judge for ranking hypotheses; DeepEval (already a P0 gate); incident reranking by recency and effectiveness (dataset fields exist); token optimization (tools aggregate before the LLM); a real sandbox provider as another adapter.
- Conflict: "fraud pattern updates" — answered by ops-ai detector thresholds learning from human verdicts, with gateway rule changes going to humans as tickets (§5 fraud_watch).
- Not covered: failure prediction, predictive dashboard, compliance service.
- If time remains after P1: (a) extend the eval set and metrics; (b) a Stripe test-mode adapter, off by default — counted as done only if a lost response before the provider id is saved is recovered by a same-key retry or a webhook.

## 10. Top risks (fix first)

1. Recovery when the provider result is unknown, and double charging (§3 reconciler, §7 crash matrix).
2. A refund and a chargeback together returning more than the captured principal (§3 state machines).
3. Tenant leakage, PII reaching the LLM, and AI claims that go beyond the evidence (§5).
4. Schedule: three days and no code yet. Day 1 must end with one thin vertical slice (sign-up → wallet → charge → event → projection → mock answer) green in `make ci`; every later slice lands with its tests. Cut order if behind: audit hash chain → Jaeger UI → P1.
