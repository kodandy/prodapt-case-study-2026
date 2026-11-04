# Requirements Traceability

Source: `docs/caes-study.pdf` ("Requirement 1 (Basic)", "Sample queries") and the production-readiness instruction (D-020).
Every requirement is split into sub-items. A sub-item is **Done** only when both the
"Satisfied by (code)" and "Verified by (test)" columns point at real files and the test passes in `make ci`.

Priority (D-020, strict): **Basic** (B1–B13, SQ, D, DL) → **P0** (implemented and proved by tests) → **P1** (if time) → **P2** (design doc only). P0/P1 work never displaces a Basic item.

Status legend: `Planned` → `In progress` → `Done` → `Re-verified` (Phase 5 final check).

Path prefixes: `gw/` = `services/gateway/src/gateway/`, `ai/` = `services/ops-ai/src/ops_ai/`. Tests live in `<feature>/__tests__/`.
Paths are planned locations from `docs/architecture-detail.md` §1; they are fixed when the code lands.

## B1. User account management with sign-up, login, MFA, and wallet management

| ID | Sub-requirement | Acceptance criterion | Satisfied by (code) | Verified by (test) | Phase | Status |
|---|---|---|---|---|---|---|
| B1.1 | Sign-up | Password stored as argon2 hash, never plain; duplicate email → 409 | `gw/accounts/` | `gw/accounts/__tests__/` | 2 | Planned |
| B1.2 | Login | Valid credentials → session token; wrong password → 401; secrets never logged | `gw/accounts/` | `gw/accounts/__tests__/` | 2 | Planned |
| B1.3 | MFA (TOTP) | Enrol returns provisioning URI; when MFA is enabled, token is issued only after a valid TOTP; wrong/expired code → 401; TOTP secret encrypted at rest | `gw/accounts/` | `gw/accounts/__tests__/` | 2 | Planned |
| B1.4 | Wallet management | Create one wallet per currency, top-up, read balance per currency; balance is derived from ledger postings; `available` and `held` shown separately | `gw/wallets/` | `gw/wallets/__tests__/` | 2 | Planned |

## B2. Payment processing for peer-to-peer and merchant transactions with multi-currency support

| ID | Sub-requirement | Acceptance criterion | Satisfied by (code) | Verified by (test) | Phase | Status |
|---|---|---|---|---|---|---|
| B2.1 | P2P payment | Wallet-to-wallet transfer posts balanced journal in one DB transaction under the user-account lock | `gw/payments/` | `gw/payments/__tests__/` | 2 | Planned |
| B2.2 | Merchant payment | Charge with a tokenised **test** card; raw PAN never persisted | `gw/payments/`, `gw/payments/tokenization/` | `gw/payments/__tests__/` | 2 | Planned |
| B2.3 | Multi-currency | Cross-currency payment posts FX legs through per-currency clearing accounts; each currency nets to 0; rate id on the journal | `gw/payments/fx/` | `gw/payments/__tests__/` | 2 | Planned |
| B2.4 | Atomicity / ledger balance | Insufficient funds → 422 with zero postings; every journal balances per currency (see P0-2) | `gw/payments/ledger/` | `gw/payments/ledger/__tests__/` | 2 | Planned |
| B2.5 | External-rail wallet debit | `bank_transfer` / `upi` payout takes a hold in tx1; success captures, definitive failure releases, `unknown` keeps it held; duplicate capture/release is a no-op | `gw/wallets/`, `gw/payments/` | `gw/wallets/__tests__/` | 2 | Planned |

## B3. Transaction history, refunds, and dispute resolution workflows

| ID | Sub-requirement | Acceptance criterion | Satisfied by (code) | Verified by (test) | Phase | Status |
|---|---|---|---|---|---|---|
| B3.1 | Transaction history | `GET /v1/payments` (merchant, REST + gRPC `ListPayments`) and `GET /v1/me/transactions` (consumer); cursor-paginated; filter by status, date range, method; caller sees only own/tenant records | `gw/payments/` | `gw/payments/__tests__/` | 2 | Planned |
| B3.2 | Refunds | Full and partial; cumulative refund ≤ captured amount; duplicate refund rejected; requires `Idempotency-Key` | `gw/refunds_disputes/` | `gw/refunds_disputes/__tests__/` | 2 | Planned |
| B3.3 | Dispute workflow | State machine `opened → under_review → won \| lost`; invalid transitions rejected; outcome from the simulated network or recorded by `ops_lead`; merchant can only submit evidence; `lost` posts chargeback and sets `chargeback_flag` | `gw/refunds_disputes/` | `gw/refunds_disputes/__tests__/` | 2 | Planned |
| B3.4 | Refund × chargeback conflict | Refund + chargeback on the same payment can never return more than the captured amount | `gw/refunds_disputes/` | `gw/refunds_disputes/__tests__/` | 2 | Planned |

## B4. NLP-enabled transaction failure reason detection

| ID | Sub-requirement | Acceptance criterion | Satisfied by (code) | Verified by (test) | Phase | Status |
|---|---|---|---|---|---|---|
| B4.1 | Natural-language query by support agents | NL text → typed `FailureQuery` (merchant, window, method, status) via LLM function calling; mock LLM gives the same structure | `ai/assistant/` | `ai/assistant/__tests__/` | 3 | Planned |
| B4.2 | Structured failure reasons | Raw/normalized codes and free-text `failure_reason` classified rules-first into `{taxonomy_code, category, owner}`; LLM fallback proposes from a closed list with confidence, low confidence → `unclassified`; `retryable` derived by a deterministic policy (timeouts/`unknown` never retryable); response groups `{taxonomy_code, share, count, sample_ids}` | `ai/taxonomy/` | `ai/taxonomy/__tests__/` | 3 | Planned |
| B4.3 | Suggested fixes | Each reason group carries a fix from the versioned playbook, enriched by matching resolution notes; observed facts, hypotheses and how to confirm are separated | `ai/taxonomy/`, `ai/assistant/` | `ai/assistant/__tests__/` | 3 | Planned |

## B5. RAG-enabled support assistant for payment troubleshooting using historical transaction data

| ID | Sub-requirement | Acceptance criterion | Satisfied by (code) | Verified by (test) | Phase | Status |
|---|---|---|---|---|---|---|
| B5.1 | Retrieval over historical data | Retrieves failed/flagged transactions and past incidents (`resolution_notes`) through hybrid search (B6) | `ai/search/`, `ai/investigation/` | `ai/investigation/__tests__/` | 3 | Planned |
| B5.2 | Grounded answer | Answer contract: each claim carries evidence refs produced this turn; slot values rendered from tool output; with insufficient evidence the assistant abstains | `ai/assistant/` | `ai/assistant/__tests__/`, `ai/evaluation/` | 3 | Planned |

## B6. Hybrid search combining keyword and semantic retrieval over transaction records

| ID | Sub-requirement | Acceptance criterion | Satisfied by (code) | Verified by (test) | Phase | Status |
|---|---|---|---|---|---|---|
| B6.1 | Keyword retrieval | Postgres full-text search (`tsvector`) behind a swappable interface | `ai/search/` | `ai/search/__tests__/` | 3 | Planned |
| B6.2 | Semantic retrieval | pgvector similarity; embedder swappable (OpenAI / pinned local model), one index per embedder | `ai/search/` | `ai/search/__tests__/` | 3 | Planned |
| B6.3 | Fusion | Reciprocal Rank Fusion; on a fixed corpus a keyword-only miss is found by vector and vice versa | `ai/search/` | `ai/search/__tests__/` | 3 | Planned |

## B7. Fraud detection and basic risk scoring with flagging of suspicious transactions

| ID | Sub-requirement | Acceptance criterion | Satisfied by (code) | Verified by (test) | Phase | Status |
|---|---|---|---|---|---|---|
| B7.1 | Risk scoring | Synchronous rule score 0.0–1.0 with reason codes at authorisation; no LLM on the path; velocity-lookup failure follows the dependency policy (`risk_unavailable` / `degraded_scoring`) | `gw/risk/` | `gw/risk/__tests__/` | 2 | Planned |
| B7.2 | Flagging | Fraud flag is separate from payment status (a declined payment can still be flagged) | `gw/risk/` | `gw/risk/__tests__/` | 2 | Planned |
| B7.3 | Card-testing detection | Real-time block in gateway; async detector in ops-ai flags the planted burst exactly on a fixed fixture; false-positive rate measured on legitimate micro-payment groups | `gw/risk/`, `ai/fraud_watch/` | `ai/fraud_watch/__tests__/` | 3 | Planned |

## B8. REST/gRPC APIs for merchant integrations with input validation guardrails and idempotency enforcement

| ID | Sub-requirement | Acceptance criterion | Satisfied by (code) | Verified by (test) | Phase | Status |
|---|---|---|---|---|---|---|
| B8.1 | REST API | `POST /v1/payments`, `GET /v1/payments/{id}`, `GET /v1/payments`, refunds; merchant API key auth (stored hashed) | `gw/app.py`, `gw/payments/history.py` (`get_payment`, `list_payments`), `gw/payments/refunds.py`, `gw/accounts/service.py` (HMAC-SHA256 key hash) | `gw/payments/__tests__/test_charge.py`, `gw/payments/__tests__/test_history.py`, `gw/payments/__tests__/test_refunds.py` | 2 | Done |
| B8.2 | gRPC API | `CreatePayment` / `GetPayment` / `ListPayments` call the same domain service as REST | `gw/merchant_api/grpc/` | `gw/merchant_api/__tests__/` | 2 | Planned |
| B8.3 | Input validation guardrails (API) | Invalid currency, amount, card (Luhn / non-test PAN), oversize fields → field-level errors on both REST and gRPC, from one validation module | `gw/merchant_api/` | `gw/merchant_api/__tests__/` | 2 | Planned |
| B8.4 | Idempotency enforcement | `Idempotency-Key` header (REST) / `idempotency-key` metadata (gRPC); lost-response replay returns the stored response; same key + different body → 422; concurrency proof in P0-1 | `gw/payments/idempotency/` | `gw/payments/idempotency/__tests__/` | 2 | Planned |
| B8.5 | Guardrails (AI side) | Tenant scope taken from auth, never from LLM output; tool allowlist + typed args; retrieved text treated as untrusted (see P0-8) | `ai/assistant/` | `ai/assistant/__tests__/` | 3 | Planned |

## B9. Tool-calling support for payment status lookup and fraud analysis

| ID | Sub-requirement | Acceptance criterion | Satisfied by (code) | Verified by (test) | Phase | Status |
|---|---|---|---|---|---|---|
| B9.1 | `get_payment_status` tool | Assistant calls it with correct args for SQ2 | `ai/investigation/` | `ai/assistant/__tests__/` | 3 | Planned |
| B9.2 | `analyze_fraud` tool | Assistant calls it with correct args for SQ3 | `ai/investigation/` | `ai/assistant/__tests__/` | 3 | Planned |
| B9.3 | Supporting tools | `aggregate_failures` (SQ1), `search_incidents` (SQ1/RAG), `explain_settlement` (SQ4); test asserts tool name + args per sample query | `ai/investigation/` | `ai/assistant/__tests__/` | 3 | Planned |

## B10. Memory and session management for merchant support interactions

| ID | Sub-requirement | Acceptance criterion | Satisfied by (code) | Verified by (test) | Phase | Status |
|---|---|---|---|---|---|---|
| B10.1 | Session management | Redis session keyed by `(merchant_id, user_id, session_id)` with TTL and ownership check; merchant A cannot read merchant B's session | `ai/memory/` | `ai/memory/__tests__/` | 3 | Planned |
| B10.2 | Short-term memory | Last N turns + structured conversation state; "what about yesterday?" keeps merchant + metric and shifts the window | `ai/memory/` | `ai/memory/__tests__/` | 3 | Planned |
| B10.3 | Long-term memory | Explicit, auditable `merchant_memory` records written only via a confirmed tool call, with source turn id; recalled in a new session; correct/delete endpoint | `ai/memory/` | `ai/memory/__tests__/` | 3 | Planned |

## B11. Notifications for payment confirmations and suspicious activity alerts

| ID | Sub-requirement | Acceptance criterion | Satisfied by (code) | Verified by (test) | Phase | Status |
|---|---|---|---|---|---|---|
| B11.1 | Payment confirmation | Event written to the transactional outbox in the payment DB transaction; worker delivers it; shown in UI | `gw/notifications/`, `gw/outbox/` | `gw/notifications/__tests__/` | 2 | Planned |
| B11.2 | Suspicious activity alert | Gateway risk flag → "was this you?" notice to the cardholder/merchant; ops-ai fraud_watch → alert to ops staff only | `gw/notifications/`, `ai/alerts/` | `gw/notifications/__tests__/`, `ai/alerts/__tests__/` | 2–3 | Planned |
| B11.3 | At-least-once safety | Redelivered event creates no duplicate notification or alert | `gw/notifications/`, `ai/alerts/` | `gw/notifications/__tests__/` | 2 | Planned |

## B12. Simple ticket routing for operational escalations

| ID | Sub-requirement | Acceptance criterion | Satisfied by (code) | Verified by (test) | Phase | Status |
|---|---|---|---|---|---|---|
| B12.1 | Escalation → ticket | Assistant escalation, fraud alert, dispute and unresolved `unknown` attempt create tickets with reason | `ai/tickets/` | `ai/tickets/__tests__/` | 3 | Planned |
| B12.2 | Routing | Rule router → queue `{fraud, payments_ops, settlement, compliance}` + priority | `ai/tickets/` | `ai/tickets/__tests__/` | 3 | Planned |
| B12.3 | Lifecycle + dedupe | `open → assigned → resolved`; duplicate source event creates no second ticket | `ai/tickets/` | `ai/tickets/__tests__/` | 3 | Planned |

## B13. Simple front-end interface for user and merchant interaction

| ID | Sub-requirement | Acceptance criterion | Satisfied by (code) | Verified by (test) | Phase | Status |
|---|---|---|---|---|---|---|
| B13.1 | User screen | Sign-up/login with MFA, wallets, pay, history, refund request / dispute | `web/src/features/user/` | Playwright smoke | 2–3 | Planned |
| B13.2 | Merchant screen | Payments list, settlement summary, support assistant chat; refund from a `suggested_action` requires explicit confirmation | `web/src/features/merchant/` | Playwright smoke | 2–3 | Planned |
| B13.3 | Ops console (addition) | NL failure query, investigation with evidence, flagged transactions, alerts, ticket queue, approvals | `web/src/features/ops/` | Playwright smoke | 3 | Planned |

## Sample queries (must run end-to-end in the demo)

| ID | Query | Expected evidence (tools) | Expected output | Test | Phase | Status |
|---|---|---|---|---|---|---|
| SQ1 | 15% of a merchant's card payments failing this afternoon — root cause and fix | `aggregate_failures`, `compare_baseline`, `change_timeline`, `search_incidents` | Failure rate vs baseline; concentration on one issuer/BIN range in the planted window; observed facts vs hypotheses vs how to confirm; ranked fixes from the playbook; citations | `ai/evaluation/__tests__/` (E2E) | 3 | Planned |
| SQ2 | International transfer declined despite sufficient funds — what triggered it | `get_payment_status` | Balance shown sufficient (rules out NSF); the rule that fired (corridor / KYC cross-border limit) with the values that tripped it; fix | `ai/evaluation/__tests__/` (E2E) | 3 | Planned |
| SQ3 | Detect and flag micro-transaction series (card testing) | live burst via merchant API → detector, `analyze_fraud` | Flagged ids + pattern features; alert notification; ticket in `fraud` queue | `ai/evaluation/__tests__/` (E2E) | 3 | Planned |
| SQ4 | Plain-English settlement summary: why payout differs from expected | `explain_settlement` | Typed breakdown (gross, MDR, FX, refunds, chargebacks + fees, reserve → net) verified before narration; plain-English text | `ai/evaluation/__tests__/` (E2E) | 3 | Planned |

## Dataset

| ID | Requirement | Acceptance criterion | Satisfied by | Verified by | Phase | Status |
|---|---|---|---|---|---|---|
| D1 | Synthetic dataset, ≥ 10,000 records | Seeded, reproducible; CSV and JSON derived from one canonical scenario log | `scripts/generate_dataset.py` | `scripts/__tests__/` | 1 | Planned |
| D2 | Minimum schema | All 16 required fields with required types and enum values | `scripts/generate_dataset.py` | `scripts/__tests__/` | 1 | Planned |
| D3 | Planted scenarios for SQ1–SQ4 | Each scenario present and detectable | `scripts/generate_dataset.py` | `scripts/__tests__/` | 1 | Planned |
| D4 | Schema extensions documented | README lists every added field, incl. incident resolution-effectiveness fields | `README.md` | review | 5 | Planned |

## P0. Production readiness — implemented and proved by tests (D-020)

| ID | Item | Acceptance criterion | Satisfied by (code) | Verified by (test) | Phase | Status |
|---|---|---|---|---|---|---|
| P0-1 | Idempotency: DB unique + Redis | N=20 concurrent same key → simulator charge count 1, one journal, identical bodies within the wait budget; repeated with Redis flushed, Redis down, lock expired | `gw/payments/idempotency/` | `gw/payments/idempotency/__tests__/` | 2 | Planned |
| P0-2 | Balanced ledger | Hypothesis property test: random sequences of charges, transfers, FX, refunds, chargebacks, holds → every journal balances per currency; account balances = sum of postings | `gw/payments/ledger/` | `gw/payments/ledger/__tests__/` | 2 | Planned |
| P0-3 | Balance locking | Pessimistic `SELECT … FOR UPDATE` on user accounts in id order, covering balance check → hold/journal; platform accounts append-only. N parallel debits never overdraw. Rationale written in architecture-detail §3 | `gw/wallets/`, `gw/payments/ledger/` | `gw/wallets/__tests__/` | 2 | Planned |
| P0-4 | Crash safety | Crash matrix C1–C5 (architecture-detail §7), one assertion set per failure point; at least one real process kill + restart | `gw/payments/reconciler/`, `gw/outbox/`, `ai/ingest/` | `gw/payments/reconciler/__tests__/`, `tests/e2e/crash/` | 2 | Planned |
| P0-5 | LLM outage isolation | With `LlmPort` raising: charges succeed, risk flags and card-testing alerts still raised, assistant returns T2 evidence list | `gw/risk/`, `ai/assistant/` | `ai/assistant/__tests__/`, `gw/risk/__tests__/` | 3 | Planned |
| P0-6 | PII / card masking before LLM | Allowlisted DTO + free-text redaction; canary PAN/email/phone/password/token never appear in any `LlmPort` payload (exact-match assertion, independent of the detector) | `ai/assistant/llm_input/` | `ai/assistant/__tests__/` | 3 | Planned |
| P0-7 | Read-only tools, human approval for money | Tool registry contains no write tool except the routing proposal; refund only via gateway API with the human's JWT and explicit confirmation; `suggestion_id` linked in audit only after server-side check | `ai/investigation/`, `gw/refunds_disputes/` | `ai/investigation/__tests__/`, `gw/refunds_disputes/__tests__/` | 3 | Planned |
| P0-8 | Prompt-injection defense | Injection fixtures in resolution notes, memory and user input do not change the tool set, scope or checker outcome; schema-invalid model output rejected | `ai/assistant/` | `ai/assistant/__tests__/` | 3 | Planned |
| P0-9 | RBAC | Policy table `role × action` (consumer, merchant_admin/merchant_support, ops_agent, ops_lead = admin); generated matrix test + consumer ownership + merchant tenancy tests | `gw/accounts/` | `gw/accounts/__tests__/` | 2 | Planned |
| P0-10 | Append-only audit | UPDATE/DELETE rejected for the app role (trigger + grants); audit row in the same tx as the operation, failure rolls the operation back; who/when/what/correlation id recorded. Hash chain optional (cut-first) | `gw/audit/` | `gw/audit/__tests__/` | 2 | Planned |
| P0-11 | No secrets / PII in logs | Canary values run through a full E2E flow; captured logs of all services contain none of them | `gw/observability/`, `ai/observability/` | `tests/e2e/logs/` | 2–3 | Planned |
| P0-12 | Correlation id + OTel traces | One trace id across API span → outbox event header → consumer span → tool and LLM spans; every log line carries `trace_id` | `gw/observability/`, `ai/observability/` | `tests/e2e/tracing/` | 3 | Planned |
| P0-13 | Metrics | `/metrics` exposes request count, errors, latency histograms (p50/p95/p99), payment success rate, failures by taxonomy code, fraud flags, LLM tokens/latency/cost/eval score; mock values labeled `mock` | `gw/observability/`, `ai/observability/` | scrape tests per service | 3 | Planned |
| P0-14 | Alert rules | Rule file (card failure rate > 3× baseline, provider errors, outbox/consumer lag, reconciler unknown, LLM fallback); `promtool test rules` passes on fixtures | `ops/alerts.yml` | `ops/alerts.test.yml` | 3 | Planned |
| P0-15 | CI with eval gate | `ci.yml` + `make ci`: ruff, mypy strict, pytest, tsc, eslint, eval; deterministic DeepEval custom metrics gate on thresholds; cross-tenant leakage gate fails on a single leak; LLM-judge metrics only with a key, skip reported | `.github/workflows/ci.yml`, `Makefile`, `ai/evaluation/` | CI run log | 1→5 | Planned |

## P1. If time

| ID | Item | Acceptance criterion | Phase | Status |
|---|---|---|---|---|
| P1-1 | Transactional outbox | Already Basic (B11.1) and covered by P0-4; listed here only to match the instruction | 2 | Planned (via B11.1) |
| P1-2 | Retry + DLQ | Consumer retries with exponential backoff and jitter; dead-letter stream after N failures; test that a poison event lands in the DLQ and the rest proceed | 4 | Planned |
| P1-3 | Provider timeout + circuit breaker | Breaker per route opens on error rate, half-opens after cool-down; `unknown` attempts are never rerouted | 4 | Planned |
| P1-4 | Rate limits | Token bucket per merchant API key and per user; 429 with `Retry-After`; Redis down → fail-open with a metric | 4 | Planned |
| P1-5 | Load test | k6 or Locust on `POST /v1/payments` (card) at a stated load; p50/p95/p99 + error rate in the design doc, with the 100k TPS production design | 4 | Planned |
| P1-6 | Health, readiness, graceful shutdown | `/healthz` and `/readyz` (DB, Redis, stream reachability) on every process. On SIGTERM: stop accepting requests, drain in-flight ones; a consumer finishes its current message and XACKs before exit; the relay finishes its current batch. Test: SIGTERM mid-processing → no duplicate and no lost work | 2 | Planned (1st in P1 order) |
| P1-7 | DB migrations (Alembic) | Separate migration history per service; upgrade from empty DB to head and downgrade by one step run in CI. Pulled forward to Day 1 (D-021): the schema is created through Alembic from the start | 1 | Planned (Day 1) |
| P1-8 | Model and prompt pinning, fallback model, LLM cost cap | Model ids pinned to dated versions in env; prompts are versioned files whose hash is stored in the AI audit trail; primary model failure → fallback model → T2; daily token budget per merchant, exceeded → T2 with a metric. Tests for each path on the mock `LlmPort` | 3 | Planned (2nd in P1 order) |

P1 order (cheapest per value first, D-021): P1-6 → P1-8 → P1-5 → P1-3 → P1-4 → P1-2. P1-7 is not in this order because it moved to Day 1.

## P2. Design doc only

Written in `docs/design.md` with a compose → GCP mapping (Cloud Run / GKE, Cloud SQL / Spanner, Memorystore, Pub/Sub replacing Redis Streams, Cloud KMS / HSM, Managed Prometheus, Cloud Trace). OTel and Prometheus carry over unchanged.

| ID | Item | Content |
|---|---|---|
| P2-1 | Multi-region active-active | Cloud Spanner vs. per-region Cloud SQL + reconciliation, compared on consistency, cost and operations; home region per account |
| P2-2 | 99.999 % availability | SPOF list, failover per component, error budget (≈ 26 s/month) and the policy when it is spent |
| P2-3 | PCI-DSS scope reduction | Tokenization vault in a separate project / VPC; keys in Cloud KMS or Cloud HSM |
| P2-4 | Data residency + GDPR erasure | Per-user keys, crypto-shredding on erasure; retention table per store |
| P2-5 | Release safety | Canary releases, feature flags, rollback procedure |
| P2-6 | Backup and restore | Backup policy, restore drill, RTO / RPO targets |
| P2-7 | 100k TPS topology | Stateless scale-out, time partitioning, sharding by home region, Pub/Sub or Kafka at scale (from P1-5) |
| P2-8 | Other design-only items | mTLS between services, WORM audit checkpoint, AML/KYC re-verification triggers |

## Deliverables

| ID | Deliverable | Satisfied by | Phase | Status |
|---|---|---|---|---|
| DL1 | Architecture diagram (PDF/JPEG) | `docs/architecture.md` → mmdc → `docs/architecture.pdf`, `.png` | 5 | Planned |
| DL2 | Design decisions and trade-offs | `docs/design.md`: every decision as Decision / Why / What we gave up / Alternatives (DOC-4); lock strategy, P0 evidence map, P2 items | 5 | Planned |
| DL3 | Executable code + README (setup, ≥ 1 E2E example) | `README.md` (all 4 sample queries with input and output; `make ci`) | 5 | Planned |
| DL4 | 10-minute presentation | `docs/presentation.md`: 0:30 goal, 4:00 SQ1–SQ4, ≥ 3:00 production design (PR-1–PR-3, one line on crash recovery), 0:30 wrap-up, 2:00 Q&A | 5 | Planned |

## Production documents and demo items (D-021)

| ID | Item | Satisfied by | Phase | Status |
|---|---|---|---|---|
| DOC-1 | Production readiness status per NFR | `docs/production-readiness.md`: each NFR marked 実装しテスト確認済み / 手元で検証済み / 設計のみ, with evidence (test name, metric, design section). Single source for the NFR claims (moved from architecture-detail §8) | 5 | Planned |
| DOC-2 | Runbook: card failure-rate spike | `docs/runbooks/card-failure-spike.md` (SQ1 scenario): alert fired → narrow by method / route / BIN → `change_timeline` → log and trace queries → rollback or approved reroute | 5 | Planned |
| DOC-3 | Runbook: LLM provider outage | `docs/runbooks/llm-provider-outage.md`: detect (LLM failure rate, fallback count) → confirm payments unaffected (T0 metrics) → run on fallback model or T2 → confirm recovery | 5 | Planned |
| DOC-4 | "Why / what we gave up" for every decision | `docs/design.md` format + `gave_up` field on each decision in `state/progress.json` | 5 | Planned |
| PR-1 | Demo: concurrent same-key test | Live run of the P0-1 test (≈ 40 s) | 5 | Planned |
| PR-2 | Demo: failure spike → alert → runbook | SQ1 data trips the P0-14 rule; shown on a provisioned Grafana dashboard next to Prometheus alerts, linked to DOC-2 (≈ 60 s) | 5 | Planned |
| PR-3 | Demo: NFR status table | DOC-1 (≈ 60 s) | 5 | Planned |
