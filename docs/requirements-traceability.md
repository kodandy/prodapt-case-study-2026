# Requirements Traceability

Source: `docs/caes-study.pdf` ("Requirement 1 (Basic)", "Sample queries") and the production-readiness instruction (D-020).
Every requirement is split into sub-items. A sub-item is **Done** only when both the
"Satisfied by (code)" and "Verified by (test)" columns point at real files and the test passes in `make ci`.

Priority (D-020, strict): **Basic** (B1–B13, SQ, D, DL) → **P0** (implemented and proved by tests) → **P1** (if time) → **P2** (design doc only). P0/P1 work never displaces a Basic item.

Status legend: `Planned` → `In progress` → `Done` → `Re-verified` (Phase 5 final check).

Path prefixes: `gw/` = `services/gateway/src/gateway/`, `ai/` = `services/ops-ai/src/ops_ai/`. Tests live in `<feature>/__tests__/`.
Code and test paths are real files on main (checked 2026-11-04); `none` means nothing implements the row yet.

## B1. User account management with sign-up, login, MFA, and wallet management

| ID | Sub-requirement | Acceptance criterion | Satisfied by (code) | Verified by (test) | Phase | Status |
| --- | --- | --- | --- | --- | --- | --- |
| B1.1 | Sign-up | Password stored as argon2 hash, never plain; duplicate email → 409 | `gw/accounts/service.py` (`sign_up`, `hash_password`), `POST /v1/signup` in `gw/app.py` | `gw/accounts/__tests__/test_accounts_service.py`, `gw/__tests__/test_app.py` | 2 | Done |
| B1.2 | Login | Valid credentials → session token; wrong password → 401; secrets never logged | `gw/accounts/service.py`, `gw/accounts/tokens.py`, `gw/accounts/sessions.py`, `POST /v1/sessions`, `/v1/user-tokens` in `gw/app.py` | `gw/accounts/__tests__/test_b1_routes.py`, `gw/accounts/__tests__/test_user_login.py`, `gw/accounts/__tests__/test_login_throttle.py` | 2 | Done |
| B1.3 | MFA (TOTP) | Enrol returns provisioning URI; when MFA is enabled, token is issued only after a valid TOTP; wrong/expired code → 401; TOTP secret encrypted at rest | `gw/accounts/mfa.py` (AES-GCM sealed secret), `POST /v1/me/mfa/totp[/verify]` | `gw/accounts/__tests__/test_mfa.py`, `gw/accounts/__tests__/test_b1_routes.py`, `gw/__tests__/test_user_tokens_totp.py` | 2 | Done |
| B1.4 | Wallet management | Create one wallet per currency, top-up, read balance per currency; balance is derived from ledger postings; `available` and `held` shown separately | `gw/wallets/service.py` (`ensure_wallet`, `top_up_wallet`, `list_wallets`), `GET /v1/me/wallets` | `gw/wallets/__tests__/test_wallet_service.py`, `gw/wallets/__tests__/test_transfers.py` | 2 | Partial (missing: no HTTP route to create a wallet or top up, only service functions and sign-up USD wallet; balance is read from the `AccountBalance` row kept beside postings, not derived by summing them) |

## B2. Payment processing for peer-to-peer and merchant transactions with multi-currency support

| ID | Sub-requirement | Acceptance criterion | Satisfied by (code) | Verified by (test) | Phase | Status |
| --- | --- | --- | --- | --- | --- | --- |
| B2.1 | P2P payment | Wallet-to-wallet transfer posts balanced journal in one DB transaction under the user-account lock | `gw/wallets/service.py` (`transfer`, `_lock_balances` FOR UPDATE), `POST /v1/me/transfers` | `gw/wallets/__tests__/test_transfers.py`, `gw/wallets/__tests__/test_transfer_route.py` | 2 | Done |
| B2.2 | Merchant payment | Charge with a tokenised **test** card; raw PAN never persisted | `gw/payments/charge.py`, `gw/payments/tokenization/cards.py`, `gw/payments/schemas.py` | `gw/payments/__tests__/test_charge.py`, `gw/payments/__tests__/test_schemas.py`, `gw/payments/tokenization/__tests__/test_cards.py` | 2 | Done |
| B2.3 | Multi-currency | Cross-currency payment posts FX legs through per-currency clearing accounts; each currency nets to 0; rate id on the journal | none (no FX rate, clearing accounts or cross-currency payment in code) | none | 2 | Planned |
| B2.4 | Atomicity / ledger balance | Insufficient funds moves nothing (zero postings, no transfer row); every journal balances per currency (see P0-2). Wallet transfer → 409 `insufficient_funds` with the current `available_minor` (decided in spec `gateway-basic.md`, wallet transfers; the answer is not stored, so a retry after a top-up can succeed); a card charge the issuer declines for funds → 201 `declined`, `decline_code: insufficient_funds` | `gw/wallets/service.py`, `gw/payments/ledger/service.py`, `gw/payments/charge.py` | `gw/wallets/__tests__/test_transfers.py`, `gw/payments/ledger/__tests__/test_ledger_service.py`, `gw/payments/__tests__/test_charge.py` | 2 | Done |
| B2.5 | External-rail wallet debit | `bank_transfer` / `upi` payout takes a hold in tx1; success captures, definitive failure releases, `unknown` keeps it held; duplicate capture/release is a no-op | none (no `bank_transfer` / `upi` method, hold or capture/release in code) | none | 2 | Planned |

## B3. Transaction history, refunds, and dispute resolution workflows

| ID | Sub-requirement | Acceptance criterion | Satisfied by (code) | Verified by (test) | Phase | Status |
| --- | --- | --- | --- | --- | --- | --- |
| B3.1 | Transaction history | `GET /v1/payments` (merchant, REST + gRPC `ListPayments`) and `GET /v1/me/transactions` (consumer); cursor-paginated; filter by status, date range, method; caller sees only own/tenant records. Done: merchant REST `GET /v1/payments` with `state`, half-open `created_from`/`created_to` (ISO 8601 with offset, normalised to UTC) and `method` filters that combine with the cursor; bad value or reversed range → 400 (spec `gateway-basic.md`). Consumer history ships as `GET /v1/me/transfers` (cursor only, no filters yet). Not yet: gRPC `ListPayments`, consumer filters | `gw/app.py`, `gw/payments/history.py` (`list_payments`), `gw/wallets/` | `gw/payments/__tests__/test_history.py`, `gw/wallets/__tests__/test_transfer_history.py` | 2 | In progress |
| B3.2 | Refunds | Full and partial; cumulative refund ≤ captured amount; duplicate refund rejected; requires `Idempotency-Key` | `gw/payments/refunds.py`, `POST /v1/payments/{id}/refunds` | `gw/payments/__tests__/test_refunds.py`, `gw/payments/__tests__/test_b3_hardening.py` | 2 | Done |
| B3.3 | Dispute workflow | State machine `opened → under_review → won \| lost`; invalid transitions rejected; outcome from the simulated network or recorded by `ops_lead`; merchant can only submit evidence; `lost` posts chargeback and sets `chargeback_flag` | `gw/payments/disputes.py` (`open_dispute`, `submit_evidence`, `resolve_dispute`), `POST /v1/disputes/{id}/evidence`, `GET /v1/disputes/{id}` | `gw/payments/__tests__/test_disputes.py`, `gw/payments/__tests__/test_b3_hardening.py` | 2 | Partial (missing: no HTTP route to open or resolve a dispute (test asserts none), so `ops_lead` / simulated-network outcome is service-level only; no `chargeback_flag` column) |
| B3.4 | Refund × chargeback conflict | Refund + chargeback on the same payment can never return more than the captured amount | `gw/payments/disputes.py`, `gw/payments/refunds.py` | `gw/payments/__tests__/test_disputes.py` (`..._never_exceed_the_payment_amount_in_the_ledger`) | 2 | Done |

## B4. NLP-enabled transaction failure reason detection

| ID | Sub-requirement | Acceptance criterion | Satisfied by (code) | Verified by (test) | Phase | Status |
| --- | --- | --- | --- | --- | --- | --- |
| B4.1 | Natural-language query by support agents | NL text → typed `FailureQuery` (merchant, window, method, status) via LLM function calling; mock LLM gives the same structure | `ai/investigation/query.py` (`parse_failure_query`, `FailureQueryArgs`, rules fallback), `ai/assistant/routes.py` (`POST /v1/assistant/failures`) | `ai/investigation/__tests__/test_failure_query.py`, `ai/assistant/__tests__/test_failure_routes.py` | 3 | Done (country is parsed but reported in `unapplied_filters`: payment facts carry no country) |
| B4.2 | Structured failure reasons | Raw/normalized codes and free-text `failure_reason` classified rules-first into `{taxonomy_code, category, owner}`; LLM fallback proposes from a closed list with confidence, low confidence → `unclassified`; `retryable` derived by a deterministic policy (timeouts/`unknown` never retryable); response groups `{taxonomy_code, share, count, sample_ids}` | `ai/investigation/reasons.py` (`TAXONOMY`, `classify_reason`, `label_reasons`, `failure_reason_groups`, `transfer_reason_groups`) | `ai/investigation/__tests__/test_failure_reasons.py`, `ai/investigation/__tests__/test_failures_service.py` | 3 | Done |
| B4.3 | Suggested fixes | Each reason group carries a fix from the versioned playbook, enriched by matching resolution notes; observed facts, hypotheses and how to confirm are separated | `ai/investigation/playbook.py` + `playbooks/failures.v1.json`, `ai/investigation/diagnosis.py` (`diagnose`: facts / hypotheses / how_to_confirm / ranked fixes) | `ai/investigation/__tests__/test_playbook.py`, `ai/investigation/__tests__/test_diagnosis.py`, `ai/investigation/__tests__/test_failures_service.py` | 3 | Done |

## B5. RAG-enabled support assistant for payment troubleshooting using historical transaction data

| ID | Sub-requirement | Acceptance criterion | Satisfied by (code) | Verified by (test) | Phase | Status |
| --- | --- | --- | --- | --- | --- | --- |
| B5.1 | Retrieval over historical data | Retrieves failed/flagged transactions and past incidents (`resolution_notes`) through hybrid search (B6) | `ai/search/service.py` (keyword FTS only), `ai/assistant/service.py` (`ask`), `ai/investigation/tools.py` (`search_incidents`) | `ai/search/__tests__/test_search_service.py`, `ai/assistant/__tests__/test_assistant_service.py` | 3 | Partial (missing: hybrid search, which is B6, owned by another session) |
| B5.2 | Grounded answer | Answer contract: each claim carries evidence refs produced this turn; slot values rendered from tool output; with insufficient evidence the assistant abstains | `ai/assistant/grounding.py` (`ground_claims`, `grounded_narration`, `ABSTAIN_TEXT`), `ai/assistant/tool_assistant.py`, `ai/investigation/failures.py` | `ai/assistant/__tests__/test_grounding.py`, `ai/assistant/__tests__/test_tool_assistant.py`, `ai/investigation/__tests__/test_failures_service.py` | 3 | Done (evaluation harness lives in the ai-eval-judge branch) |

## B6. Hybrid search combining keyword and semantic retrieval over transaction records

| ID | Sub-requirement | Acceptance criterion | Satisfied by (code) | Verified by (test) | Phase | Status |
| --- | --- | --- | --- | --- | --- | --- |
| B6.1 | Keyword retrieval | Postgres full-text search (`tsvector`) behind a swappable interface | `ai/search/service.py` (`search_documents`) | `ai/search/__tests__/test_search_service.py` | 3 | Done (2026-11-04; keyword leg of `hybrid_search`) |
| B6.2 | Semantic retrieval | pgvector similarity; embedder swappable (OpenAI / pinned local model), one index per embedder | `ai/search/embedder.py` (`Embedder` port, deterministic `LocalHashEmbedder`), `ai/search/openai_embedder.py` (`OPS_AI_EMBEDDER=openai`), `ai/search/models.py` (one `vector` column + HNSW index per embedder id; migration `b6e1c2d3f4a5`), `ai/search/service.py` (`vector_search`), `ai/ingest/consumer.py` + `ai/search/backfill.py` (`python -m ops_ai.search backfill`) | `ai/search/__tests__/test_local_embedder.py`, `test_vector_search.py`, `test_openai_embedder.py`, `test_backfill.py`; `ai/ingest/__tests__/test_consumer_embedding.py`; `ai/__tests__/test_config_embedder.py`, `test_main_embedder.py` | 3 | Done (2026-11-04; local embedder is feature hashing, spelling-level not semantic; ingest writes the local column only, OpenAI column via backfill) |
| B6.3 | Fusion | Reciprocal Rank Fusion; on a fixed corpus a keyword-only miss is found by vector and vice versa | `ai/search/service.py` (`reciprocal_rank_fusion` k=60, `hybrid_search`, both legs scoped to `merchant_id`), wired into `ai/assistant/service.py` (`ask`) | `ai/search/__tests__/test_rrf.py`, `test_hybrid_search.py`; `ai/assistant/__tests__/test_assistant_hybrid.py`; `ai/__tests__/test_app_embedder.py` | 3 | Done (2026-11-04; `investigation` `search_incidents` not yet on hybrid) |

## B7. Fraud detection and basic risk scoring with flagging of suspicious transactions

| ID | Sub-requirement | Acceptance criterion | Satisfied by (code) | Verified by (test) | Phase | Status |
| --- | --- | --- | --- | --- | --- | --- |
| B7.1 | Risk scoring | Synchronous rule score 0.0–1.0 with reason codes at authorisation; no LLM on the path; velocity-lookup failure follows the dependency policy (`risk_unavailable` / `degraded_scoring`) | `gw/payments/risk.py` (`assess`), `gw/payments/charge.py`, `GET /v1/payments/{id}/risk` | `gw/payments/__tests__/test_risk.py`, `gw/payments/__tests__/test_charge_risk.py`, `gw/payments/__tests__/test_charge_risk_hardening.py`, `gw/payments/__tests__/test_risk_in_flight.py` | 2 | Partial (missing: no velocity-lookup failure policy (`risk_unavailable` / `degraded_scoring`); lookup is an in-transaction DB read) |
| B7.2 | Flagging | Fraud flag is separate from payment status (a declined payment can still be flagged) | `gw/payments/risk.py`, `gw/payments/models.py` (`risk_blocked`, flag separate from `state`) | `gw/payments/__tests__/test_charge_risk.py`, `gw/payments/__tests__/test_risk.py` | 2 | Done |
| B7.3 | Card-testing detection | Real-time block in gateway; async detector in ops-ai flags the planted burst exactly on a fixed fixture; false-positive rate measured on legitimate micro-payment groups | `gw/payments/risk.py` (velocity block), `ai/fraud_watch/analysis.py`, `ai/fraud_watch/watch.py` | `ai/fraud_watch/__tests__/test_fraud_golden.py`, `ai/fraud_watch/__tests__/test_watch_payment.py`, `ai/fraud_watch/__tests__/test_analyze_fraud.py`, `gw/payments/__tests__/test_charge_risk_hardening.py` | 3 | Partial (missing: no measurement of false-positive rate on legitimate micro-payment groups) |

## B8. REST/gRPC APIs for merchant integrations with input validation guardrails and idempotency enforcement

| ID | Sub-requirement | Acceptance criterion | Satisfied by (code) | Verified by (test) | Phase | Status |
| --- | --- | --- | --- | --- | --- | --- |
| B8.1 | REST API | `POST /v1/payments`, `GET /v1/payments/{id}`, `GET /v1/payments`, refunds; merchant API key auth (stored hashed) | `gw/app.py`, `gw/payments/history.py` (`get_payment`, `list_payments`), `gw/payments/refunds.py`, `gw/accounts/service.py` (HMAC-SHA256 key hash) | `gw/payments/__tests__/test_charge.py`, `gw/payments/__tests__/test_history.py`, `gw/payments/__tests__/test_refunds.py` | 2 | Done |
| B8.2 | gRPC API | `CreatePayment` / `GetPayment` / `ListPayments` call the same domain service as REST | none (no gRPC server, proto or dependency in the repo) | none | 2 | Planned |
| B8.3 | Input validation guardrails (API) | Invalid currency, amount, card (Luhn / non-test PAN), oversize fields → field-level errors on both REST and gRPC, from one validation module | `gw/payments/schemas.py` (Luhn, allowlisted test PAN, currency, amount, length limits) | `gw/payments/__tests__/test_schemas.py`, `gw/payments/__tests__/test_b3_input_hardening.py` | 2 | Partial (missing: REST only; gRPC absent) |
| B8.4 | Idempotency enforcement | `Idempotency-Key` header (REST) / `idempotency-key` metadata (gRPC); lost-response replay returns the stored response; same key + different body → 422; concurrency proof in P0-1 | `gw/payments/charge.py`, `gw/payments/refunds.py`, `gw/payments/idempotency/models.py`, `_require_idempotency_key` in `gw/app.py` | `gw/payments/__tests__/test_charge.py`, `gw/payments/__tests__/test_refunds.py` | 2 | Partial (REST done: replay returns stored body, same key + different body → 422; missing: gRPC `idempotency-key` metadata) |
| B8.5 | Guardrails (AI side) | Tenant scope taken from auth, never from LLM output; tool allowlist + typed args; retrieved text treated as untrusted (see P0-8) | `ai/tools/registry.py` (allowlist, `extra=forbid` args, scope from caller only, `SET TRANSACTION READ ONLY` + rollback), `ai/tools/catalog.py`, `ai/scope.py`, `ai/assistant/openai_llm.py` | `ai/tools/__tests__/test_registry.py`, `ai/tools/__tests__/test_planner.py`, `ai/__tests__/test_llm_redteam_routes.py` | 3 | Done |

## B9. Tool-calling support for payment status lookup and fraud analysis

| ID | Sub-requirement | Acceptance criterion | Satisfied by (code) | Verified by (test) | Phase | Status |
| --- | --- | --- | --- | --- | --- | --- |
| B9.1 | `get_payment_status` tool | Assistant calls it with correct args for SQ2 | `ai/transfers/tools.py` (`get_payment_status`), registered as an LLM-callable tool in `ai/tools/catalog.py` (`user_tools`) | `ai/transfers/__tests__/test_get_payment_status.py`, `ai/tools/__tests__/test_registry.py`, `ai/tools/__tests__/test_planner.py` | 3 | Done |
| B9.2 | `analyze_fraud` tool | Assistant calls it with correct args for SQ3 | `ai/fraud_watch/analysis.py` (`analyze_fraud`), registered in `ai/tools/catalog.py` (`merchant_tools`) | `ai/fraud_watch/__tests__/test_analyze_fraud.py`, `ai/tools/__tests__/test_registry.py`, `ai/tools/__tests__/test_planner.py` | 3 | Done |
| B9.3 | Supporting tools | `aggregate_failures` (SQ1), `search_incidents` (SQ1/RAG), `explain_settlement` (SQ4); test asserts tool name + args per sample query | `ai/investigation/tools.py` (`aggregate_failures`, `compare_to_baseline`, `change_timeline`, `search_incidents`), `ai/tools/catalog.py`, `ai/tools/planner.py`, `ai/settlement/tools.py` | `ai/investigation/__tests__/test_baseline_timeline.py`, `ai/tools/__tests__/test_planner.py`, `ai/settlement/__tests__/test_explain_settlement_tool.py` | 3 | Done |

## B10. Memory and session management for merchant support interactions

| ID | Sub-requirement | Acceptance criterion | Satisfied by (code) | Verified by (test) | Phase | Status |
| --- | --- | --- | --- | --- | --- | --- |
| B10.1 | Session management | Redis session keyed by `(merchant_id, user_id, session_id)` with TTL and ownership check; merchant A cannot read merchant B's session | `ai/memory/session.py`, `ai/memory/routes.py` (scope from the merchant token only; another owner's session is a 404) | `ai/memory/__tests__/test_session_store.py`, `test_memory_routes.py` | 3 | Done (TTL 1800 s; per-merchant session cap is open: rate limit belongs at the edge) |
| B10.2 | Short-term memory | Last N turns + structured conversation state; "what about yesterday?" keeps merchant + metric and shifts the window | `ai/memory/state.py`, `ai/memory/conversation.py` (last 10 turns; rule-based, works with the mock LLM) | `ai/memory/__tests__/test_state.py`, `test_conversation.py` | 3 | Done |
| B10.3 | Long-term memory | Explicit, auditable `merchant_memory` records written only via a confirmed tool call, with source turn id; recalled in a new session; correct/delete endpoint | `ai/memory/store.py`, `ai/memory/models.py`, migration `b10a4e7c2d91` (`propose_merchant_memory` → `POST …/memory-proposals/{id}/confirm`; one row per source turn; `PATCH`/`DELETE /v1/assistant/memories/{id}`) | `ai/memory/__tests__/test_long_term_store.py`, `test_memory_routes.py` | 3 | Done |

## B11. Notifications for payment confirmations and suspicious activity alerts

| ID | Sub-requirement | Acceptance criterion | Satisfied by (code) | Verified by (test) | Phase | Status |
| --- | --- | --- | --- | --- | --- | --- |
| B11.1 | Payment confirmation | Event written to the transactional outbox in the payment DB transaction; worker delivers it; shown in UI | `gw/outbox/relay.py`, `gw/notifications/worker.py`, `gw/notifications/service.py` | `gw/notifications/__tests__/test_notifications.py`, `gw/notifications/__tests__/test_worker.py`, `gw/outbox/__tests__/test_relay.py`, `gw/payments/__tests__/test_charge.py` | 2 | Partial (missing: notifications are not shown in the web UI; no read API) |
| B11.2 | Suspicious activity alert | Gateway risk flag → "was this you?" notice to the cardholder/merchant; ops-ai fraud_watch → alert to ops staff only | `gw/notifications/service.py`, `ai/fraud_watch/watch.py` → `ai/alerts/` (`raise_ops_alert`, `deliver_pending` to Redis stream `ops.alerts`, staff read/ack API, `python -m ops_ai.alerts`) | `gw/notifications/__tests__/test_notifications.py`, `ai/alerts/__tests__/`, `ai/fraud_watch/__tests__/test_watch_ops_alert.py` | 2–3 | Partial (missing: "was this you?" cardholder notice; alerts router not mounted until the compliance-rbac staff dependency lands) |
| B11.3 | At-least-once safety | Redelivered event creates no duplicate notification or alert | `gw/notifications/models.py` (unique source event + kind, processed table), `ai/fraud_watch/watch.py` (`window_key` upsert) | `gw/notifications/__tests__/test_notifications.py`, `ai/fraud_watch/__tests__/test_watch_payment.py`, `ai/fraud_watch/__tests__/test_fraud_tables.py` | 2 | Done |

## B12. Simple ticket routing for operational escalations

| ID | Sub-requirement | Acceptance criterion | Satisfied by (code) | Verified by (test) | Phase | Status |
| --- | --- | --- | --- | --- | --- | --- |
| B12.1 | Escalation → ticket | Assistant escalation, fraud alert, dispute and unresolved `unknown` attempt create tickets with reason | `ai/tickets/escalation.py` (`/v1/assistant/ask`: human request or `t2_fallback`; question redacted, 50/merchant/UTC day), `ai/tickets/sources.py` (dispute = settlement-report `chargeback` flag via `seed_settlement`, since no dispute event reaches ops-ai; `unknown` older than 15 min swept by the ingest worker every 60 s), `ai/fraud_watch/watch.py` (card testing) | `ai/tickets/__tests__/` (escalation, dispute, unknown sweep, worker sweep, card-testing source) | 3 | Done |
| B12.2 | Routing | Rule router → queue `{fraud, payments_ops, settlement, compliance}` + priority | `ai/tickets/routing.py` (`route_ticket`: card_testing → fraud/high, dispute → compliance/high, unknown_payment → payments_ops/normal, escalation by keyword topic) | `ai/tickets/__tests__/test_routing_b12.py`, `test_route_ticket.py` | 3 | Done |
| B12.3 | Lifecycle + dedupe | `open → assigned → resolved`; duplicate source event creates no second ticket | `ai/tickets/service.py` (unique `source_key` + `ON CONFLICT DO NOTHING`; conditional `UPDATE … WHERE status`; invalid → 409), `ai/tickets/api.py` (`GET /v1/tickets`, `GET /v1/tickets/{id}`, `POST /v1/tickets/{id}/transition`; merchant token, own tickets only), migration `b12c4e7a9f31` | `ai/tickets/__tests__/test_lifecycle.py`, `test_open_ticket.py`, `test_tickets_api.py` | 3 | Done (no ops-staff role in the gateway yet: the API is merchant-scoped) |

## B13. Simple front-end interface for user and merchant interaction

| ID | Sub-requirement | Acceptance criterion | Satisfied by (code) | Verified by (test) | Phase | Status |
| --- | --- | --- | --- | --- | --- | --- |
| B13.1 | User screen | Sign-up/login with MFA, wallets, pay, history, refund request / dispute | `web/src/features/user/` | Playwright smoke | 2–3 | Partial (done: auth+TOTP, wallets, P2P transfer, transfer history panel `web/src/features/user/HistoryPanel.tsx`, tests `web/src/features/user/__tests__/UserArea.test.tsx`, `web/e2e/user.spec.ts`; missing: no user refund/dispute) |
| B13.2 | Merchant screen | Payments list, settlement summary, support assistant chat; refund from a `suggested_action` requires explicit confirmation | `web/src/features/merchant/` | Playwright smoke | 2–3 | Done |
| B13.3 | Ops console (addition) | NL failure query, investigation with evidence, flagged transactions, alerts, ticket queue, approvals | `web/src/features/ops/` | Playwright smoke | 3 | Design (no ops principal; see docs/specs/web.md) |

## Sample queries (must run end-to-end in the demo)

| ID | Query | Expected evidence (tools) | Expected output | Test | Phase | Status |
| --- | --- | --- | --- | --- | --- | --- |
| SQ1 | 15% of a merchant's card payments failing this afternoon — root cause and fix | `aggregate_failures`, `compare_baseline`, `change_timeline`, `search_incidents` | Failure rate vs baseline; concentration on one issuer/BIN range in the planted window; observed facts vs hypotheses vs how to confirm; ranked fixes from the playbook; citations | `ai/evaluation/__tests__/` (E2E) | 3 | Partial (service: `ai/investigation/failures.py` + `POST /v1/assistant/failures`, tested by `ai/investigation/__tests__/test_failures_service.py`, `ai/investigation/__tests__/test_baseline_timeline.py`, `ai/assistant/__tests__/test_failure_routes.py`; `compare_baseline` / `change_timeline` are now LLM-callable tools in `ai/tools/catalog.py`; missing: `ai/evaluation/` E2E, owned by the ai-eval-judge branch) |
| SQ2 | International transfer declined despite sufficient funds — what triggered it | `get_payment_status` | Balance shown sufficient (rules out NSF); the rule that fired (corridor / KYC cross-border limit) with the values that tripped it; fix | `ai/evaluation/__tests__/` (E2E) | 3 | Partial (service goldens: `ai/transfers/__tests__/test_investigate_transfer_golden.py`; no `ai/evaluation/` E2E) |
| SQ3 | Detect and flag micro-transaction series (card testing) | live burst via merchant API → detector, `analyze_fraud` | Flagged ids + pattern features; alert notification; ticket in `fraud` queue | `ai/evaluation/__tests__/` (E2E) | 3 | Partial (goldens: `ai/fraud_watch/__tests__/test_fraud_golden.py`, `test_watch_payment.py` live burst → alert + `fraud` ticket; no notification and no `ai/evaluation/` E2E) |
| SQ4 | Plain-English settlement summary: why payout differs from expected | `explain_settlement` | Typed breakdown (gross, MDR, FX, refunds, chargebacks + fees, reserve → net) verified before narration; plain-English text | `ai/evaluation/__tests__/` (E2E) | 3 | Partial (goldens: `ai/settlement/__tests__/test_settlement_golden.py`; no `ai/evaluation/` E2E) |

## Dataset

| ID | Requirement | Acceptance criterion | Satisfied by | Verified by | Phase | Status |
| --- | --- | --- | --- | --- | --- | --- |
| D1 | Synthetic dataset, ≥ 10,000 records | Seeded, reproducible; CSV and JSON derived from one canonical scenario log | `scripts/generate_dataset.py` | `scripts/__tests__/test_generate_dataset.py` | 1 | Done |
| D2 | Minimum schema | All 16 required fields with required types and enum values | `scripts/generate_dataset.py` | `scripts/__tests__/test_generate_dataset.py` | 1 | Done |
| D3 | Planted scenarios for SQ1–SQ4 | Each scenario present and detectable | `scripts/generate_dataset.py` | `scripts/__tests__/test_generate_dataset.py`, `scripts/__tests__/test_generate_dataset_sq1_bin.py` | 1 | Done |
| D4 | Schema extensions documented | README lists every added field, incl. incident resolution-effectiveness fields | `README.md` | review | 5 | Planned |

## P0. Production readiness — implemented and proved by tests (D-020)

| ID | Item | Acceptance criterion | Satisfied by (code) | Verified by (test) | Phase | Status |
| --- | --- | --- | --- | --- | --- | --- |
| P0-1 | Idempotency: DB unique + Redis | N=20 concurrent same key → simulator charge count 1, one journal, identical bodies within the wait budget; repeated with Redis flushed, Redis down, lock expired | `gw/payments/charge.py` (Redis lock + DB idempotency row), `gw/payments/idempotency/models.py` | `gw/payments/__tests__/test_charge.py` (E1 with and without Redis) | 2 | Partial (missing: no run with Redis flushed or lock expired; E1 asserts one provider call, one journal, one payment, one outbox row, identical bodies) |
| P0-2 | Balanced ledger | Hypothesis property test: random sequences of charges, transfers, FX, refunds, chargebacks, holds → every journal balances per currency; account balances = sum of postings | `gw/payments/ledger/service.py` | `gw/payments/ledger/__tests__/test_ledger_service.py` | 2 | Partial (Hypothesis covers balanced / unbalanced multi-currency journals; missing: random sequences of charges, transfers, FX, refunds, chargebacks, holds and balance = sum of postings) |
| P0-3 | Balance locking | Pessimistic `SELECT … FOR UPDATE` on user accounts in id order, covering balance check → hold/journal; platform accounts append-only. N parallel debits never overdraw. Rationale written in architecture-detail §3 | `gw/wallets/service.py` (`_lock_balances`, FOR UPDATE) | `gw/wallets/__tests__/test_transfers.py` (`test_parallel_transfers_from_one_wallet_never_overdraw_it_and_the_sums_match`) | 2 | Partial (transfer debit proven; missing: no holds; charge path does not lock user accounts) |
| P0-4 | Crash safety | Crash matrix C1–C5 (architecture-detail §7), one assertion set per failure point; at least one real process kill + restart | `gw/payments/charge.py` (crash hook), `gw/outbox/relay.py`, `ai/ingest/consumer.py` | `gw/payments/__tests__/test_charge.py` (E2 crash after tx2), `gw/outbox/__tests__/test_relay.py`, `ai/ingest/__tests__/test_consumer_dead_letter.py`, `ai/ingest/__tests__/test_worker_dead_letter.py` | 2 | Partial (missing: no reconciler, no real process kill, no `tests/e2e/crash/`; only some matrix points have tests) |
| P0-5 | LLM outage isolation | With `LlmPort` raising: charges succeed, risk flags and card-testing alerts still raised, assistant returns T2 evidence list | `ai/assistant/service.py` (`LlmUnavailableError` → `t2_fallback`); gateway risk is rule-based with no LLM import | `ai/assistant/__tests__/test_assistant_service.py`, `ai/__tests__/test_app.py` | 3 | Partial (missing: T2 path proven; no test that runs charges / risk flags / card-testing alert with `LlmPort` raising) |
| P0-6 | PII / card masking before LLM | Allowlisted DTO + free-text redaction; canary PAN/email/phone/password/token never appear in any `LlmPort` payload (exact-match assertion, independent of the detector) | `ai/assistant/openai_llm.py` (`_mask_cards`, `_strip_tags`, `_render_input`) | `ai/assistant/__tests__/test_openai_llm.py`, `ai/assistant/__tests__/test_openai_llm_hardening.py`, `ai/assistant/__tests__/test_openai_llm_redteam.py` | 3 | Partial (card-number masking and tag stripping tested; missing: no allowlisted DTO and no all-field canary test for email / phone / password / token) |
| P0-7 | Read-only tools, human approval for money | Tool registry contains no write tool except the routing proposal; refund only via gateway API with the human's JWT and explicit confirmation; `suggestion_id` linked in audit only after server-side check | `ai/investigation/tools.py`, `ai/transfers/tools.py`, `ai/settlement/tools.py` (read-only); refund only at `POST /v1/payments/{id}/refunds` | `ai/investigation/__tests__/test_investigation_tools.py`, `gw/payments/__tests__/test_refunds.py` | 3 | Partial (missing: no `suggested_action` / `suggestion_id` flow and no human-JWT refund; the web UI has an explicit refund confirmation) |
| P0-8 | Prompt-injection defense | Injection fixtures in resolution notes, memory and user input do not change the tool set, scope or checker outcome; schema-invalid model output rejected | `ai/assistant/openai_llm.py`, `ai/auth.py` | `ai/assistant/__tests__/test_openai_llm_redteam.py`, `ai/assistant/__tests__/test_openai_llm_redteam_order.py`, `ai/__tests__/test_llm_redteam_routes.py` | 3 | Partial (red-team tests on model output and tag handling; missing: injection fixtures in resolution notes and memory (no memory), no schema-invalid output rejection test) |
| P0-9 | RBAC | Policy table `role × action` (consumer, merchant_admin/merchant_support, ops_agent, ops_lead = admin); generated matrix test + consumer ownership + merchant tenancy tests | `gw/accounts/` (consumer user, merchant API key, tenant from key), `ai/scope.py` | `gw/accounts/__tests__/test_sessions.py`, `gw/accounts/__tests__/test_merchant_id_and_keys.py`, `gw/payments/__tests__/test_refunds.py` (other merchant → 404) | 2 | Partial (consumer ownership and merchant tenancy tested; missing: no `role × action` policy table, no ops roles, no generated matrix test) |
| P0-10 | Append-only audit | UPDATE/DELETE rejected for the app role (trigger + grants); audit row in the same tx as the operation, failure rolls the operation back; who/when/what/correlation id recorded. Hash chain optional (cut-first) | `gw/audit/models.py`, trigger `audit_log_append_only` in `services/gateway/migrations/versions/20261103_1feee27d0cd2_initial_gateway_schema.py`, audit rows written inside operations | `gw/payments/__tests__/test_charge.py`, `gw/payments/__tests__/test_disputes.py`, `gw/wallets/__tests__/test_transfers.py` (audit row per operation) | 2 | Partial (missing: trigger exists but no test that UPDATE / DELETE is rejected; no role grants; no rollback-on-audit-failure test) |
| P0-11 | No secrets / PII in logs | Canary values run through a full E2E flow; captured logs of all services contain none of them | none (no cross-service log canary) | none | 2–3 | Planned |
| P0-12 | Correlation id + OTel traces | One trace id across API span → outbox event header → consumer span → tool and LLM spans; every log line carries `trace_id` | none (no OTel, no `trace_id`) | none | 3 | Planned |
| P0-13 | Metrics | `/metrics` exposes request count, errors, latency histograms (p50/p95/p99), payment success rate, failures by taxonomy code, fraud flags, LLM tokens/latency/cost/eval score; mock values labeled `mock` | none (no `/metrics` endpoint) | none | 3 | Planned |
| P0-14 | Alert rules | Rule file (card failure rate > 3× baseline, provider errors, outbox/consumer lag, reconciler unknown, LLM fallback); `promtool test rules` passes on fixtures | none (`ops/` directory does not exist) | none | 3 | Planned |
| P0-15 | CI with eval gate | `ci.yml` + `make ci`: ruff, mypy strict, pytest, tsc, eslint, eval; deterministic DeepEval custom metrics gate on thresholds; cross-tenant leakage gate fails on a single leak; LLM-judge metrics only with a key, skip reported | `.github/workflows/ci.yml` (runs `make ci`), `Makefile` (ruff, mypy, migrate-check, pytest, `web-ci`) | CI run log | 1→5 | Partial (missing: no `ai/evaluation/`, no eval gate, no cross-tenant leakage gate) |

## P1. If time

| ID | Item | Acceptance criterion | Phase | Status |
| --- | --- | --- | --- | --- |
| P1-1 | Transactional outbox | Already Basic (B11.1) and covered by P0-4; listed here only to match the instruction | 2 | Partial (via B11.1: `gw/outbox/` relay + worker with `gw/outbox/__tests__/`; crash coverage is P0-4, Partial) |
| P1-2 | Retry + DLQ | Consumer retries with exponential backoff and jitter; dead-letter stream after N failures; test that a poison event lands in the DLQ and the rest proceed | 4 | Partial (`ai/ingest/consumer.py` dead-letters malformed entries, `ai/ingest/__tests__/test_consumer_dead_letter.py`; missing: no retry with backoff and jitter, no after-N-failures rule) |
| P1-3 | Provider timeout + circuit breaker | Breaker per route opens on error rate, half-opens after cool-down; `unknown` attempts are never rerouted | 4 | Planned (no breaker in code) |
| P1-4 | Rate limits | Token bucket per merchant API key and per user; 429 with `Retry-After`; Redis down → fail-open with a metric | 4 | Planned (no rate limit in code; only login throttle `gw/accounts/throttle.py`) |
| P1-5 | Load test | k6 or Locust on `POST /v1/payments` (card) at a stated load; p50/p95/p99 + error rate in the design doc, with the 100k TPS production design | 4 | Planned (no load test) |
| P1-6 | Health, readiness, graceful shutdown | `/healthz` and `/readyz` (DB, Redis, stream reachability) on every process. On SIGTERM: stop accepting requests, drain in-flight ones; a consumer finishes its current message and XACKs before exit; the relay finishes its current batch. Test: SIGTERM mid-processing → no duplicate and no lost work | 2 | Partial (`/healthz` + `/readyz` on gateway and ops-ai, `gw/__tests__/test_app.py`, `ai/__tests__/test_app.py`; stop signal handled in `gw/outbox/worker.py`, `gw/notifications/worker.py`, `ai/ingest/worker.py` with tests; missing: no SIGTERM drain test for the API servers; relay and consumer have no mid-processing SIGTERM test) |
| P1-7 | DB migrations (Alembic) | Separate migration history per service; upgrade from empty DB to head and downgrade by one step run in CI. Pulled forward to Day 1 (D-021): the schema is created through Alembic from the start | 1 | Done (Alembic per service in `services/*/migrations/`; `make migrate-check` round-trips upgrade, downgrade -1, upgrade and `alembic check`, run by `make ci`) |
| P1-8 | Model and prompt pinning, fallback model, LLM cost cap | Model ids pinned to dated versions in env; prompts are versioned files whose hash is stored in the AI audit trail; primary model failure → fallback model → T2; daily token budget per merchant, exceeded → T2 with a metric. Tests for each path on the mock `LlmPort` | 3 | Partial (`ai/assistant/openai_llm.py` fallback model + T2 fallback, `ai/assistant/__tests__/test_openai_llm_hardening.py`; missing: model ids not dated-pinned in env, no prompt hash in audit, no per-merchant daily token budget) |

P1 order (cheapest per value first, D-021): P1-6 → P1-8 → P1-5 → P1-3 → P1-4 → P1-2. P1-7 is not in this order because it moved to Day 1.

## P2. Design doc only

Written in `docs/design.md` with a compose → GCP mapping (Cloud Run / GKE, Cloud SQL / Spanner, Memorystore, Pub/Sub replacing Redis Streams, Cloud KMS / HSM, Managed Prometheus, Cloud Trace). OTel and Prometheus carry over unchanged.

| ID | Item | Content |
| --- | --- | --- |
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
| --- | --- | --- | --- | --- |
| DL1 | Architecture diagram (PDF/JPEG) | `docs/architecture.md` (runtime topology from `compose.yaml` + card payment, P2P transfer, refund, events into ops-ai, ops assistant; planned parts dashed) → `make docs-diagram` (mmdc in `minlag/mermaid-cli:11.4.2`) → `docs/architecture.pdf`, `.png` | 5 | Done (2026-11-04; re-render after the feature freeze) |
| DL2 | Design decisions and trade-offs | `docs/design.md`: every decision as Decision / Why / What we gave up / Alternatives (DOC-4); lock strategy, P0 evidence map, P2 items | 5 | Done (snapshot `b5f7271`; P0 map links only existing test files; P2-1–P2-8 each have a section, design only) |
| DL3 | Executable code + README (setup, ≥ 1 E2E example) | `README.md` (all 4 sample queries with input and output; `make ci`) | 5 | Planned |
| DL4 | 10-minute presentation | `docs/presentation.md`: 0:30 goal, 4:00 SQ1–SQ4, ≥ 3:00 production design (PR-1–PR-3, one line on crash recovery), 0:30 wrap-up, 2:00 Q&A | 5 | Planned |

## Production documents and demo items (D-021)

| ID | Item | Satisfied by | Phase | Status |
| --- | --- | --- | --- | --- |
| DOC-1 | Production readiness status per NFR | `docs/production-readiness.md`: each NFR marked 実装しテスト確認済み / 手元で検証済み / 設計のみ, with evidence (test name, metric, design section). Single source for the NFR claims (moved from architecture-detail §8) | 5 | Planned |
| DOC-2 | Runbook: card failure-rate spike | `docs/runbooks/card-failure-spike.md` (SQ1 scenario): alert fired → narrow by method / route / BIN → `change_timeline` → log and trace queries → rollback or approved reroute | 5 | Planned |
| DOC-3 | Runbook: LLM provider outage | `docs/runbooks/llm-provider-outage.md`: detect (LLM failure rate, fallback count) → confirm payments unaffected (T0 metrics) → run on fallback model or T2 → confirm recovery | 5 | Planned |
| DOC-4 | "Why / what we gave up" for every decision | `docs/design.md` format + `gave_up` field on each decision in `state/progress.json` | 5 | Planned |
| PR-1 | Demo: concurrent same-key test | Live run of the P0-1 test (≈ 40 s) | 5 | Planned |
| PR-2 | Demo: failure spike → alert → runbook | SQ1 data trips the P0-14 rule; shown on a provisioned Grafana dashboard next to Prometheus alerts, linked to DOC-2 (≈ 60 s) | 5 | Planned |
| PR-3 | Demo: NFR status table | DOC-1 (≈ 60 s) | 5 | Planned |
