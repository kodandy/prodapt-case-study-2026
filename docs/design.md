# Design decisions and trade-offs

Deliverable DL2 (`docs/requirements-traceability.md`). Every decision is written as **Decision / Why / What we gave
up / Alternatives** (DOC-4).

Snapshot: this document describes `main` at commit `b5f7271` (2026-11-04). Sources: `docs/architecture-detail.md`
(the planned design), `docs/submission-outline.md` (the scope anchor), `docs/specs/*.md` and the code under
`services/` and `web/`. Where the code differs from the planned design, the code wins and the difference is stated.

Later merges into `main` (wallets/FX/payouts, RBAC/audit role/KYC/GDPR, observability and resilience, gRPC, hybrid
search, the R2 agents) changed several statements; they were corrected on 2026-11-05 (D3.5, D5.6, D6.5, D6.6,
sections 8 and 9). Current status per requirement is in `docs/requirements-traceability.md`, NFR status and open gaps
in `docs/production-readiness.md`; where this document and those disagree, those win.

How claims are labeled:

- **Tested** — implemented, and a named test file under the repository asserts it.
- **Partial** — part of the mechanism is implemented and tested; the rest is stated as not built.
- **Design only** — described here, not implemented. Nothing labeled this way is claimed as working.

Path prefixes: `gw/` = `services/gateway/src/gateway/`, `ai/` = `services/ops-ai/src/ops_ai/`.

## Contents

1. [Problem and users](#1-problem-and-users)
2. [Architecture](#2-architecture)
3. [Payment correctness](#3-payment-correctness)
4. [Lock strategy](#4-lock-strategy)
5. [Operations intelligence](#5-operations-intelligence)
6. [Security](#6-security)
7. [Web client](#7-web-client)
8. [P0 evidence map](#8-p0-evidence-map)
9. [What was cut, and how it would be built](#9-what-was-cut-and-how-it-would-be-built)
10. [Production design on GCP (P2, design only)](#10-production-design-on-gcp-p2-design-only)

Topics named in the brief: vector store [D5.6](#d56-hybrid-search-postgres-full-text-plus-pgvector-fused-by-rrf),
message broker [D2.2](#d22-transactional-outbox-relay-redis-streams-deduped-projection),
caching [D2.6](#d26-caching-no-read-cache-by-choice), AI model selection [D5.7](#d57-ai-model-selection),
sync vs async [D2.1](#d21-two-services-with-a-one-way-event-port), [D3.7](#d37-synchronous-rule-based-risk-scoring-the-flag-is-separate-from-the-status),
[D5.4](#d54-card-testing-is-detected-twice-block-in-the-gateway-explain-in-ops-ai).

### Key decisions at a glance

| Topic | Choice | Main trade-off | Status | Details |
|---|---|---|---|---|
| Vector store | pgvector in the same Postgres, fused with full-text search by RRF | Default local embedder is feature hashing (not semantic); some paths still use full-text only | Tested | [D5.6](#d56-hybrid-search-postgres-full-text-plus-pgvector-fused-by-rrf) |
| Message broker | Transactional outbox → relay → Redis Streams, deduped consumer | No exactly-once; single-node Redis; outbox kept forever in the demo | Tested | [D2.2](#d22-transactional-outbox-relay-redis-streams-deduped-projection) |
| Caching | No read cache; money/account responses sent `no-store` | Repeated assistant questions re-run tools and the LLM | Tested | [D2.6](#d26-caching-no-read-cache-by-choice) |
| AI model | LLM only narrates evidence from tools; never decides a payment; `MockLlm` in CI | CI does not measure real-model prose quality | Partial (real adapter tested against a fake server only) | [D5.7](#d57-ai-model-selection) |
| Sync vs async | Money path and risk scoring synchronous; ops-ai fed asynchronously via events | ops-ai projection can lag (eventual consistency); risk is rule-based, no learned model | Tested | [D2.1](#d21-two-services-with-a-one-way-event-port), [D3.7](#d37-synchronous-rule-based-risk-scoring-the-flag-is-separate-from-the-status), [D5.4](#d54-card-testing-is-detected-twice-block-in-the-gateway-explain-in-ops-ai) |

---

## 1. Problem and users

When a payment fails, is declined, looks fraudulent or settles differently than expected, a merchant or an ops agent
gets the cause, the evidence and a suggested fix, while the payment path stays correct (no double charge, balanced
ledger, no card numbers stored) and keeps working when the LLM is down.

Primary users are merchants (integrate through the API, check payments and payouts, ask the assistant) and operations
staff (diagnose failures, review flagged transactions, handle tickets). Consumers use wallets, P2P transfers, refunds
and disputes. The four sample queries (SQ1 failure spike, SQ2 declined international transfer, SQ3 card testing,
SQ4 settlement gap) are instances of that one problem (`docs/product-brief.md`).

### D1.1 Prove correctness before breadth

- **Decision:** Order the work strictly Basic → P0 → P1 → P2. P0 items are claimed only with a test. P2 is this
  document only.
- **Why:** A payment system that is wide but wrong (double charges, unbalanced ledger) fails the review on its first
  question. Tests are the only evidence a reviewer can re-run.
- **What we gave up:** Breadth. Several Basic items are Thin (minimal, tested, not in the live walkthrough), and some
  sub-items are cut (section 9).
- **Alternatives:** Build every Basic item to demo depth first and harden later. Rejected because hardening after the
  fact is where double-charge and tenant-leak bugs hide.

---

## 2. Architecture

```
web (nginx, same origin) ──/api/gw──▶ gateway-api ──tx──▶ Postgres (gateway schema)
                         └─/api/ops─▶ ops-ai-api           │ outbox rows
                                        ▲                   ▼
                       ops-ai-consumer ◀── Redis Streams ◀── relay
                         (projections)      gw.payments.v1
```

Compose services: `postgres`, `redis`, `gateway-api`, `relay`, the gateway notifier, `ops-ai-api`,
`ops-ai-consumer`, `web` (`docker-compose.yml`, `make demo`).

### D2.1 Two services with a one-way event port

- **Decision:** A `gateway` service owns money (accounts, wallets, payments, ledger, refunds, disputes, risk). An
  `ops-ai` service owns investigation (projections, tools, assistant). They share only the `contracts` package (event
  schemas). Neither imports the other.
- **Why:** The payment path must not depend on the AI side. A crash, a slow LLM or a bad deploy of ops-ai cannot
  block or alter a charge. The boundary is enforced by a test, not by convention.
- **What we gave up:** One process and one database would be simpler to run and to query. ops-ai reads a projection
  that can lag behind the gateway (eventual consistency).
- **Alternatives:** A modular monolith with an import rule only. Rejected because the LLM outage isolation claim
  (P0-5) is easier to prove with a process boundary.
- **Evidence (Tested):** [`tests/test_import_rules.py`](../tests/test_import_rules.py)
  (`test_gateway_never_imports_ops_ai`, `test_ops_ai_never_imports_gateway`).

### D2.2 Transactional outbox, relay, Redis Streams, deduped projection

- **Decision:** A state change and its event are written in the same Postgres transaction (`gw/outbox/`). A separate
  `relay` process publishes unpublished rows to Redis Streams with `FOR UPDATE SKIP LOCKED`, in id order, and marks
  them published. The ops-ai consumer applies each event and records `processed_events(consumer, event_id)` in the
  same transaction, then `XACK`s. Delivery is at least once; the projection changes once.
- **Why:** No dual-write: either the payment and its event both commit or neither does. At-least-once plus dedupe is
  the simplest pair that loses nothing and double-counts nothing.
- **What we gave up:** Exactly-once delivery (not attempted). Outbox rows are kept forever in the demo (no retention
  window). Redis Streams is a single node in compose.
- **Alternatives:** Debezium / logical decoding (more infrastructure for the same guarantee); publishing to the stream
  inside the request (dual-write, loses events on a crash between the two writes); Kafka (heavier to run locally; it
  is the production option in P2-7).
- **Evidence (Tested):** [`gw/outbox/__tests__/test_relay.py`](../services/gateway/src/gateway/outbox/__tests__/test_relay.py),
  [`gw/outbox/__tests__/test_worker.py`](../services/gateway/src/gateway/outbox/__tests__/test_worker.py),
  [`ai/ingest/__tests__/test_consumer.py`](../services/ops-ai/src/ops_ai/ingest/__tests__/test_consumer.py)
  (`test_consume_payments_redelivery_of_the_same_event_changes_nothing`,
  `test_apply_payment_event_does_not_overwrite_a_higher_entity_version`).

### D2.3 Poison events go to a dead-letter stream

- **Decision:** A stream entry that does not parse as a valid event is copied to `gw.payments.v1.dead` (capped at
  about 10,000 entries) and acknowledged. An error while applying a *valid* event propagates and leaves the entry
  pending, so the worker retries it.
- **Why:** One malformed message must not block the stream, but a transient database error must not lose a valid
  event.
- **What we gave up:** Exponential backoff with jitter and a retry count before dead-lettering (P1-2 in full). Valid
  events retry on a fixed sleep.
- **Alternatives:** Dead-letter after N failures for every error class. Rejected for now because it would dead-letter
  valid events during a database outage.
- **Evidence (Tested):** [`ai/ingest/__tests__/test_consumer_dead_letter.py`](../services/ops-ai/src/ops_ai/ingest/__tests__/test_consumer_dead_letter.py),
  [`ai/ingest/__tests__/test_worker_dead_letter.py`](../services/ops-ai/src/ops_ai/ingest/__tests__/test_worker_dead_letter.py).

### D2.4 Historical data is seeded into ops-ai, not replayed through the gateway

- **Decision:** The 12k-record synthetic dataset (`scripts/generate_dataset.py`) is loaded into ops-ai through the
  same projection function the stream consumer uses. Each record carries `ingest_source` (`seed` / `live`). Live
  charges made during the demo flow through the gateway and the stream.
- **Why:** Replaying 12k payments through the gateway would need ledger rows, wallets and provider calls for history
  that only exists to be investigated. Using the consumer's own projection function keeps seed and live rows
  identical in shape.
- **What we gave up:** Seeded payments have no ledger rows in the gateway, so a gateway lookup of a seeded payment
  does not exist. `docs/architecture-detail.md` §2 planned a backfill importer through the gateway; it was not built.
- **Alternatives:** The planned backfill importer with `origin=backfill` events. Kept as the production path for a
  migration.
- **Evidence (Tested):** [`ai/seed/__tests__/test_seed_dataset.py`](../services/ops-ai/src/ops_ai/seed/__tests__/test_seed_dataset.py),
  [`ai/seed/__tests__/test_seed_mapping.py`](../services/ops-ai/src/ops_ai/seed/__tests__/test_seed_mapping.py),
  [`scripts/__tests__/test_generate_dataset.py`](../scripts/__tests__/test_generate_dataset.py).

### D2.5 ops-ai trusts gateway-issued tokens; scope comes from the token

- **Decision:** The gateway issues short-lived RS256 JWTs (`POST /v1/merchant-tokens`, `POST /v1/user-tokens`). ops-ai
  verifies them with the public key and builds its query scope (`merchant_id` or `user_id`) from the verified claims
  only. Lifetime is capped at 900 s and `iat` may not be more than 30 s in the future.
- **Why:** ops-ai never has to store credentials, and a request body cannot widen its own scope.
- **What we gave up:** Revocation before expiry (no denylist); key rotation is manual.
- **Alternatives:** A shared HMAC secret (any service that can verify can also mint); an introspection call to the
  gateway per request (couples ops-ai availability to the gateway).
- **Evidence (Tested):** [`ai/__tests__/test_verify_token.py`](../services/ops-ai/src/ops_ai/__tests__/test_verify_token.py),
  [`ai/__tests__/test_verify_token_hardening.py`](../services/ops-ai/src/ops_ai/__tests__/test_verify_token_hardening.py),
  [`gw/accounts/__tests__/test_tokens.py`](../services/gateway/src/gateway/accounts/__tests__/test_tokens.py),
  [`tests/e2e/test_runtime_contract.py`](../tests/e2e/test_runtime_contract.py)
  (`test_a_gateway_token_issued_for_one_merchant_never_unlocks_another_merchants_evidence`).

---

### D2.6 Caching: no read cache, by choice

- **Decision:** No service caches application reads. Every balance, payment status, risk decision and assistant
  answer is read from Postgres at request time. Responses that carry money or account data are sent with
  `Cache-Control: no-store` by the gateway and by nginx (`web/nginx.conf`). Two Redis uses look like caches but are not:
  the `idem:*` key is an in-flight lock that only suppresses contention (D3.1), and an idempotent replay returns the
  stored response from Postgres, which is the record of what happened, not a copy that can go stale.
- **Why:** The reads that matter most are freshness-critical. A cached balance, payment status or velocity count can
  approve a payment that should be refused, or show an ops user a state that is no longer true. At the measured local
  load the queries are indexed point reads and keyset pages, so a cache would add an invalidation problem without
  removing a bottleneck. ops-ai already serves analytics from its own projections, which are a deliberate, event-fed
  read model with a known lag (D2.2), not a cache of the gateway.
- **What we gave up:** Repeated identical questions to the assistant re-run tools and, with a key, re-call the LLM
  (cost and latency per call; bounded by D5.5's budget and concurrency cap). Static SPA assets are also served
  `no-store` today, which costs bandwidth but no correctness.
- **Where a cache would go first:** (1) content-hashed SPA assets with long `immutable` caching; (2) a short-TTL cache
  of lag-tolerant analytics aggregates in ops-ai (failure-rate and settlement summaries), keyed by tenant so scope is
  never shared; (3) an LLM answer cache keyed by tenant, question and the evidence hash, so a changed fact misses the
  cache. Balances, payment state and risk inputs stay uncached.
- **Alternatives:** Redis read-through for balances and payment status (rejected: stale money data); HTTP caching at
  nginx for API responses (rejected: per-user data, and it would need `Vary` on every credential).
- **Evidence (Code):** `NO_STORE_HEADERS` in `gw/app.py` and `gw/payments/consumer_routes.py`; `web/nginx.conf`;
  D3.1 for the Redis lock.

## 3. Payment correctness

### D3.1 Idempotency: Postgres is the arbiter, Redis only suppresses contention

- **Decision:** `POST /v1/payments` requires an `Idempotency-Key`. tx1 inserts `(merchant_id, key, request_hash,
  lease_until)` with `ON CONFLICT DO NOTHING` and creates the payment and the attempt in the same transaction. Only the
  inserter calls the provider. A Redis `SET NX PX` lock (10 s, released by a token-checked Lua script) keeps
  concurrent duplicates from all hitting Postgres. Same key + same request → the stored response; same key + different
  request → 422 `idempotency_key_reused`; still in flight after a 5 s wait → 409 `idempotency_in_progress`.
  `request_hash` covers amount, currency, method, card fingerprint and expiry, never the PAN.
- **Why:** Correctness must not depend on Redis. With Redis down, flushed or the lock expired, the unique constraint
  still yields one charge.
- **What we gave up:** A caller that retries during a long provider call gets 409 and must retry again. The
  "20 concurrent callers get 20 identical bodies" result only holds when the provider answers within the 5 s budget.
- **Alternatives:** Redis-only idempotency (wrong when Redis loses the key); a Postgres advisory lock per key (works,
  but holds a connection per waiter); serializable isolation (retry storms under contention).
- **Evidence (Tested):** [`gw/payments/__tests__/test_charge.py`](../services/gateway/src/gateway/payments/__tests__/test_charge.py)
  (`test_e1_twenty_concurrent_charges_with_one_key_make_one_provider_charge_with_redis`,
  `test_e1_twenty_concurrent_charges_with_one_key_make_one_provider_charge_without_redis`,
  `test_charge_card_same_key_with_a_different_amount_is_rejected_as_key_reuse`,
  `test_charge_card_second_caller_gets_409_while_the_first_is_still_at_the_provider`).
  The same pattern guards transfers and refunds:
  [`gw/wallets/__tests__/test_transfers.py`](../services/gateway/src/gateway/wallets/__tests__/test_transfers.py),
  [`gw/payments/__tests__/test_refunds.py`](../services/gateway/src/gateway/payments/__tests__/test_refunds.py).
  The "Redis flushed" and "lock expired" variants named in P0-1 are not separate tests; "without Redis" covers the
  path where no lock is taken at all.

### D3.2 The provider is called outside any transaction; tx2 is a compare-and-set

- **Decision:** The provider call happens between tx1 and tx2 with a timeout, using the attempt id as the provider's
  idempotency key. tx2 runs `UPDATE payments … WHERE state IN ('created','unknown')`; only the writer whose update
  matches a row writes the journal, the outbox event, the audit row and the stored response.
- **Why:** Holding a database transaction (and row locks) across a network call would let a slow provider exhaust the
  connection pool. The conditional update makes "finalize once" a property of one SQL statement.
- **What we gave up:** A window exists between the provider answering and tx2 committing. A crash there leaves the
  attempt in `created` until something looks it up (see D3.3).
- **Alternatives:** One transaction around the provider call (simple, does not scale, and still not crash-safe);
  a saga engine (more machinery than one payment step needs).
- **Evidence (Tested):** [`gw/payments/__tests__/test_charge.py`](../services/gateway/src/gateway/payments/__tests__/test_charge.py)
  (`test_e2_crash_after_tx2_commit_then_retry_returns_the_stored_body_without_a_second_charge`).

### D3.3 An unknown outcome is stored as 202 and never retried with a new key

- **Decision:** Only transport-level failures (`ProviderTimeoutError`, `ConnectionError`, `TimeoutError`) count as
  unknown. The payment moves to `unknown`, the idempotency row stores 202 with the body, and a retry with the same key
  replays that 202 without calling the provider. No journal, outbox row or audit row is written for an unknown
  outcome. Any other exception propagates and leaves the payment unfinalized.
- **Why:** Treating a timeout as a failure invites the merchant to retry with a new key, which is the classic double
  charge. Treating a programming error as unknown would hide bugs.
- **What we gave up:** **The reconciler is not built.** Nothing resolves an `unknown` or stale `created` attempt by
  looking it up at the provider. Those payments stay unresolved until manual action. Crash-matrix cases C1 and C5
  (section 8) are therefore design only.
- **Alternatives:** Retry the provider inline (blocks the request and still needs a lookup when retries run out).
- **Evidence (Tested):** [`gw/payments/__tests__/test_charge.py`](../services/gateway/src/gateway/payments/__tests__/test_charge.py)
  (`test_h1_*`, `test_h2_*`); spec `docs/specs/hardening.md` H1, H2.
- **Design only (reconciler):** pick up attempts in `created` past their 30 s lease or in `unknown`; look them up at
  the provider by attempt id; before the provider's acceptance deadline `not_found` stays `unknown` and is re-queried
  with backoff, resending only with the same key; after the deadline `not_found` is safely `failed`; near the end of
  the provider's idempotency retention window automatic resends stop and a manual-reconciliation ticket is opened.

### D3.4 Double-entry ledger in integer minor units, balanced per currency before commit

- **Decision:** Every money movement posts a journal whose lines sum to zero **per currency**; `post_journal` rejects
  anything else before it is written. Amounts are integer minor units everywhere inside the system (no float). A card
  charge posts `provider_receivable` (+) against `merchant_payable` (−); refunds and chargebacks post reversals;
  transfers post wallet against wallet.
- **Why:** A per-currency zero sum makes "money created or destroyed" a rejected write instead of a reconciliation
  finding. Integer minor units remove rounding as a class of bug.
- **What we gave up:** The check lives in application code, not in the database (no deferred constraint trigger), so
  a write that bypasses `post_journal` is not caught. The property test covers the journal function, not random
  sequences of whole business operations as P0-2 planned.
- **Alternatives:** A database constraint trigger on journal commit (stronger, cut for time); single-entry balance
  columns (no audit trail of why a balance changed).
- **Evidence (Tested):** [`gw/payments/ledger/__tests__/test_ledger_service.py`](../services/gateway/src/gateway/payments/ledger/__tests__/test_ledger_service.py)
  (Hypothesis: `test_property_any_balanced_multi_currency_journal_is_accepted_and_stored_balanced`,
  `test_property_any_journal_with_a_nonzero_residual_is_rejected_and_not_stored`,
  `test_post_journal_rejects_amounts_that_cancel_only_across_currencies`). Per-operation balance checks:
  [`test_charge.py`](../services/gateway/src/gateway/payments/__tests__/test_charge.py),
  [`test_refunds.py`](../services/gateway/src/gateway/payments/__tests__/test_refunds.py)
  (`test_refund_payment_keeps_the_ledger_balanced_per_currency_across_currencies`),
  [`test_disputes.py`](../services/gateway/src/gateway/payments/__tests__/test_disputes.py),
  [`test_transfers.py`](../services/gateway/src/gateway/wallets/__tests__/test_transfers.py)
  (`test_transfer_keeps_available_plus_held_equal_to_minus_the_postings_for_every_wallet`).

### D3.5 Per-currency wallets; FX legs through per-currency clearing accounts

- **Decision:** A user has one wallet per currency. A same-currency transfer debits only the wallet of the requested
  currency. A cross-currency transfer or card charge posts FX legs through per-currency `fx_clearing` accounts —
  sender side (ccy A) against FX clearing A, FX clearing B against the receiving side (ccy B) — so each currency
  nets to zero, with the `fx_rate_id` stored on the journal. A refund of an FX-settled charge reverses at the charge's
  own rate.
- **Why:** Per-currency zero sums keep the ledger invariant (D3.4) unchanged when currencies are mixed.
- **What we gave up:** Rates are 12 static demo rates seeded by a migration (no live feed). Chargebacks of FX-settled
  charges post in the payment currency without FX legs. No FX spread fee line.
- **Alternatives:** One cross-currency journal balanced in a base currency (breaks the per-currency invariant).
- **Evidence (Tested):** [`gw/wallets/__tests__/test_transfers.py`](../services/gateway/src/gateway/wallets/__tests__/test_transfers.py)
  (`test_transfer_debits_only_the_wallet_of_the_requested_currency`),
  [`test_fx_transfers.py`](../services/gateway/src/gateway/wallets/__tests__/test_fx_transfers.py),
  [`gw/payments/fx/__tests__/test_fx_service.py`](../services/gateway/src/gateway/payments/fx/__tests__/test_fx_service.py),
  [`test_ledger_sequences.py`](../services/gateway/src/gateway/payments/ledger/__tests__/test_ledger_sequences.py).

### D3.6 Card data: tokenize at the edge of the domain, never store the PAN

- **Decision:** `tokenize_card` keeps a random token, BIN, last4 and an HMAC-SHA256 fingerprint keyed by
  `GATEWAY_CARD_FINGERPRINT_KEY`; the PAN is dropped there. Only allowlisted test PANs pass validation (Luhn, ASCII
  digits only). Events and ops-ai carry BIN, last4 and a per-merchant `card_ref` (an HMAC of the fingerprint under a
  derived subkey), never the fingerprint itself.
- **Why:** If the PAN is never written, there is nothing to leak from the database, the stream or the projection.
  A keyed fingerprint cannot be brute-forced back to a PAN from the 10^15-ish PAN space without the key. A per-merchant
  `card_ref` lets ops-ai detect card testing within a merchant without being able to link cards across merchants.
- **What we gave up:** No real card can be charged. The vault is a table in the gateway database, not a separate
  PCI zone (P2-3).
- **Alternatives:** Store an encrypted PAN (keeps the PAN in scope); a plain SHA-256 fingerprint (brute-forceable).
- **Evidence (Tested):** [`test_charge.py`](../services/gateway/src/gateway/payments/__tests__/test_charge.py)
  (`test_charge_card_never_writes_the_full_pan_to_any_gateway_or_simulator_table`),
  [`tests/e2e/test_thin_slice.py`](../tests/e2e/test_thin_slice.py)
  (`test_thin_slice_never_leaves_a_full_pan_in_any_schema_or_on_the_stream`),
  [`gw/payments/tokenization/__tests__/test_cards.py`](../services/gateway/src/gateway/payments/tokenization/__tests__/test_cards.py),
  [`gw/payments/tokenization/__tests__/test_card_ref.py`](../services/gateway/src/gateway/payments/tokenization/__tests__/test_card_ref.py),
  [`tests/test_events_card_ref.py`](../tests/test_events_card_ref.py).

### D3.7 Synchronous rule-based risk scoring; the flag is separate from the status

- **Decision:** At charge time, inside tx1, `assess_charge_risk` scores 0.00–1.00 from three rules: high amount per
  currency (0.40), card velocity ≥ 3 payments in 10 min (0.40), ≥ 2 provider declines in 60 min (0.30). Score ≥ 0.50
  flags; ≥ 0.90 blocks (declined `risk_blocked`, no provider call). In-flight siblings on the same card (state
  `created` within the attempt lease) count as declines. `risk_flagged` is a column separate from the payment state.
- **Why:** No LLM and no network call on the payment path, so an LLM outage cannot weaken scoring. Counting in-flight
  siblings closes the race where a parallel burst all scores before any decline is recorded.
- **What we gave up:** No learned model. The velocity lookup and the database are the same dependency, so the
  `risk_unavailable` / `degraded_scoring` policy of `architecture-detail.md` §3 has no separate failure mode to
  handle and is not implemented.
- **Alternatives:** An ML score service (another dependency on the hot path); an async-only detector (cannot block
  the next charge of a burst).
- **Evidence (Tested):** [`gw/payments/__tests__/test_risk.py`](../services/gateway/src/gateway/payments/__tests__/test_risk.py),
  [`test_risk_in_flight.py`](../services/gateway/src/gateway/payments/__tests__/test_risk_in_flight.py)
  (`test_parallel_high_amount_burst_on_a_declining_card_reaches_the_provider_three_times_and_blocks_the_rest`),
  [`test_charge_risk.py`](../services/gateway/src/gateway/payments/__tests__/test_charge_risk.py),
  [`test_charge_risk_hardening.py`](../services/gateway/src/gateway/payments/__tests__/test_charge_risk_hardening.py).

### D3.8 Refunds and disputes share one cap under one row lock

- **Decision:** Refundable = amount − refunded − disputed (reserved) − chargeback, read and updated under
  `SELECT … FOR UPDATE` on the payment row. A dispute reserves its amount when opened; `won` releases it with no
  journal; `lost` posts the chargeback plus a per-currency chargeback fee. The state machine is
  `opened → under_review → won | lost`. The merchant HTTP API can read a dispute and submit evidence only; opening and
  resolving are service functions (the simulated network or an ops role).
- **Why:** A refund and a chargeback racing on the same payment is the one way to pay out more than was captured.
  One lock and one formula make that impossible.
- **What we gave up:** No consumer-facing dispute or refund request route; no ops route to record an outcome
  (no ops principal exists yet).
- **Alternatives:** Separate counters per flow with a reconciliation job (detects the overpay after it happens).
- **Evidence (Tested):** [`test_refunds.py`](../services/gateway/src/gateway/payments/__tests__/test_refunds.py)
  (`test_refund_payment_concurrent_refunds_with_different_keys_never_exceed_the_payment_amount`,
  `test_refund_payment_concurrent_full_refunds_let_exactly_one_through`),
  [`test_disputes.py`](../services/gateway/src/gateway/payments/__tests__/test_disputes.py)
  (`test_dispute_and_refund_together_never_exceed_the_payment_amount_in_the_ledger`,
  `test_there_is_no_http_route_for_the_merchant_to_open_or_resolve_a_dispute`),
  [`test_b3_hardening.py`](../services/gateway/src/gateway/payments/__tests__/test_b3_hardening.py).

### D3.9 Notifications are derived from the outbox and deduped by source event

- **Decision:** A notifier worker reads payment outbox rows and writes a confirmation for a successful payment and a
  suspicious-activity alert for a flagged or risk-blocked one. The notification id is `uuid5(source event, kind)`, so a
  redelivered event creates no second record. Backfill events are marked processed and create nothing.
- **Why:** At-least-once input with a deterministic id gives at-most-one record without a lock.
- **What we gave up:** No delivery channel (email, push); records are in-app only.
- **Alternatives:** Sending from the request handler (lost on a crash after commit, or sent for a rolled-back payment).
- **Evidence (Tested):** [`gw/notifications/__tests__/test_notifications.py`](../services/gateway/src/gateway/notifications/__tests__/test_notifications.py),
  [`gw/notifications/__tests__/test_worker.py`](../services/gateway/src/gateway/notifications/__tests__/test_worker.py),
  [`tests/test_notification_events.py`](../tests/test_notification_events.py).

---

## 4. Lock strategy

### D4.1 Pessimistic row locks on the rows a decision reads, everything else append-only

- **Decision:** Every read-then-write on money takes `SELECT … FOR UPDATE` on exactly the rows the decision depends
  on, in a fixed order, inside the transaction that writes the result. Rows that are only appended to (postings,
  outbox, audit) take no lock.
- **Why:** Contention is per wallet, per payment or per card, so the locks are narrow and short (no network call is
  made while holding one, D3.2). A version-conflict retry loop (optimistic locking) would add latency and code paths
  for no benefit at this scale.
- **What we gave up:** A hot wallet (one merchant receiving many transfers) serializes on its balance row. Throughput
  per row is bounded by commit latency.
- **Alternatives:** Optimistic concurrency with a version column and retries; `SERIALIZABLE` isolation (retry storms
  under contention); a single-writer queue per account (more moving parts).

Where each lock is, and what proves it:

| Contended resource | Mechanism | Code | Test |
|---|---|---|---|
| User wallet balance (transfer, top-up, payout hold / settle) | `SELECT … FOR UPDATE` on `account_balances` rows, **ascending account id**, balance check and journal in one tx | `gw/wallets/service.py` `lock_balances`, `gw/wallets/payouts.py` | [`test_transfers.py`](../services/gateway/src/gateway/wallets/__tests__/test_transfers.py): `test_parallel_transfers_from_one_wallet_never_overdraw_it_and_the_sums_match`, `test_opposite_direction_transfers_between_the_same_pair_all_complete_without_deadlock` |
| Wallet creation on first receipt | unique constraint + insert-or-get | `gw/wallets/service.py` | [`test_transfers.py`](../services/gateway/src/gateway/wallets/__tests__/test_transfers.py): `test_parallel_ensure_wallet_calls_create_exactly_one_account_and_balance_row` |
| Platform and merchant accounts | **no balance row**; append-only postings, balances aggregated on read | `gw/payments/ledger/service.py` | [`test_ledger_service.py`](../services/gateway/src/gateway/payments/ledger/__tests__/test_ledger_service.py) |
| Payment refundable amount (refund, dispute) | `SELECT … FOR UPDATE` on the payment row | `gw/payments/refunds.py` `lock_payment`, `gw/payments/disputes.py` | [`test_refunds.py`](../services/gateway/src/gateway/payments/__tests__/test_refunds.py), [`test_disputes.py`](../services/gateway/src/gateway/payments/__tests__/test_disputes.py) |
| Idempotency key (charge) | unique `(merchant_id, key)` insert `ON CONFLICT DO NOTHING`; Redis `SET NX PX` as an optional contention filter | `gw/payments/charge.py` `_claim`, `_try_lock` | [`test_charge.py`](../services/gateway/src/gateway/payments/__tests__/test_charge.py): `test_e1_*` |
| Payment finalization | compare-and-set `UPDATE … WHERE state IN ('created','unknown')` | `gw/payments/charge.py` `_finalize` | [`test_charge.py`](../services/gateway/src/gateway/payments/__tests__/test_charge.py): `test_e2_*` |
| Card risk window | `pg_advisory_xact_lock(hashtextextended(fingerprint))` for the tx1 duration | `gw/payments/risk.py` `_card_activity` | [`test_risk_in_flight.py`](../services/gateway/src/gateway/payments/__tests__/test_risk_in_flight.py) |
| Outbox publishing | `FOR UPDATE SKIP LOCKED`, id order | `gw/outbox/relay.py` | [`test_relay.py`](../services/gateway/src/gateway/outbox/__tests__/test_relay.py) |
| Projection row (ops-ai) | `processed_events` unique insert, then `FOR UPDATE` on the `payment_facts` row; highest `entity_version` wins | `ai/ingest/consumer.py` | [`test_consumer.py`](../services/ops-ai/src/ops_ai/ingest/__tests__/test_consumer.py) |
| TOTP enrol / verify / lockout | `FOR UPDATE` on the user and `user_totp` rows | `gw/accounts/mfa.py` | [`test_mfa.py`](../services/gateway/src/gateway/accounts/__tests__/test_mfa.py) |
| argon2 CPU on login | 4 concurrency slots, 2 s wait, then 503 with `Retry-After`; DB session closed before hashing | `gw/app.py` | [`test_user_tokens_concurrency.py`](../services/gateway/src/gateway/__tests__/test_user_tokens_concurrency.py) |

### D4.2 Why the risk lock is an advisory lock, not a row lock

- **Decision:** The per-card serialization uses a transaction-scoped advisory lock on a hash of the fingerprint.
- **Why:** The rows that matter (earlier and in-flight payments on the card) may not exist yet, and a new card has no
  row to lock. An advisory lock serializes on the *key*, held until tx1 commits, so the next charge on the card sees
  the previous one as `created`.
- **What we gave up:** Two different fingerprints whose 64-bit hashes collide also serialize (harmless, rare). Charges
  on one card are serialized through tx1 (short; the provider call is outside it).
- **Alternatives:** A `cards` row with `FOR UPDATE` (would work once the token row exists; adds a write per charge);
  `SERIALIZABLE` on the risk read.

---

## 5. Operations intelligence

### D5.1 Tools first; the LLM only narrates verified evidence

- **Decision:** Each sample query has a deterministic route that computes the answer before any LLM is involved:
  `POST /v1/assistant/investigate` (SQ1), `/investigate-transfer` (SQ2), `/investigate-fraud` (SQ3),
  `/explain-settlement` (SQ4), plus `/ask`. The tools (`aggregate_failures`, `compare_baseline`, `search_incidents`,
  `get_payment_status`, `analyze_fraud`, `explain_settlement`) are read-only SQL over the projection. The route builds
  an evidence list (`[ref] summary`) and only then asks `LlmPort` to narrate it. **There is no tool-calling loop:** the
  narration request carries no tool definitions. Added after the snapshot: `POST /v1/assistant/query`
  (`ai/assistant/tool_assistant.py`) asks a planner once to pick tools from a read-only registry
  (`ai/tools/catalog.py`), validates arguments, runs at most 4 calls in the caller's scope and falls back to a
  rule-based planner when the LLM is unavailable; it is still one planning call, not a loop.
- **Why:** Counts, rates, the rule that fired and settlement arithmetic are exact facts; an LLM should not produce
  them. The narration model has no tools, and the `/query` planner can only choose read-only, scoped tools whose
  arguments are validated, so prompt injection cannot make the model move money or read another tenant. This is a deliberate narrowing of
  `architecture-detail.md` §5 (a hand-rolled tool loop, up to 6 calls), which was not needed while every route
  computes its evidence first.
- **What we gave up:** Free-form questions outside the four routes get only the `/ask` path (keyword search +
  aggregates). The answer-contract checker exists for `/query` and `/failures` (B5.2): `ground_claims` drops claims whose
  refs are not evidence of the turn and `grounded_narration` keeps narration only when every cited `[ref]` is; the
  SQ1–SQ4 routes still only instruct the model to cite `[ref]`s.
- **Alternatives:** An agent loop with function calling (more flexible, harder to bound and to test); templated text
  only (no LLM at all; what T2 already shows).
- **Evidence (Tested):** [`ai/investigation/__tests__/test_investigation_tools.py`](../services/ops-ai/src/ops_ai/investigation/__tests__/test_investigation_tools.py),
  [`test_investigate_golden.py`](../services/ops-ai/src/ops_ai/investigation/__tests__/test_investigate_golden.py),
  [`ai/transfers/__tests__/test_investigate_transfer_golden.py`](../services/ops-ai/src/ops_ai/transfers/__tests__/test_investigate_transfer_golden.py),
  [`ai/fraud_watch/__tests__/test_fraud_golden.py`](../services/ops-ai/src/ops_ai/fraud_watch/__tests__/test_fraud_golden.py),
  [`ai/settlement/__tests__/test_settlement_golden.py`](../services/ops-ai/src/ops_ai/settlement/__tests__/test_settlement_golden.py),
  [`ai/assistant/__tests__/test_openai_llm.py`](../services/ops-ai/src/ops_ai/assistant/__tests__/test_openai_llm.py)
  (`test_complete_sends_exactly_the_four_documented_body_keys_plus_store` — no `tools` key).

### D5.2 Verify before narrating (SQ4)

- **Decision:** `explain_settlement` builds typed line items (gross, MDR, fixed fees, refunds, chargebacks and fees,
  reserve, FX → net) and checks that they sum to the payout. Only a verified statement goes to the LLM; the narrative
  is never the source of a number.
- **Why:** A settlement explanation that does not add up is worse than none.
- **What we gave up:** Settlement is computed in ops-ai from seeded settlement entries, not by a gateway settlement
  batch (the gateway has no settlement module).
- **Alternatives:** Ask the LLM to explain raw rows (unverifiable arithmetic).
- **Evidence (Tested):** [`ai/settlement/__tests__/test_statement.py`](../services/ops-ai/src/ops_ai/settlement/__tests__/test_statement.py),
  [`test_recompute_lines.py`](../services/ops-ai/src/ops_ai/settlement/__tests__/test_recompute_lines.py),
  [`test_explain_payout.py`](../services/ops-ai/src/ops_ai/settlement/__tests__/test_explain_payout.py),
  [`test_settlement_constraints.py`](../services/ops-ai/src/ops_ai/settlement/__tests__/test_settlement_constraints.py).

### D5.3 SQ2 investigates a transfer recorded by an external system

- **Decision:** The declined international transfer of SQ2 is a record in ops-ai (`ai/transfers/`) carrying amount and
  currency, the applied limit, the running total at decision time, the decline code, the rule id and the sender
  balance. The tool classifies "insufficient funds" versus "KYC cross-border limit" from those fields.
- **Why:** The platform does not execute cross-border transfers or make KYC decisions (`submission-outline.md` §3); it
  explains them.
- **What we gave up:** No corridor, KYC tier or sanctions checks in the gateway's own transfer path.
- **Alternatives:** Build KYC tiers into gateway transfers (out of scope for the time available).
- **Evidence (Tested):** [`ai/transfers/__tests__/test_classify_transfer.py`](../services/ops-ai/src/ops_ai/transfers/__tests__/test_classify_transfer.py),
  [`test_get_payment_status.py`](../services/ops-ai/src/ops_ai/transfers/__tests__/test_get_payment_status.py),
  [`test_investigate_transfer.py`](../services/ops-ai/src/ops_ai/transfers/__tests__/test_investigate_transfer.py).

### D5.4 Card testing is detected twice: block in the gateway, explain in ops-ai

- **Decision:** The gateway blocks a burst in real time with the risk rules (D3.7). ops-ai's `fraud_watch` runs on
  each projected payment and finds micro-payment episodes per merchant (distinct cards, small-amount share, span),
  rules only; `analyze_fraud` reports the episode with its features. Ticket routing sends card testing to the `fraud`
  queue at high priority by a deterministic rule.
- **Why:** The block must be synchronous; the explanation needs a window of history and can lag.
- **What we gave up:** Threshold tuning from human verdicts exists only as the R2-7 feedback loop (false positives
  tighten a versioned per-merchant rule that the live watch and the SQ3 assistant apply); the
  measured false-positive rate is on a synthetic labelled set, not production traffic.
- **Alternatives:** One detector in the gateway only (no explanation); LLM-based detection (non-deterministic, and on
  the wrong side of the LLM-outage boundary).
- **Evidence (Tested):** [`ai/fraud_watch/__tests__/test_analyze_fraud.py`](../services/ops-ai/src/ops_ai/fraud_watch/__tests__/test_analyze_fraud.py),
  [`test_watch_payment.py`](../services/ops-ai/src/ops_ai/fraud_watch/__tests__/test_watch_payment.py),
  [`test_fraud_golden.py`](../services/ops-ai/src/ops_ai/fraud_watch/__tests__/test_fraud_golden.py),
  [`ai/tickets/__tests__/test_route_ticket.py`](../services/ops-ai/src/ops_ai/tickets/__tests__/test_route_ticket.py).

### D5.5 LLM outage falls back to an evidence-only answer (T2)

- **Decision:** With no `OPENAI_API_KEY`, ops-ai uses `MockLlm`. With a key, `OpenAiLlm` calls the Responses API with
  a 20 s wall-clock budget and at most two requests: one to the pinned chat model and, on a retryable failure only, one
  to the pinned fallback model. If both fail, the route returns 200 with `mode == "t2_fallback"`, `text: null` and the
  **same evidence** as the healthy path. No database connection is held during the LLM call.
- **Why:** The ops user still gets the facts when the LLM is down, and the payment path never waits on it.
- **What we gave up:** No per-merchant daily token budget, no circuit breaker (P1-8 remainder, P1-3). A T2 answer has
  no prose.
- **Alternatives:** Return 503 when the LLM is down (hides evidence that was already computed).
- **Evidence (Tested):** [`ai/assistant/__tests__/test_assistant_service.py`](../services/ops-ai/src/ops_ai/assistant/__tests__/test_assistant_service.py)
  (`test_e3_llm_unavailable_falls_back_to_t2_with_the_same_evidence_as_the_healthy_path`),
  [`tests/e2e/test_thin_slice.py`](../tests/e2e/test_thin_slice.py)
  (`test_e3_llm_down_keeps_charges_succeeding_and_serves_the_same_evidence`),
  [`ai/__tests__/test_main_openai.py`](../services/ops-ai/src/ops_ai/__tests__/test_main_openai.py),
  [`ai/__tests__/test_llm_session_release.py`](../services/ops-ai/src/ops_ai/__tests__/test_llm_session_release.py),
  [`ai/assistant/__tests__/test_openai_llm_hardening.py`](../services/ops-ai/src/ops_ai/assistant/__tests__/test_openai_llm_hardening.py).
  Without a key the adapter is tested only against a local fake HTTP server; that is not a test of the real API.

### D5.6 Hybrid search: Postgres full-text plus pgvector, fused by RRF

- **Decision:** Keyword search uses a persisted `tsvector` column and `websearch_to_tsquery('english', …)`; the vector
  leg is pgvector similarity with one column and HNSW index per embedder id; `hybrid_search` fuses both by Reciprocal
  Rank Fusion (k = 60). Both legs are restricted to the caller's merchant. `/v1/assistant/ask` retrieves through
  `hybrid_search`; `POST /v1/incidents/search` reranks hybrid incident hits (R2-4), reading indexed incidents through
  their stored vectors (only the query is embedded). Incidents are indexed into
  `search_documents` (`kind = 'incident'`, `ai/search/indexing.py`, run by `python -m ops_ai.seed` and the eval
  suite); platform-wide incidents are owned by the nil-UUID sentinel and are visible only through the opt-in
  `include_platform` filter, which admits incident rows only. The SQ1 tool `search_incidents` uses `hybrid_search`
  when the app has an embedder (`POST /v1/assistant/investigate` passes `deps.embedder`): every hit is re-checked
  against `ops_ai.incidents` visibility (the merchant's or platform-wide), full-text hits not yet indexed are
  appended, resolved incidents rank first. Without an embedder it is the full-text search over `ops_ai.incidents`.
- **Why:** It runs inside the existing Postgres; per-embedder columns mean changing the model never mixes vector
  spaces; the incidents table stays the authority, so a stale search document never widens visibility.
- **What we gave up:** The default local embedder is feature hashing (spelling-level, not semantic); the OpenAI
  column is filled by a backfill only. `POST /v1/assistant/failures` and the LLM tool catalog still call
  `search_incidents` without an embedder (full-text). Hit-to-incident mapping scans the scope's visible incidents in
  Python. Incidents are indexed at seed / eval time, not on write. `/v1/assistant/ask` (no kind filter) now also
  returns the merchant's own incident documents, cited as `incident:<uuid>` refs (platform-wide incidents are not
  included there). Japanese text (pg_bigm) is design only.
- **Alternatives:** A separate vector database (a second store to keep consistent and scoped).
- **Evidence (Tested):** [`ai/search/__tests__/test_search_service.py`](../services/ops-ai/src/ops_ai/search/__tests__/test_search_service.py),
  [`test_hybrid_search.py`](../services/ops-ai/src/ops_ai/search/__tests__/test_hybrid_search.py),
  [`ai/assistant/__tests__/test_assistant_hybrid.py`](../services/ops-ai/src/ops_ai/assistant/__tests__/test_assistant_hybrid.py),
  [`ai/search/__tests__/test_incident_indexing.py`](../services/ops-ai/src/ops_ai/search/__tests__/test_incident_indexing.py),
  [`ai/investigation/__tests__/test_search_incidents_hybrid.py`](../services/ops-ai/src/ops_ai/investigation/__tests__/test_search_incidents_hybrid.py),
  [`ai/investigation/__tests__/test_investigate_hybrid_golden.py`](../services/ops-ai/src/ops_ai/investigation/__tests__/test_investigate_hybrid_golden.py),
  [`ai/__tests__/test_investigate_route_hybrid.py`](../services/ops-ai/src/ops_ai/__tests__/test_investigate_route_hybrid.py),
  [`ai/investigation/__tests__/test_investigation_tools.py`](../services/ops-ai/src/ops_ai/investigation/__tests__/test_investigation_tools.py).

### D5.7 AI model selection

- **Decision:** The LLM is only a narrator (D5.1): it turns evidence that tools already computed into prose. It never
  decides, changes or blocks a payment; the gateway's risk scoring and every status change are rule-based and run
  without it (D3.7, D5.4). Within that role:
  - **Chat:** `gpt-4.1-mini-2025-04-14` on the OpenAI Responses API, pinned to a dated snapshot.
  - **Fallback:** `gpt-4.1-nano-2025-04-14`, tried once, only after a retryable failure of the primary (HTTP 404,
    408, 409, 429, 5xx, timeout, connection error) and only inside the same 20 s budget. A non-retryable failure
    (4xx other than those, invalid or incomplete body, response over 64 KiB) goes straight to the evidence-only
    answer (D5.5). Output is capped at 600 tokens, at most 8 calls run at once, and `store` is false.
  - **Embeddings:** `text-embedding-3-small` for the OpenAI vector column, filled by the backfill CLI
    (`python -m ops_ai.search`) and used for query embeddings only when `OPS_AI_EMBEDDER=openai` and a key are both
    set. The default (`config.py`, `embedder = "local"`) is `LocalHashEmbedder` (feature hashing, spelling-level, no
    network); compose does not pass `OPS_AI_EMBEDDER`, so the demo always uses it.
  - **No key:** `MockLlm` and `LocalHashEmbedder`. The whole stack, the demo and the eval suite then run offline.
  Both model names can be overridden with `OPENAI_CHAT_MODEL` / `OPENAI_FALLBACK_MODEL`.
- **Why:** Narrating a few hundred tokens of cited evidence needs instruction following and citation discipline, not
  deep reasoning, so a small model gives the needed quality at low latency and cost. A dated snapshot keeps behaviour
  from changing under the eval suite. The fallback is a smaller model from the same family and API, so the prompt and
  parser are unchanged and an overloaded primary still gets an answer. `text-embedding-3-small` is the low-cost current
  OpenAI embedding model, and each embedder has its own column (D5.6), so changing it never mixes vector
  spaces.
- **What we gave up:** The eval suite and CI run on `MockLlm`, so they prove that the pipeline is reproducible and
  that the evidence, refs and fallbacks are right; they do not measure the real model's prose quality. The real
  adapter is tested only against a local fake HTTP server. There is no per-merchant token budget or circuit breaker
  (D5.5), no self-hosted model, and no fallback to a different provider. Data sent to OpenAI is redacted and fenced
  (D6.4) but still leaves the platform; a deployment that cannot allow that runs with no key.
- **Alternatives:** A larger reasoning model (slower and costlier for narration); a self-hosted open model (no GPU in
  the local stack, and a second runtime to operate); letting the model call tools freely and decide (rejected: an
  LLM must not sit on the payment decision path).
- **Evidence (Tested):** [`ai/assistant/__tests__/test_openai_llm_hardening.py`](../services/ops-ai/src/ops_ai/assistant/__tests__/test_openai_llm_hardening.py),
  [`ai/__tests__/test_main_openai.py`](../services/ops-ai/src/ops_ai/__tests__/test_main_openai.py), and the D5.5
  fallback tests. Model constants: `ai/assistant/openai_llm.py`, `ai/search/openai_embedder.py`.

---

## 6. Security

### D6.1 Passwords: argon2id with bounded verification concurrency

- **Decision:** Passwords are hashed with argon2id (`argon2-cffi` defaults). Unknown email and wrong password take the
  same argon2 path. At most 4 verifications run at once; a request that cannot get a slot within 2 s gets 503 with
  `Retry-After` and no password check. The DB session is closed before hashing.
- **Why:** argon2id is memory-hard. Bounding concurrency stops a login flood from turning argon2's cost into a
  denial of service on the payment API, and closing the session stops it from draining the connection pool.
- **What we gave up:** Under a login flood, legitimate logins get 503. No per-account or per-IP rate limit (P1-4).
- **Alternatives:** bcrypt (no memory hardness); unbounded verification (CPU and pool exhaustion).
- **Evidence (Tested):** [`gw/accounts/__tests__/test_accounts_service.py`](../services/gateway/src/gateway/accounts/__tests__/test_accounts_service.py),
  [`gw/__tests__/test_user_tokens_concurrency.py`](../services/gateway/src/gateway/__tests__/test_user_tokens_concurrency.py),
  [`gw/__tests__/test_user_tokens_route.py`](../services/gateway/src/gateway/__tests__/test_user_tokens_route.py).

### D6.2 TOTP MFA with the secret sealed by AES-256-GCM

- **Decision:** TOTP enrolment returns an `otpauth://` URI; the secret is stored encrypted with AES-256-GCM under
  `TOTP_ENCRYPTION_KEY` (from the environment), random 12-byte nonce, **user id as AAD** so a blob only decrypts for its
  own user. MFA is enabled only after a confirming code. After 5 failures the account's TOTP locks for 15 minutes.
  When MFA is enabled, both session login and `/v1/user-tokens` require a valid code; the TOTP flag is read after the
  argon2 check so an enrolment confirmed during the check is not missed. Without an encryption key configured, MFA
  routes return 503 instead of storing plaintext.
- **Why:** A database dump alone does not reveal TOTP secrets, and a blob copied to another user's row does not
  decrypt.
- **What we gave up:** One environment key for all users (no per-user key, no KMS envelope encryption; see P2-3,
  P2-4). No recovery codes.
- **Alternatives:** Plaintext secrets (common, unacceptable here); WebAuthn (stronger, more client work).
- **Evidence (Tested):** [`gw/accounts/__tests__/test_mfa.py`](../services/gateway/src/gateway/accounts/__tests__/test_mfa.py)
  (`test_decrypt_with_another_users_id_raises_totp_decrypt_error`,
  `test_encrypted_blob_never_contains_the_plaintext_secret`),
  [`gw/__tests__/test_user_tokens_totp.py`](../services/gateway/src/gateway/__tests__/test_user_tokens_totp.py),
  [`gw/accounts/__tests__/test_login_split.py`](../services/gateway/src/gateway/accounts/__tests__/test_login_split.py).

### D6.3 Secrets come from the environment, are length-checked, and never reach logs or errors

- **Decision:** Every key (API-key pepper, fingerprint key, TOTP key, JWT private key, OpenAI key) is read from the
  environment or a mounted file into `SecretStr`. Symmetric keys shorter than 32 characters are rejected at startup
  without echoing the value. The SQLAlchemy engine hides bound parameters in errors. Merchant API keys are stored as
  peppered hashes. `make demo` generates local secrets once (0600) and prints only file names. The OpenAI key appears
  only in the `Authorization` header; adapter errors are short and raised `from None`.
- **Why:** The common leak paths are logs, exception messages and `repr()`; each is closed by construction and
  tested.
- **What we gave up:** No secret manager in the demo; rotation is manual. No full-E2E log canary run (P0-11 partial).
- **Alternatives:** `.env` files committed with defaults (rejected outright).
- **Evidence (Tested):** [`gw/__tests__/test_config.py`](../services/gateway/src/gateway/__tests__/test_config.py),
  [`gw/__tests__/test_db.py`](../services/gateway/src/gateway/__tests__/test_db.py),
  [`scripts/__tests__/test_demo_secrets.py`](../scripts/__tests__/test_demo_secrets.py),
  [`ai/__tests__/test_main_openai.py`](../services/ops-ai/src/ops_ai/__tests__/test_main_openai.py)
  (`test_the_canary_key_never_appears_in_a_successful_response_or_in_log_records`),
  [`gw/accounts/__tests__/test_merchant_id_and_keys.py`](../services/gateway/src/gateway/accounts/__tests__/test_merchant_id_and_keys.py).

### D6.4 Mask card numbers and fence the evidence before the LLM

- **Decision:** The only data the LLM receives is the question and the evidence summaries, which are built from
  projection fields that carry BIN and last4, never a PAN. As a second line, the adapter masks any Luhn-valid digit
  run in the question and the summaries, including runs split by whitespace or markup, and caps raw field length
  before rendering. The fixed `instructions` tell the model to treat the evidence block as data, never as
  instructions.
- **Why:** Evidence is built from data that already excludes PANs; masking catches a PAN a user pastes into the
  question or a free-text field.
- **What we gave up:** Masking covers card numbers only. Email, phone and name redaction (P0-6 as planned) are not
  built; the evidence DTO is narrow but not formally allowlisted per field.
- **Alternatives:** Send raw rows and rely on the provider's data handling (unacceptable for card data).
- **Evidence (Tested, card numbers):** [`test_openai_llm_hardening.py`](../services/ops-ai/src/ops_ai/assistant/__tests__/test_openai_llm_hardening.py)
  (`test_complete_masks_a_luhn_valid_card_number_in_the_question`),
  [`test_openai_llm_redteam.py`](../services/ops-ai/src/ops_ai/assistant/__tests__/test_openai_llm_redteam.py)
  (`test_complete_never_sends_a_masked_card_number_in_another_layout_to_the_server`),
  [`test_openai_llm_redteam_order.py`](../services/ops-ai/src/ops_ai/assistant/__tests__/test_openai_llm_redteam_order.py),
  [`test_openai_llm.py`](../services/ops-ai/src/ops_ai/assistant/__tests__/test_openai_llm.py)
  (`test_complete_instructions_treat_the_evidence_block_as_data_not_instructions`).

### D6.5 Tenant scope is enforced in every query, and probed across tenants

- **Decision:** Every ops-ai tool and repository function takes a scope built from the verified token. Gateway merchant
  routes resolve the merchant from the API key and return 404 for another merchant's payment or dispute (no existence
  oracle). The projection refuses an event that would move a payment to a different merchant.
- **Why:** Cross-tenant leakage is the worst failure an ops assistant can have.
- **What we gave up:** No Postgres row-level security; enforcement is in application code and tests. A `role × action`
  policy table with default deny (`gw/accounts/rbac.py`) and a generated route × role matrix test cover the gateway,
  and ops principals (`ops_agent`, `ops_lead`) exist; P0-9 stays Partial (later routes reuse existing action names,
  the interim `X-Ops-Admin-Token` is still accepted for ops dispute resolution, ops roles read every merchant).
- **Alternatives:** Postgres RLS with `SET LOCAL` per transaction and a non-owner role (stronger; design only).
- **Evidence (Tested):** [`tests/e2e/test_runtime_contract.py`](../tests/e2e/test_runtime_contract.py),
  [`ai/assistant/__tests__/test_assistant_service.py`](../services/ops-ai/src/ops_ai/assistant/__tests__/test_assistant_service.py)
  (`test_ask_evidence_never_refers_to_another_merchants_payment`),
  [`ai/investigation/__tests__/test_investigation_tools.py`](../services/ops-ai/src/ops_ai/investigation/__tests__/test_investigation_tools.py)
  (`test_aggregate_failures_never_counts_another_merchants_payments`),
  [`test_refunds.py`](../services/gateway/src/gateway/payments/__tests__/test_refunds.py)
  (`test_refund_payment_for_another_merchants_payment_returns_404_and_changes_nothing`),
  [`ai/ingest/__tests__/test_consumer.py`](../services/ops-ai/src/ops_ai/ingest/__tests__/test_consumer.py)
  (`test_apply_payment_event_ignores_a_higher_version_event_from_a_different_merchant`).

### D6.6 Append-only audit written in the same transaction

- **Decision:** `audit_log` rows record actor, role, action, target, a summary, a correlation id and the time, and are
  written in the same transaction as the operation (charge, refund, transfer, dispute changes, TOTP changes, failed
  user-token passwords). A `BEFORE UPDATE OR DELETE` trigger raises `insufficient_privilege`.
- **Why:** If the audit write fails, the operation rolls back, so there is no unaudited money movement.
- **What we gave up:** The gateway API and gRPC server `SET ROLE gateway_app`, which has no UPDATE, DELETE or
  TRUNCATE on `audit_log`, but the compose demo logs in as a superuser, so code with SQL access can `RESET ROLE`; the
  relay and notifier do not use the role. There is no hash chain. We claim "the application does not update or
  delete audit rows", not tamper detection.
- **Alternatives:** A per-row hash chain with a verifier; an external WORM checkpoint (P2-8).
- **Evidence:** Same-transaction audit rows are **Tested** in
  [`test_charge.py`](../services/gateway/src/gateway/payments/__tests__/test_charge.py)
  (`test_charge_card_approve_writes_one_audit_row_and_stores_the_response_on_the_idempotency_row`),
  [`test_refunds.py`](../services/gateway/src/gateway/payments/__tests__/test_refunds.py),
  [`test_disputes.py`](../services/gateway/src/gateway/payments/__tests__/test_disputes.py),
  [`test_transfers.py`](../services/gateway/src/gateway/wallets/__tests__/test_transfers.py)
  (`test_a_created_transfer_writes_one_audit_row_without_any_email`),
  [`test_mfa.py`](../services/gateway/src/gateway/accounts/__tests__/test_mfa.py). The trigger and the
  `gateway_app` grants are **Tested** in
  [`test_audit_append_only.py`](../services/gateway/src/gateway/audit/__tests__/test_audit_append_only.py)
  (UPDATE/DELETE/TRUNCATE rejected as `gateway_app`, rollback when the audit insert fails).

### D6.7 Unknown country is treated as cross-border (R2-10)

- **Decision:** In `screen_operation` (`gw/compliance/kyc.py`) a transfer is cross-border when it has a live recipient
  (a user who is not erased and not the sender) and either country is unknown or the two countries differ. A blocked
  operation writes an audit row with `sender_country_unknown` / `recipient_country_unknown` and returns the generic
  403 `{"error":"kyc_reverification_required"}`.
- **Why:** Fail-closed. A missing self-declared country (never set, or nulled by GDPR erasure) must not let a
  transfer skip the stricter cross-border KYC gate.
- **What we gave up:** Users without a country cannot send P2P transfers until they set one or are verified. The
  country is still self-declared, so a user can claim the recipient's country; the real fix is a country taken from
  the verified KYC document.
- **Alternatives:** Treat unknown as domestic (fail-open, rejected); block only the unknown-recipient side.
- **Evidence:** [`test_kyc_unknown_country.py`](../services/gateway/src/gateway/compliance/__tests__/test_kyc_unknown_country.py).

### D6.8 Rule changes record which API key acted (R2-7)

- **Decision:** Merchant tokens carry an `api_key_id` claim, validated as a canonical UUID and rejected on user and
  ops tokens. ops-ai records each fraud-rule version change with `actor_kind` and `actor_id` (migration
  `f3c8a1d6e9b4`, check constraints), read only within the token's tenant.
- **Why:** A merchant principal plus role says *which merchant* changed a rule, not *which credential*. The key id makes
  a change traceable to one issued key and revocable.
- **What we gave up:** The actor is an API key, not a named person; a per-user `sub` on merchant tokens would need a
  merchant user login, which is not built. Legacy rows have no actor.
- **Alternatives:** Require a human user id for every rule change (forbids API-key-only automation).
- **Evidence:** [`test_rule_actor_identity.py`](../services/ops-ai/src/ops_ai/feedback/__tests__/test_rule_actor_identity.py).

---

## 7. Web client

### D7.1 A static SPA on the same origin, credentials in memory only

- **Decision:** One Vite + React + TypeScript SPA (no SSR). An nginx `web` service serves the bundle and proxies
  `/api/gw` and `/api/ops`, so there is no CORS. Tokens and the merchant API key live in a module-level in-memory store,
  never in `localStorage`, cookies or the URL; a 401 clears it. nginx sets a strict CSP (`script-src 'self'`, no
  inline), `nosniff`, `no-referrer`, `no-store` on `/api/*`. Each confirmed money action uses one
  `crypto.randomUUID()` Idempotency-Key, reused for retries of that submission. Amounts are integer minor units; input
  is parsed without floating point.
- **Why:** No server runtime ever sees a token; same origin removes a whole class of CORS misconfiguration; the
  idempotency key from the client makes a double-click or a retry safe end to end.
- **What we gave up:** Reload logs the user out. The merchant screen accepts the long-lived demo API key in the
  browser (acceptable only for the seeded demo merchant; production is a merchant-user login with TOTP issuing a
  short-lived token). No card entry in the browser (PAN would bring the page into PCI scope; production uses hosted
  fields from the vault origin). No ops console (no ops principal).
- **Alternatives:** Next.js (nothing here needs SSR); tokens in `httpOnly` cookies (needs CSRF handling and a BFF).
- **Evidence (Tested):** [`web/src/lib/__tests__/authStore.test.ts`](../web/src/lib/__tests__/authStore.test.ts),
  [`web/src/lib/__tests__/money.test.ts`](../web/src/lib/__tests__/money.test.ts),
  [`web/src/lib/__tests__/pendingWrites.test.ts`](../web/src/lib/__tests__/pendingWrites.test.ts),
  [`web/src/components/__tests__/ConfirmButton.test.tsx`](../web/src/components/__tests__/ConfirmButton.test.tsx),
  [`web/src/features/user/__tests__/TransferPanel.test.tsx`](../web/src/features/user/__tests__/TransferPanel.test.tsx),
  [`web/src/features/merchant/__tests__/MerchantArea.test.tsx`](../web/src/features/merchant/__tests__/MerchantArea.test.tsx).
  Spec: `docs/specs/web.md`.

---

## 8. P0 evidence map

Status vocabulary as at the top of this document; statuses match `docs/requirements-traceability.md` (P0 section) on `main` as of 2026-11-05.

| P0 | Item | Status | What is proved | Test files |
|---|---|---|---|---|
| P0-1 | Idempotency: DB unique + Redis | **Partial** | 20 concurrent same-key charges → 1 provider charge, with and without Redis; key reuse → 422; in flight → 409; unknown → stored 202 replay. Not run: Redis flushed or lock expired mid-flight | [`gw/payments/__tests__/test_charge.py`](../services/gateway/src/gateway/payments/__tests__/test_charge.py), [`gw/wallets/__tests__/test_transfers.py`](../services/gateway/src/gateway/wallets/__tests__/test_transfers.py), [`gw/payments/__tests__/test_refunds.py`](../services/gateway/src/gateway/payments/__tests__/test_refunds.py) |
| P0-2 | Balanced ledger | **Partial** | Hypothesis: any balanced multi-currency journal is accepted, any residual rejected; random sequences of top-ups, transfers (incl. FX), card charges, partial refunds and payout holds keep every journal balanced and wallet balances equal to postings. Not built: chargebacks in the generated sequences; DB-level invariant | [`gw/payments/ledger/__tests__/test_ledger_service.py`](../services/gateway/src/gateway/payments/ledger/__tests__/test_ledger_service.py), [`test_refunds.py`](../services/gateway/src/gateway/payments/__tests__/test_refunds.py), [`test_disputes.py`](../services/gateway/src/gateway/payments/__tests__/test_disputes.py), [`test_transfers.py`](../services/gateway/src/gateway/wallets/__tests__/test_transfers.py), [`test_ledger_sequences.py`](../services/gateway/src/gateway/payments/ledger/__tests__/test_ledger_sequences.py) |
| P0-3 | Balance locking | **Tested** | Parallel debits never overdraw; opposite-direction transfers do not deadlock; concurrent refunds never exceed the amount; rationale in section 4 | [`test_transfers.py`](../services/gateway/src/gateway/wallets/__tests__/test_transfers.py), [`test_refunds.py`](../services/gateway/src/gateway/payments/__tests__/test_refunds.py), [`test_disputes.py`](../services/gateway/src/gateway/payments/__tests__/test_disputes.py), [`test_risk_in_flight.py`](../services/gateway/src/gateway/payments/__tests__/test_risk_in_flight.py) |
| P0-4 | Crash safety | **Partial** | See crash matrix below. C2, C3, C4 tested with injected faults; C1, C5 need the reconciler (not built); no real process kill | [`test_charge.py`](../services/gateway/src/gateway/payments/__tests__/test_charge.py), [`gw/outbox/__tests__/test_relay.py`](../services/gateway/src/gateway/outbox/__tests__/test_relay.py), [`ai/ingest/__tests__/test_consumer.py`](../services/ops-ai/src/ops_ai/ingest/__tests__/test_consumer.py), [`ai/ingest/__tests__/test_worker.py`](../services/ops-ai/src/ops_ai/ingest/__tests__/test_worker.py) |
| P0-5 | LLM outage isolation | **Partial** | LLM down: charges still succeed; risk scoring has no LLM; assistant returns T2 with the same evidence. Not built: one test running charges, risk flags and the card-testing alert with `LlmPort` raising | [`tests/e2e/test_thin_slice.py`](../tests/e2e/test_thin_slice.py), [`ai/assistant/__tests__/test_assistant_service.py`](../services/ops-ai/src/ops_ai/assistant/__tests__/test_assistant_service.py), [`ai/__tests__/test_main_openai.py`](../services/ops-ai/src/ops_ai/__tests__/test_main_openai.py), [`ai/__tests__/test_llm_redteam_routes.py`](../services/ops-ai/src/ops_ai/__tests__/test_llm_redteam_routes.py), [`gw/payments/__tests__/test_risk.py`](../services/gateway/src/gateway/payments/__tests__/test_risk.py) |
| P0-6 | PII / card masking before LLM | **Partial** | Card numbers (incl. split layouts) masked in every LLM payload; evidence carries BIN/last4 only. Not built: email/phone/name redaction, per-field allowlist DTO | [`test_openai_llm_hardening.py`](../services/ops-ai/src/ops_ai/assistant/__tests__/test_openai_llm_hardening.py), [`test_openai_llm_redteam.py`](../services/ops-ai/src/ops_ai/assistant/__tests__/test_openai_llm_redteam.py), [`test_openai_llm_redteam_order.py`](../services/ops-ai/src/ops_ai/assistant/__tests__/test_openai_llm_redteam_order.py) |
| P0-7 | Read-only tools, human approval for money | **Partial** | The narration LLM gets no tools (request body has no `tools` key); `POST /v1/assistant/query` offers the planner only the read-only `merchant_tools()` / `user_tools()` registry (schema-validated, scoped, at most 4 calls); no ops-ai route moves money; refunds exist only on the gateway API with an Idempotency-Key, and the web client needs an explicit confirmation. Not built: `suggestion_id` flow | [`test_openai_llm.py`](../services/ops-ai/src/ops_ai/assistant/__tests__/test_openai_llm.py), [`test_refunds.py`](../services/gateway/src/gateway/payments/__tests__/test_refunds.py), [`web/src/components/__tests__/ConfirmButton.test.tsx`](../web/src/components/__tests__/ConfirmButton.test.tsx), [`web/src/features/merchant/__tests__/PaymentDetail.test.tsx`](../web/src/features/merchant/__tests__/PaymentDetail.test.tsx) |
| P0-8 | Prompt-injection defense | **Partial** | Structural: no tools, scope from token, evidence fenced as data in fixed instructions, input length caps, pathological-input time bounds. Not built: injection fixtures in incidents/memory, answer-contract checker | [`test_openai_llm.py`](../services/ops-ai/src/ops_ai/assistant/__tests__/test_openai_llm.py), [`test_openai_llm_redteam.py`](../services/ops-ai/src/ops_ai/assistant/__tests__/test_openai_llm_redteam.py), [`tests/e2e/test_runtime_contract.py`](../tests/e2e/test_runtime_contract.py) |
| P0-9 | RBAC | **Partial** | `role × action` policy table with default deny, five roles, ops principals (`ops_agent`, `ops_lead`), generated route × role matrix test; consumer ownership and merchant tenancy tested. Open: later routes reuse existing action names; interim `X-Ops-Admin-Token` still accepted for ops dispute resolution | [`tests/e2e/test_runtime_contract.py`](../tests/e2e/test_runtime_contract.py), [`gw/accounts/__tests__/test_tokens.py`](../services/gateway/src/gateway/accounts/__tests__/test_tokens.py), [`gw/accounts/__tests__/test_b1_routes.py`](../services/gateway/src/gateway/accounts/__tests__/test_b1_routes.py), [`test_refunds.py`](../services/gateway/src/gateway/payments/__tests__/test_refunds.py), [`test_disputes.py`](../services/gateway/src/gateway/payments/__tests__/test_disputes.py), [`gw/__tests__/test_rbac_matrix.py`](../services/gateway/src/gateway/__tests__/test_rbac_matrix.py) |
| P0-10 | Append-only audit | **Partial** | Audit row in the same tx as charge, refund, transfer, dispute, MFA and KYC/GDPR changes; trigger and `gateway_app` grants reject UPDATE/DELETE/TRUNCATE; rollback when the audit insert fails. Open: demo logs in as superuser and only `SET ROLE`s; no hash chain | [`test_charge.py`](../services/gateway/src/gateway/payments/__tests__/test_charge.py), [`test_transfers.py`](../services/gateway/src/gateway/wallets/__tests__/test_transfers.py), [`test_mfa.py`](../services/gateway/src/gateway/accounts/__tests__/test_mfa.py), [`test_disputes.py`](../services/gateway/src/gateway/payments/__tests__/test_disputes.py), [`test_audit_append_only.py`](../services/gateway/src/gateway/audit/__tests__/test_audit_append_only.py) |
| P0-11 | No secrets / PII in logs | **Partial** | Canary values through signup/login/payment → relay → consumer → assistant never appear in in-process Python logs; SQL parameters hidden; secret generator prints no secret. Not built: captured container logs of every service | [`ai/__tests__/test_main_openai.py`](../services/ops-ai/src/ops_ai/__tests__/test_main_openai.py), [`gw/__tests__/test_db.py`](../services/gateway/src/gateway/__tests__/test_db.py), [`scripts/__tests__/test_demo_secrets.py`](../scripts/__tests__/test_demo_secrets.py), [`ai/ingest/__tests__/test_consumer_dead_letter.py`](../services/ops-ai/src/ops_ai/ingest/__tests__/test_consumer_dead_letter.py), [`ai/ingest/__tests__/test_worker.py`](../services/ops-ai/src/ops_ai/ingest/__tests__/test_worker.py), [`tests/test_log_canary.py`](../tests/test_log_canary.py) |
| P0-12 | Correlation id + OTel traces | **Partial** | One W3C trace id from the API span through the outbox header and stream to the consumer span; the LLM span joins it. Not built: tool spans, production exporter | [`tests/e2e/test_trace_propagation.py`](../tests/e2e/test_trace_propagation.py) |
| P0-13 | Metrics | **Partial** | `/metrics` on both APIs: HTTP rate/errors/latency, payment events, provider calls, circuit state, fraud flags, consumer lag, LLM latency/fallback and estimated tokens. Not built: LLM cost and eval score metrics | [`gw/observability/__tests__/test_metrics_http.py`](../services/gateway/src/gateway/observability/__tests__/test_metrics_http.py), [`ai/observability/__tests__/test_llm_instrumentation.py`](../services/ops-ai/src/ops_ai/observability/__tests__/test_llm_instrumentation.py) |
| P0-14 | Alert rules | **Partial** | `ops/prometheus/alerts.yml` (card failure spike, provider errors/circuit, outbox/consumer lag, LLM fallback, limiter fail-open, scrape down) with promtool fixtures via `make alerts-test` (outside `make ci`). Not built: reconciler-unknown rule, external paging | `ops/prometheus/alerts.test.yml` |
| P0-15 | CI with eval gate | **Partial** | `.github/workflows/ci.yml` runs `make ci`: ruff, ruff format, mypy, Alembic upgrade + downgrade per service, pytest, `make eval` (SQ1–SQ4 goldens, in-repo custom metrics, cross-tenant leakage gate), web `npm run ci`. Not built: LLM-judged metrics run with a key in CI | `.github/workflows/ci.yml`, `Makefile`, [`test_investigate_golden.py`](../services/ops-ai/src/ops_ai/investigation/__tests__/test_investigate_golden.py), [`test_investigate_transfer_golden.py`](../services/ops-ai/src/ops_ai/transfers/__tests__/test_investigate_transfer_golden.py), [`test_fraud_golden.py`](../services/ops-ai/src/ops_ai/fraud_watch/__tests__/test_fraud_golden.py), [`test_settlement_golden.py`](../services/ops-ai/src/ops_ai/settlement/__tests__/test_settlement_golden.py), [`test_eval_suite.py`](../services/ops-ai/src/ops_ai/evaluation/__tests__/test_eval_suite.py) |

### Crash matrix (P0-4)

| # | Failure point | Status | Evidence |
|---|---|---|---|
| C1 | Provider succeeded, gateway dies before tx2 | **Design only** (needs the reconciler, D3.3). Today the attempt stays `created`; the provider's own ledger keyed by attempt id means a lookup would find it, but nothing performs the lookup | — |
| C2 | tx2 committed, gateway dies before the HTTP response | **Tested** (injected fault after commit) | `test_e2_crash_after_tx2_commit_then_retry_returns_the_stored_body_without_a_second_charge` in [`test_charge.py`](../services/gateway/src/gateway/payments/__tests__/test_charge.py) |
| C3 | Relay dies after `XADD`, before marking published | **Tested** for the consequence: a redelivered event changes the projection once | `test_consume_payments_redelivery_of_the_same_event_changes_nothing` in [`test_consumer.py`](../services/ops-ai/src/ops_ai/ingest/__tests__/test_consumer.py) |
| C4 | Consumer dies after projection commit, before `XACK` | **Tested** for the consequence: the pending entry is re-read and is a no-op; a DB error leaves it pending | [`test_consumer.py`](../services/ops-ai/src/ops_ai/ingest/__tests__/test_consumer.py), [`test_consumer_dead_letter.py`](../services/ops-ai/src/ops_ai/ingest/__tests__/test_consumer_dead_letter.py) (`test_a_database_error_while_applying_a_valid_event_propagates_and_leaves_the_entry_pending`) |
| C5 | Provider result stays unknown past the retention window | **Design only** (reconciler + manual-reconciliation ticket) | — |

No test kills a real process; all faults are injected in-process.

---

## 9. What was cut, and how it would be built

Each cut was made to keep a Basic item or a P0 proof. Order of the list: closest to the money first. The "Status on main" note records cuts that later merges built (2026-11-05); the traceability matrix has the detail.

| Cut | Why it was cut | How it would be built |
|---|---|---|
| Reconciler and lease reclaim | Needs a provider lookup API and a scheduler; the 202-unknown replay (D3.3) already stops double charges from client retries | D3.3 "Design only" paragraph; a periodic worker with `FOR UPDATE SKIP LOCKED` over expired leases |
| External-rail holds (`bank_transfer`, `upi`) | No external rail in scope; `held_minor` exists on wallet balance rows and is respected by transfers, but nothing places a hold | hold in tx1 (`available −= x`, `held += x`); capture / release in tx2 with `WHERE state = 'held'` so duplicates are no-ops; `unknown` stays held Status on main: **Built:** `bank_transfer` / `upi` payouts hold in tx1 and capture / release in tx2 against a simulated rail (B2.5); no payout reconciler. |
| FX conversion | Time (D3.5) | D3.5 four-line journal through per-currency clearing accounts Status on main: **Built:** FX legs through per-currency clearing accounts (D3.5, B2.3); chargebacks are not FX-aware. |
| DB-level ledger invariant | Application check is tested; a trigger needed careful testing | deferred constraint trigger summing postings per `(journal_id, currency)` at commit |
| Separate DB roles, audit rejection test | One owner role in compose | app role without `UPDATE`/`DELETE` on `audit_log`, migrations by an owner role; a test as the app role Status on main: **Built (partial):** `gateway_app` via `SET ROLE` and append-only tests (D6.6, P0-10); the demo still logs in as superuser. |
| RBAC policy table, ops principal | No ops user flow in the demo | `role × action` table; route dependency checks it; matrix test generated from the table Status on main: **Built (partial):** policy table, matrix test, ops principals (D6.5, P0-9). |
| gRPC merchant API | REST covers the demo; gRPC adds codegen and a second transport | `CreatePayment` / `GetPayment` / `ListPayments` calling the same domain functions and the same validation module; `idempotency-key` metadata Status on main: **Built:** merchant gRPC with TLS (plaintext only with `GATEWAY_GRPC_INSECURE=true`, used by the loopback demo) (B8.2). |
| pgvector + RRF hybrid search | The B6 session did not land | D5.6 Status on main: **Built:** D5.6, B6; the SQ1 `search_incidents` tool is on hybrid search over indexed incidents (B5.1); the failure assistant and tool catalog still call it full-text. |
| Session memory (Redis) and long-term merchant memory | Not landed at this snapshot | Redis hash keyed `(merchant_id, user_id, session_id)` with TTL and ownership check; long-term memory only via a confirmed write with the source turn id Status on main: **Built:** `ai/memory/` (B10.1–B10.3). |
| Answer-contract checker | Tools-first routes made it less urgent | claims with `evidence_refs`, slots rendered from tool output, reject digits outside slots, regenerate once then T2 Status on main: **Built for `/query` and `/failures`:** `ai/assistant/grounding.py` (B5.2, D5.1). |
| Rate limits, circuit breaker | P1, after correctness | Redis token bucket per API key and per user, 429 + `Retry-After`, fail-open with a metric; breaker per route that never reroutes an `unknown` attempt Status on main: **Built:** Redis token buckets and a per-route breaker ([production-readiness](production-readiness.md#resilience)); the breaker counts non-decline provider rejections as failures. |
| OTel, metrics, alert rules, Grafana | P0-12–14 lost to Basic items | OTel SDK with FastAPI/SQLAlchemy/Redis/httpx instrumentation, trace context in outbox headers; Prometheus `/metrics`; `promtool test rules` in CI Status on main: **Built (partial):** P0-12–P0-14; no production exporter, `make alerts-test` is outside `make ci`. |
| Load test | P1-5 not run | k6 on `POST /v1/payments` at a stated load, p50/p95/p99 with the error rate Status on main: **Run:** k6 at 2 and 40 req/s on a laptop; the p99 < 2 s target is met at both rates, 40 req/s with four gateway workers ([docs/perf](perf/README.md)). |
| Key revocation | JWTs are short-lived (≤ 900 s) | `jti` denylist in Redis checked by ops-ai; API-key revocation flag checked on lookup |

---

## 10. Production design on GCP (P2, design only)

Nothing in this section is implemented or measured. Numbers are design targets or labeled estimates.

### Local → GCP mapping

| Local (compose) | GCP | Note |
|---|---|---|
| `gateway-api`, `ops-ai-api` | GKE Autopilot (gateway), Cloud Run (ops-ai) | gateway needs steady connection pools and pod disruption budgets; ops-ai is bursty |
| `relay`, `ops-ai-consumer`, notifier | GKE Deployments | long-running workers |
| Postgres | Cloud Spanner or Cloud SQL for PostgreSQL (P2-1) | |
| Redis (locks) | Memorystore for Redis | still optional for correctness (D3.1) |
| Redis Streams | Pub/Sub with ordering keys per `payment_id` | consumers keep `processed_events` dedupe; Pub/Sub is also at least once |
| Secrets in env / files | Secret Manager + Cloud KMS | |
| Vault table | Separate project + VPC, Cloud HSM keys (P2-3) | |
| nginx `web` | Cloud Storage + Cloud CDN for the bundle, external HTTPS LB + Cloud Armor in front of the APIs | Cloudflare (Pages + Workers routes + WAF) is a written alternative edge. It adds a vendor in series, so it does not help 99.999 %, and if it terminates TLS it **processes** card data and PII in transit, which puts it in PCI scope even though it stores nothing |
| OTel SDK, Prometheus `/metrics`, Grafana (local) | Cloud Trace, Managed Service for Prometheus | instrumentation code would not change; no production exporter is configured yet |

### P2-1 Multi-region active-active

- **Decision:** Every account has a **home region**; all writes for that account go to its home region. Use **Cloud
  Spanner** (multi-region configuration) for the gateway's money tables.
- **Why:** The ledger needs serializable writes on one wallet row and per-currency balance checks. Spanner gives
  external consistency across regions without application-level conflict resolution, so the existing lock strategy
  (section 4) keeps its meaning. A home region keeps the hot path's write quorum close to the user.

| | Cloud Spanner (multi-region) | Per-region Cloud SQL + reconciliation |
|---|---|---|
| Consistency | Externally consistent transactions across regions; a cross-region transfer is one transaction | Strong only inside a region; a cross-region transfer becomes a saga (debit, in-transit account, credit) with eventual consistency and compensations |
| Failover | Automatic, no data loss for committed transactions (RPO 0 by design) | Regional replica promotion; RPO > 0 with async cross-region replication; manual or scripted |
| Cost | Higher fixed cost (node / processing-unit minimum per region in the configuration) | Lower at small scale; grows with replicas and the reconciliation tooling |
| Operations | No sharding by hand; schema changes are online; Postgres dialect is partial (no triggers, advisory locks differ) | Familiar Postgres; manual sharding, saga recovery and reconciliation jobs to operate |
| Code impact | Replace the audit trigger and `pg_advisory_xact_lock` with Spanner equivalents (row lock on a card-window row); keep `FOR UPDATE` semantics via read-write transactions | Keep the code; add the cross-region saga and its reconciler |

- **What we gave up:** Cost at low volume, and some Postgres features. Write latency for a cross-region transfer pays
  a cross-region commit.
- **Alternatives:** Per-region Cloud SQL with a cross-region saga (cheaper, but every cross-region transfer has an
  in-transit state and a reconciliation duty); CockroachDB (similar model, self-operated or a third-party service).

### P2-2 99.999 % availability

- **Decision:** Target 99.999 % for the **payment write path** (`POST /v1/payments`, transfers, refunds) only. The
  ops assistant has its own SLO (p95 < 20 s, with T2 fallback). Error budget: 0.001 % of a 30-day month
  = **25.9 s/month** (≈ 26 s).
- **Why:** Five nines is only plausible for a narrow path with no single point of failure; the AI side is explicitly
  allowed to degrade.

| Component | SPOF today (compose) | Production failover |
|---|---|---|
| Postgres | single container | Spanner multi-region (P2-1), or Cloud SQL HA + cross-region replica |
| Redis | single container | Memorystore HA; payments continue without it (D3.1) |
| Redis Streams / relay | single stream, single relay | Pub/Sub (regional, replicated); relay as ≥ 2 replicas using `SKIP LOCKED` |
| gateway-api | single container | ≥ 3 pods per region across zones, PDB, multi-region LB with health checks |
| Provider | simulator | ≥ 2 acquirers per route; breaker per route; never reroute an `unknown` attempt |
| KMS / HSM | env key | regional key rings; cached data keys so a short KMS blip does not stop charges |
| Edge | nginx | global HTTPS LB + Cloud Armor; no second edge vendor in series |
| ops-ai | single container | not on the five-nines path; failure → T2 or 503 for the assistant only |

- **Error-budget policy:** while budget remains, normal releases. When more than 50 % is spent in a month, only
  canaried releases with automatic rollback (P2-5). When it is spent, freeze feature releases on the payment path;
  only reliability fixes and security patches ship until the 30-day window recovers; every budget-burning incident gets
  a post-mortem with an owner.
- **What we gave up:** The claim covers the write path, not the whole product. A 26 s budget means a single regional
  failover must complete in seconds, which is only realistic with Spanner-style automatic failover.
- **Alternatives:** 99.99 % (≈ 4.3 min/month), which per-region Cloud SQL HA can meet more cheaply.

### P2-3 PCI-DSS scope reduction

- **Decision:** Move tokenization into a **vault service in its own GCP project and VPC**. Card entry uses hosted
  fields served from the vault origin, so the PAN goes browser → vault and never touches the gateway or the SPA. The
  vault returns a token; the gateway only ever sees token, BIN, last4 and fingerprint (as it does today). Vault keys
  (PAN encryption for the acquirer hand-off, fingerprint HMAC key) live in **Cloud HSM** (FIPS 140-2 Level 3); other
  service keys in Cloud KMS. VPC Service Controls around the vault project; only the vault's service account can use
  its keys; access logged to a separate audit project.
- **Why:** PCI scope follows the cardholder data. Keeping the PAN in one small project reduces the systems that need
  the full set of controls. Today the PAN already does not persist (D3.6), but it transits the gateway process.
- **What we gave up:** One more service and network hop on the charge path. The SAQ level depends on the final page
  layout and is not claimed.
- **Alternatives:** A third-party vault / processor tokenization (Stripe, Adyen) — removes most scope, adds vendor
  dependency; keeping the vault inside the gateway project (simpler, whole gateway stays in scope).

### P2-4 Data residency and GDPR erasure

- **Decision:** Residency follows the home region (P2-1): a user's PII and ledger rows live in the region they are
  assigned to. PII sits in one erasable table per region. Events, the projection and the ledger carry pseudonymous ids
  only (the existing events already carry `card_ref`, not the fingerprint, and no email — `tests/test_notification_events.py`
  asserts the pseudonymous field set). Free-text that may contain PII is encrypted with a **per-user data key**
  (envelope-encrypted by Cloud KMS); erasure deletes that key (**crypto-shredding**), which also makes copies in backups
  unreadable.

| Store | Contents | Retention | On erasure |
|---|---|---|---|
| Gateway users / PII table | email, name, phone | life of account | delete row |
| TOTP secrets | encrypted secret | life of account | delete row |
| Ledger, payments, journals | pseudonymous ids, amounts | legal retention (e.g. 7–10 years, jurisdiction-dependent) | keep; no PII to erase |
| Audit log | actor ids, summaries without PII | legal retention | keep; summaries already exclude email |
| Outbox / streams | pseudonymous events | demo: forever; production: 30 days + snapshot | none needed |
| ops-ai projections, search documents | pseudonymous facts, incident text | 13 months | rebuild without the user; shred per-user text keys |
| Assistant conversations and memory | user text | session TTL; memory until deleted | delete by `user_id`; shred key |
| Backups | all of the above | 35 days (P2-6) | crypto-shredding covers encrypted text; plain PII expires with the backup |

- **What we gave up:** Cross-region analytics need pseudonymous aggregates only. Per-user keys add a KMS dependency to
  reads of free text.
- **Alternatives:** Hard-delete across every store and backup (impractical for backups and append-only logs).

### P2-5 Release safety

- **Decision:** Progressive delivery for the payment path: deploy to one zone's canary pods with 1 % → 10 % → 50 % →
  100 % traffic steps, each held while automated checks compare canary against baseline on payment success rate,
  5xx rate, p99 latency and `unknown` outcome rate. Any regression rolls back automatically. Behaviour changes ship
  dark behind feature flags (risk thresholds, routing config, new rails) with per-merchant targeting. Database
  migrations are expand → migrate → contract, so the previous release always runs against the new schema (the
  existing CI already checks `upgrade head` and `downgrade -1` per service).
- **Why:** Most outages are self-inflicted by deploys; with a 26 s budget the rollback must be automatic.
- **What we gave up:** Releases take longer (hours, not minutes).
- **Rollback procedure:** stop the rollout → shift traffic to the previous ReplicaSet → disable any flag the release
  turned on → leave the expanded schema in place (it is backward compatible) → open an incident.
- **Alternatives:** Blue/green with a full switch (simpler, but exposes 100 % of traffic at once).

### P2-6 Backup and restore

- **Decision:**

| Store | Backup | RPO target | RTO target |
|---|---|---|---|
| Spanner (or Cloud SQL) money tables | Point-in-time recovery (7-day version retention) + daily backups kept 35 days, copied to a second region | 0 for regional failure (synchronous replicas); ≤ 5 min for logical corruption via PITR | 15 min for regional failure (automatic); ≤ 4 h for a PITR restore |
| ops-ai projections | none needed; rebuilt by replaying events + reseeding | n/a | ≤ 2 h (rebuild) |
| Vault | HSM-backed key backup per Cloud HSM policy; token table backed up like money tables | 0 / ≤ 5 min | ≤ 1 h |
| Audit log | daily export to a WORM bucket (P2-8) | 24 h for the WORM copy; 0 in the primary | n/a (append-only) |

- **Restore drill:** quarterly, restore the latest backup into an isolated project, run the ledger invariant check
  (every journal balances per currency; account balances equal the sum of postings) and compare counts with the
  source; record the measured RTO. A drill that misses its target is an incident.
- **Why:** A backup that has never been restored is not a backup; the ledger invariant is the cheapest proof that a
  restore is consistent.
- **What we gave up:** Cost of cross-region backup copies and drill projects.
- **Alternatives:** Rely on replicas only (replicas faithfully copy a bad migration or a mass delete).

### P2-7 100k TPS topology

- **Decision:** Stateless, horizontally scaled API pods; money tables sharded by home region and, within a region,
  by account / merchant id (Spanner splits on key ranges; key on a hashed prefix to avoid hotspots); time-partitioned
  append-only tables (postings, outbox, audit, payment_facts) by day; Pub/Sub (or Kafka on GKE, if ordering per key and
  replay at this volume justify running it) instead of Redis Streams; the outbox relay partitioned by shard.
- **Why:** Per-charge work today is two short transactions with roughly ten row writes (idempotency, payment, attempt,
  updates, two postings, journal, outbox, audit) — an **estimate**: 100k charges/s ≈ 1M row writes/s, beyond a single
  Postgres primary; it has to be spread by key. Locks are per wallet, per payment and per card (section 4), so they
  shard cleanly; the only global hot spots would be platform accounts, which already take append-only postings with
  no balance row.
- **Hot-row mitigation:** a large merchant's payable account is append-only (no lock). A hot *user* wallet still
  serializes; if that matters, split it into N sub-balances and draw from any with enough funds.
- **What we gave up:** Cross-shard queries (history across merchants) move to the analytics side (BigQuery via
  Pub/Sub). A k6 run of `POST /v1/payments` exists ([perf results](perf/README.md), 2026-11-05, one run each, 90 s
  `constant-arrival-rate`): at an offered 2 req/s, p50 405 ms / p95 1.91 s / p99 2.82 s with 0 % errors (re-run on 2026-11-06 on a less loaded
  VM: p50 72 ms / p95 524 ms / p99 1.25 s, target met); at an offered
  40 req/s the first run saturated near 7 completed req/s (p99 44.5 s); the 2026-11-06 re-run shows one gateway process
  is CPU-bound (p99 13.77 s); the committed compose now runs four uvicorn workers with Prometheus multiprocess
  metrics and meets the target (2026-11-07: p99 338 ms, 0 % errors). Conditions of the first runs: one laptop, a Docker VM shared by about 14 compose stacks, one single-worker
  gateway process, local Postgres and Redis, simulator acquirer, all load on 3 test cards (the per-card lock
  is held only in the claim transaction and was not the limit). So these numbers do not size a production pod; sizing still waits for a run on dedicated hardware
  with many distinct cards.
- **Alternatives:** Vertical scaling of one Postgres (does not reach the target); a CRDT/eventually consistent balance
  (cannot guarantee "never overdraw" synchronously).

### P2-8 Other design-only items

#### mTLS between services

- **Decision:** Service-to-service traffic (gateway ↔ vault, ops-ai → gateway query API, workers → databases) uses
  mTLS with workload identities (GKE Workload Identity + a service mesh such as Cloud Service Mesh), with authorization
  policies naming which service account may call which service.
- **Why:** Today only browser → nginx is encrypted, and only with `make tls-up` (the `web-tls` profile); nginx → web,
  web → gateway / ops-ai APIs, the services → Postgres and Redis, and the default merchant gRPC port are plaintext on the
  compose network. The only field-level encryption is the TOTP secret (AES-256-GCM, D6.2).
  The demo's JWT verification proves *who the user is* but not *which service is calling*.
- **What we gave up:** Mesh operational overhead and some latency.
- **Alternatives:** Per-service TLS with hand-managed certificates (rotation burden); network policies only (no
  caller identity).

#### WORM audit checkpoint

- **Decision:** Add a per-row hash chain (`prev_hash`, `row_hash`) to `audit_log`; every hour write the latest
  `row_hash` and row count to a Cloud Storage bucket with a locked retention policy (WORM); a verifier recomputes the
  chain and compares it with the checkpoints.
- **Why:** The trigger (D6.6) stops the application from editing audit rows but cannot stop a database owner. A
  checkpoint outside the database makes in-place edits *detectable* after the fact.
- **What we gave up:** Tamper detection, not prevention; edits between checkpoints are detected only at the next
  verification.
- **Alternatives:** A ledger database with built-in verification (e.g. immudb, or a managed ledger service) — another
  datastore to operate.

#### AML / KYC re-verification triggers

- **Decision:** KYC status and tier would gate transfer limits in the gateway (today SQ2 only *explains* a decision
  recorded by an external system, D5.3). Re-verification is triggered by: cumulative cross-border volume crossing a
  tier threshold; a change of payout bank account, phone or email followed by a large transfer within 24 h; a new
  corridor to a higher-risk country; a sanctions-list update that newly matches the user; document expiry; and a
  `fraud` queue ticket confirmed by a human. A triggered user's cross-border limit drops to the lowest tier until
  re-verified; each trigger writes an audit row and a `compliance` ticket.
- **Why:** These are the events where the original KYC no longer describes the risk.
- **What we gave up:** No automated decisioning; the platform raises tickets and lowers limits, humans decide.
- **Alternatives:** Periodic re-verification only (misses event-driven risk); a third-party AML monitoring service
  feeding the same ticket queue.
