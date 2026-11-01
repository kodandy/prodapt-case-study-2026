# Detailed architecture

Status: **Confirmed** (2026-11-01). Builds on D-001, D-010–D-013.
Decisions: agent loop (D-014, §5), answer contract (D-015, §5), ingestion via gateway (D-016, §2), tenant enforcement (D-017, §5), all remaining sections (D-018).
Reviewed with a cross-model consult on 2026-11-01 (2 rounds); its fixes are already applied below.

## 1. Repository layout

```
contracts/                  # the only package both services import: event + port schemas (Pydantic) → JSON Schema export
proto/                      # gRPC merchant API
services/gateway/src/gateway/
  accounts/                 # sign-up, login, TOTP, JWT issue (identity provider)
  wallets/
  payments/                 # charge + P2P transfer use cases
    ledger/  fx/  tokenization/  idempotency/  routing/  providers/ (simulator adapter)  reconciler/
  refunds_disputes/
  risk/                     # sync scoring, real-time card-testing block, decline rules (corridor, limits)
  settlement/
  merchant_api/  rest/  grpc/
  ops_port/  query_api/  command_api/   # the D-013 port, gateway side
  outbox/                   # table + relay
  notifications/            # transactional (confirmations)
  audit/
services/ops-ai/src/ops_ai/
  ingest/                   # stream consumers → projections
  search/                   # FTS + pgvector + RRF
  investigation/            # deterministic tools
  assistant/                # tool loop, LlmPort, mock, guardrails, answer checker
  memory/
  fraud_watch/              # delayed card-testing detector
  alerts/  tickets/
  evaluation/
web/src/features/{user,merchant,ops}/
scripts/                    # generate_dataset.py, import, replay
```

Each feature keeps its tests in its own `__tests__/`. An import-rule test fails if `gateway` imports `ops_ai` or the reverse.

## 2. Data and ingestion

- The generator writes a **canonical scenario log**: amounts in minor units with the currency exponent, plus attempts, settlements, disputes and incidents with resolution notes.
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

### Ledger
- Double-entry. Each owner gets one account per currency.
- Platform accounts: clearing (one per currency), FX, fees, reserve, provider receivable.
- Balances are derived from postings, with a cached balance row updated under a row lock in the same transaction.
- Amounts are `Decimal` / minor units only. Float is never used inside the system.

### State machines (per payment method)

| Method | Flow |
|---|---|
| card | sale (authorize + capture) is synchronous → `succeeded \| declined \| failed \| unknown` |
| wallet / P2P | internal and synchronous |
| bank_transfer, upi | `pending` → the simulator settles or fails it asynchronously |
| refund | `requested` → `succeeded \| failed`, with a reversal journal |
| dispute | `opened → under_review → won \| lost`. `lost` posts the chargeback and a chargeback fee |

- Refunds and dispute losses are checked under the same `SELECT … FOR UPDATE` on the payment: refunded principal + chargeback principal must stay ≤ the captured amount. Fees are booked separately.

### Sync charge path (T0, no LLM)
1. Authenticate the API key (stored hashed).
2. Validate (Pydantic / proto, Luhn, test-PAN allowlist).
3. Tokenize: the vault keeps the token, BIN, last4 and fingerprint. The PAN is never stored.
4. **tx1**: insert the idempotency row `(merchant_id, key, request_hash, lease_until)` and create the payment and the attempt in the same transaction. The provider key is the attempt id.
   - On conflict with a different hash: 422.
   - On conflict with the same hash: wait up to 5 s for the stored response, then return 409 `idempotency_in_progress` with `Retry-After`.
   - Async methods return a fixed 202 as their idempotent response. The current state is read from `GET`.
5. Risk score (rules → 0.0–1.0 + reason codes). Above the block threshold: decline and flag.
6. Route selection, recording the config version.
7. Call the provider **outside** the transaction.
8. **tx2**: `UPDATE … WHERE state IN ('created','unknown')`. Only the winner writes the journal, the outbox rows and the stored response.

**Reconciler**
- Picks up attempts still in `created` after their lease and looks them up at the provider by key.
- `not_found` before the provider's acceptance deadline means `unknown`, not `failed`. The reconciler re-queries with backoff and resends only with the same key.
- After the deadline, `not_found` means the request was never received, so `failed` is safe.
- If the deadline has passed and the result is still unknown, the payment is surfaced to ops and a ticket is raised.

**Guarantees we claim**
- The side effect happens at most once.
- Once a request completes, retries get the stored response.
- The "20 concurrent → 20 identical bodies" test only holds when the provider finishes within the wait budget.

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
- Deterministic fault profiles per route.
- Lookup by key, with a declared acceptance deadline.
- A scenario-control endpoint that only exists when `DEMO_MODE` is set.

### Routing
- Configs are versioned rows. Every change emits a `config.changed` event.

### Identity
- `accounts` issues short-lived asymmetric JWTs carrying `user_id`, `roles` and `merchant_id`.
- Roles: `consumer`, `merchant_admin`, `merchant_support`, `ops_agent`, `ops_lead`.
- Passwords use argon2. MFA is TOTP, and the TOTP secret is encrypted with a key from the environment.

### Audit
- `audit_log` is append-only and records refunds, disputes, proposals, approvals and command executions.

## 4. The port in detail

### Streams
- Topics: `gw.payments.v1`, `gw.risk.v1`, `gw.disputes.v1`, `gw.settlement.v1`, `gw.config.v1`, `gw.provider_health.v1`.
- The relay uses `FOR UPDATE SKIP LOCKED`, marks rows published, and keeps outbox rows. The demo keeps all of them. Production would add snapshots and a retention window.
- `scripts/replay` replays from an offset.

### Consumers
- One Redis consumer group per concern.
- Dedupe uses `processed_events(event_id)`, written in the same transaction as the projection.
- State projections apply the event with the highest `entity_version` (last writer wins).

### Query API (`/internal/v1/query/...`)
- Two lookups: one payment's status with its decline detail, and one settlement's breakdown.
- Both require a service credential plus the forwarded user JWT. The gateway authorizes on both.

### Command API (routing override)
1. ops-ai creates a proposal: route, from/to provider, TTL, expected `config_version`.
2. An `ops_lead` approves it in the UI. The approval call goes to the gateway with the user's JWT.
3. The approval is bound to `proposal_id` + the SHA-256 of the canonical proposal, has a 10-minute expiry, and is single-use.
4. The gateway re-validates the current version and the permission, then applies the override.
5. When the TTL expires, the override is reverted only if the config is still at the version it set.

The LLM can propose. It never approves.

## 5. ops-ai

### Projections
- `payment_facts`: one row per payment, holding route, provider, BIN prefix, raw and normalized codes, and risk.
- `search_documents`: payments, incidents and resolution notes, with a `tsvector` column. There is one embeddings table per embedder.
- `change_timeline`, `provider_health`, `card_testing_windows`.
- Aggregates are SQL `GROUP BY` over `payment_facts` at query time. There are no incremental counters, so reordered or replayed events cannot double-count.
  - In production this would be materialized views or continuous aggregates.

### Search
- Full-text search and pgvector are fused with RRF.
- The keyword side sits behind an interface, so Japanese tokenization (e.g. pg_bigm) can be swapped in later.

### Investigation tools
- All tools are deterministic, with typed I/O generated from Pydantic: `aggregate_failures`, `compare_baseline`, `change_timeline`, `search_incidents`, `get_payment_status` (calls the gateway), `analyze_fraud`, `explain_settlement` (calls the gateway), `propose_routing_override`.
- Which tools are exposed depends on the caller's role and the adapter's capabilities.

### Assistant loop
- A hand-rolled loop on the OpenAI Responses API, behind `LlmPort`.
- Limits: at most 6 tool calls and a 20 s budget. Beyond that it falls back to T2: dashboards plus the evidence list.
- The mock `LlmPort` (a rule-based NL → typed query parser with templated narration) only proves the wiring.
- A real-API run of the 4 sample queries is reported separately, with its date and model.

### Answer contract (differentiator candidate)
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
- Window features: distinct cards, small-amount share, decline ratio, BIN spread.
- Produces an alert plus a ticket in the `fraud` queue, deduped by window key.

### Tickets
- A rules table maps each event to a queue (`fraud`, `payments_ops`, `settlement`, `compliance`) and a priority.
- Lifecycle: `open → assigned → resolved`. Tickets are deduped by source event.

## 6. Web and ops

- One Vite SPA with three areas: user, merchant and ops console. nginx routes `/api/gw` and `/api/ops`.
- Logs are structured JSON with a redaction filter. The correlation id travels in event headers.
- The clock is injectable, with a fixed `DEMO_NOW`. Notifications are an in-app table plus a log adapter.

### Restart behavior (tested)

| Failure | Expected recovery |
|---|---|
| Relay crash | Unpublished outbox rows resume |
| ops-ai crash | The consumer group resumes from pending entries |
| Projection wipe | Rebuilt by replaying the retained outbox |
| Gateway crash between the provider call and tx2 | The reconciler resolves the attempt by its key |

## 7. Top risks (fix first)

1. Recovery when the provider result is unknown, and double-posting (§3 reconciler).
2. A refund and a chargeback together returning more than the captured principal (§3 state machines).
3. Tenant leakage, and AI claims that go beyond the evidence (§5 tenant enforcement, answer contract).
