# Requirements Traceability — Requirement 1 (Basic)

Source: `docs/caes-study.pdf`, "Requirement 1 (Basic)" and "Sample queries".
Every Basic bullet is split into sub-items. A sub-item is **Done** only when both the
"Satisfied by (code)" and "Verified by (test)" columns point at real files and the test passes.

Status legend: `Planned` (Phase 0) → `In progress` → `Done` → `Re-verified` (Phase 5 final check).

Paths are planned locations (feature-sliced: `services/platform/src/platform/<feature>/`,
tests in `<feature>/__tests__/`). They are fixed when the code lands.

## B1. User account management with sign-up, login, MFA, and wallet management

| ID | Sub-requirement | Acceptance criterion | Satisfied by (code) | Verified by (test) | Phase | Status |
|---|---|---|---|---|---|---|
| B1.1 | Sign-up | Password stored as argon2 hash, never plain; duplicate email → 409 | `accounts/` | `accounts/__tests__/` | 2 | Planned |
| B1.2 | Login | Valid credentials → session token; wrong password → 401; secrets never logged | `accounts/` | `accounts/__tests__/` | 2 | Planned |
| B1.3 | MFA (TOTP) | Enrol returns provisioning URI; when MFA is enabled, token is issued only after a valid TOTP; wrong/expired code → 401 | `accounts/` | `accounts/__tests__/` | 2 | Planned |
| B1.4 | Wallet management | Create one wallet per currency, top-up, read balance per currency; balance is derived from ledger postings | `wallets/` | `wallets/__tests__/` | 2 | Planned |

## B2. Payment processing for peer-to-peer and merchant transactions with multi-currency support

| ID | Sub-requirement | Acceptance criterion | Satisfied by (code) | Verified by (test) | Phase | Status |
|---|---|---|---|---|---|---|
| B2.1 | P2P payment | Wallet-to-wallet transfer posts balanced journal in one DB transaction | `payments/` | `payments/__tests__/` | 2 | Planned |
| B2.2 | Merchant payment | Charge with a tokenised **test** card; raw PAN never persisted | `payments/`, `payments/tokenization` | `payments/__tests__/` | 2 | Planned |
| B2.3 | Multi-currency | Cross-currency payment posts FX legs through per-currency clearing accounts; each currency nets to 0 | `payments/fx` | `payments/__tests__/` | 2 | Planned |
| B2.4 | Atomicity / ledger balance | Insufficient funds → 422 with zero postings; property test: every journal sums to 0 per currency | `payments/ledger` | `payments/__tests__/` | 2 | Planned |

## B3. Transaction history, refunds, and dispute resolution workflows

| ID | Sub-requirement | Acceptance criterion | Satisfied by (code) | Verified by (test) | Phase | Status |
|---|---|---|---|---|---|---|
| B3.1 | Transaction history | Paginated; filter by status, date range, method; caller sees only own/merchant-scoped records | `payments/history` | `payments/__tests__/` | 2 | Planned |
| B3.2 | Refunds | Full and partial; cumulative refund ≤ captured amount; duplicate refund rejected | `refunds_disputes/` | `refunds_disputes/__tests__/` | 2 | Planned |
| B3.3 | Dispute workflow | State machine `opened → under_review → won \| lost`; invalid transitions rejected; `lost` posts chargeback and sets `chargeback_flag` | `refunds_disputes/` | `refunds_disputes/__tests__/` | 2 | Planned |
| B3.4 | Refund × chargeback conflict | Refund + chargeback on the same payment can never return more than the captured amount | `refunds_disputes/` | `refunds_disputes/__tests__/` | 2 | Planned |

## B4. NLP-enabled transaction failure reason detection

| ID | Sub-requirement | Acceptance criterion | Satisfied by (code) | Verified by (test) | Phase | Status |
|---|---|---|---|---|---|---|
| B4.1 | Natural-language query by support agents | NL text → typed `FailureQuery` (merchant, window, method, status) via LLM function calling; mock LLM gives the same structure | `support_assistant/` | `support_assistant/__tests__/` | 3 | Planned |
| B4.2 | Structured failure reasons | Free-text `failure_reason` classified into a taxonomy `{taxonomy_code, category, retryable, owner}`; response groups `{taxonomy_code, share, count, sample_ids}` | `support_assistant/failure_taxonomy` | `support_assistant/__tests__/` | 3 | Planned |
| B4.3 | Suggested fixes | Each reason group carries a suggested fix; observed facts, hypotheses and how to confirm are separated | `support_assistant/` | `support_assistant/__tests__/` | 3 | Planned |

## B5. RAG-enabled support assistant for payment troubleshooting using historical transaction data

| ID | Sub-requirement | Acceptance criterion | Satisfied by (code) | Verified by (test) | Phase | Status |
|---|---|---|---|---|---|---|
| B5.1 | Retrieval over historical data | Retrieves failed/flagged transactions and past incidents (`resolution_notes`) through hybrid search (B6) | `support_assistant/rag` | `support_assistant/__tests__/` | 3 | Planned |
| B5.2 | Grounded answer | Each claim maps to a cited record id; with insufficient evidence the assistant abstains instead of guessing | `support_assistant/rag` | `support_assistant/__tests__/`, `evaluation/` | 3 | Planned |

## B6. Hybrid search combining keyword and semantic retrieval over transaction records

| ID | Sub-requirement | Acceptance criterion | Satisfied by (code) | Verified by (test) | Phase | Status |
|---|---|---|---|---|---|---|
| B6.1 | Keyword retrieval | Postgres full-text search (`tsvector`) | `search/` | `search/__tests__/` | 3 | Planned |
| B6.2 | Semantic retrieval | pgvector similarity; embedder swappable (OpenAI / pinned local model), one index per embedder | `search/` | `search/__tests__/` | 3 | Planned |
| B6.3 | Fusion | Reciprocal Rank Fusion; on a fixed corpus a keyword-only miss is found by vector and vice versa | `search/` | `search/__tests__/` | 3 | Planned |

## B7. Fraud detection and basic risk scoring with flagging of suspicious transactions

| ID | Sub-requirement | Acceptance criterion | Satisfied by (code) | Verified by (test) | Phase | Status |
|---|---|---|---|---|---|---|
| B7.1 | Risk scoring | Synchronous score 0.0–1.0 with reason codes at authorisation | `fraud/` | `fraud/__tests__/` | 3 | Planned |
| B7.2 | Flagging | Fraud flag is separate from payment status (a declined payment can still be flagged) | `fraud/` | `fraud/__tests__/` | 3 | Planned |
| B7.3 | Card-testing detection | Async detector flags the planted burst exactly on a fixed fixture; false-positive rate measured on legitimate micro-payment groups | `fraud/card_testing` | `fraud/__tests__/` | 3 | Planned |

## B8. REST/gRPC APIs for merchant integrations with input validation guardrails and idempotency enforcement

| ID | Sub-requirement | Acceptance criterion | Satisfied by (code) | Verified by (test) | Phase | Status |
|---|---|---|---|---|---|---|
| B8.1 | REST API | `POST /v1/payments`, `GET /v1/payments/{id}`, refunds; merchant API key auth | `merchant_api/rest` | `merchant_api/__tests__/` | 2 | Planned |
| B8.2 | gRPC API | `CreatePayment` / `GetPayment` call the same domain service as REST | `merchant_api/grpc` | `merchant_api/__tests__/` | 2 | Planned |
| B8.3 | Input validation guardrails (API) | Invalid currency, amount, card (Luhn / non-test PAN), oversize fields → field-level errors on both REST and gRPC | `merchant_api/` | `merchant_api/__tests__/` | 2 | Planned |
| B8.4 | Idempotency enforcement | 20 concurrent requests with the same `Idempotency-Key` → exactly 1 journal and identical responses; lost-response replay returns the stored response; same key + different body → 422 | `merchant_api/idempotency` | `merchant_api/__tests__/` | 2 | Planned |
| B8.5 | Guardrails (AI side) | Tenant scope taken from auth, never from LLM output; tool allowlist + typed args; retrieved text treated as untrusted | `support_assistant/guardrails` | `support_assistant/__tests__/` | 3 | Planned |

## B9. Tool-calling support for payment status lookup and fraud analysis

| ID | Sub-requirement | Acceptance criterion | Satisfied by (code) | Verified by (test) | Phase | Status |
|---|---|---|---|---|---|---|
| B9.1 | `get_payment_status` tool | Assistant calls it with correct args for Q2 | `support_assistant/tools` | `support_assistant/__tests__/` | 3 | Planned |
| B9.2 | `analyze_fraud` tool | Assistant calls it with correct args for Q3 | `support_assistant/tools` | `support_assistant/__tests__/` | 3 | Planned |
| B9.3 | Supporting tools | `aggregate_failures` (Q1), `search_incidents` (Q1/RAG), `explain_settlement` (Q4); test asserts tool name + args per sample query | `support_assistant/tools` | `support_assistant/__tests__/` | 3 | Planned |

## B10. Memory and session management for merchant support interactions

| ID | Sub-requirement | Acceptance criterion | Satisfied by (code) | Verified by (test) | Phase | Status |
|---|---|---|---|---|---|---|
| B10.1 | Session management | Redis session with TTL; merchant A cannot read merchant B's session | `support_assistant/memory` | `support_assistant/__tests__/` | 3 | Planned |
| B10.2 | Short-term memory | Last N turns + structured conversation state; "what about yesterday?" keeps merchant + metric and shifts the window | `support_assistant/memory` | `support_assistant/__tests__/` | 3 | Planned |
| B10.3 | Long-term memory | Explicit, auditable `merchant_memory` records written only via a confirmed tool call, with source turn id; recalled in a new session; correct/delete endpoint | `support_assistant/memory` | `support_assistant/__tests__/` | 3 | Planned |

## B11. Notifications for payment confirmations and suspicious activity alerts

| ID | Sub-requirement | Acceptance criterion | Satisfied by (code) | Verified by (test) | Phase | Status |
|---|---|---|---|---|---|---|
| B11.1 | Payment confirmation | Event written to a transactional outbox in the payment DB transaction; worker delivers it; shown in UI | `notifications/`, `payments/outbox` | `notifications/__tests__/` | 2 | Planned |
| B11.2 | Suspicious activity alert | Fraud flag emits an alert through the same outbox → stream path | `notifications/` | `notifications/__tests__/` | 3 | Planned |
| B11.3 | At-least-once safety | Redelivered event creates no duplicate notification | `notifications/` | `notifications/__tests__/` | 2 | Planned |

## B12. Simple ticket routing for operational escalations

| ID | Sub-requirement | Acceptance criterion | Satisfied by (code) | Verified by (test) | Phase | Status |
|---|---|---|---|---|---|---|
| B12.1 | Escalation → ticket | Assistant escalation, fraud flag and dispute create tickets with reason | `tickets/` | `tickets/__tests__/` | 2 | Planned |
| B12.2 | Routing | Rule router → queue `{fraud, payments_ops, settlement, compliance}` + priority | `tickets/` | `tickets/__tests__/` | 2 | Planned |
| B12.3 | Lifecycle + dedupe | `open → assigned → resolved`; duplicate source event creates no second ticket | `tickets/` | `tickets/__tests__/` | 2 | Planned |

## B13. Simple front-end interface for user and merchant interaction

| ID | Sub-requirement | Acceptance criterion | Satisfied by (code) | Verified by (test) | Phase | Status |
|---|---|---|---|---|---|---|
| B13.1 | User screen | Sign-up/login with MFA, wallets, pay, history, refund request / dispute | `web/src/features/user/` | Playwright smoke | 2–3 | Planned |
| B13.2 | Merchant screen | Payments list, settlement summary, support assistant chat | `web/src/features/merchant/` | Playwright smoke | 2–3 | Planned |
| B13.3 | Support agent screen (addition) | NL failure query, flagged transactions, ticket queue | `web/src/features/support/` | Playwright smoke | 3 | Planned |

## Sample queries (must run end-to-end in the demo)

| ID | Query | Expected evidence (tools) | Expected output | Test | Phase | Status |
|---|---|---|---|---|---|---|
| SQ1 | 15% of a merchant's card payments failing this afternoon — root cause and fix | `aggregate_failures`, `search_incidents` | Failure rate vs baseline; concentration on one issuer/BIN range in the planted window; observed facts vs hypotheses vs how to confirm; ranked fixes; citations | `evaluation/__tests__/` (E2E) | 3 | Planned |
| SQ2 | International transfer declined despite sufficient funds — what triggered it | `get_payment_status` | Balance shown sufficient (rules out NSF); the rule that fired (corridor / KYC cross-border limit) with the values that tripped it; fix | `evaluation/__tests__/` (E2E) | 3 | Planned |
| SQ3 | Detect and flag micro-transaction series (card testing) | live burst via merchant API → detector, `analyze_fraud` | Flagged ids + pattern features; alert notification; ticket in `fraud` queue | `evaluation/__tests__/` (E2E) | 3 | Planned |
| SQ4 | Plain-English settlement summary: why payout differs from expected | `explain_settlement` | Typed breakdown (gross, MDR, FX, refunds, chargebacks + fees, reserve → net) verified before narration; plain-English text | `evaluation/__tests__/` (E2E) | 3 | Planned |

## Dataset

| ID | Requirement | Acceptance criterion | Satisfied by | Verified by | Phase | Status |
|---|---|---|---|---|---|---|
| D1 | Synthetic dataset, ≥ 10,000 records | Seeded, reproducible; CSV and JSON | `scripts/generate_dataset.py` | `scripts/__tests__/` | 1 | Planned |
| D2 | Minimum schema | All 16 required fields with required types and enum values | `scripts/generate_dataset.py` | `scripts/__tests__/` | 1 | Planned |
| D3 | Planted scenarios for SQ1–SQ4 | Each scenario present and detectable | `scripts/generate_dataset.py` | `scripts/__tests__/` | 1 | Planned |
| D4 | Schema extensions documented | README lists every added field | `README.md` | review | 5 | Planned |

## Deliverables

| ID | Deliverable | Satisfied by | Phase | Status |
|---|---|---|---|---|
| DL1 | Architecture diagram (PDF/JPEG) | `docs/architecture.md` → mmdc → `docs/architecture.pdf`, `.png` | 5 | Planned |
| DL2 | Design decisions and trade-offs | `docs/design.md` | 5 | Planned |
| DL3 | Executable code + README (setup, ≥ 1 E2E example) | `README.md` (all 4 sample queries with input and output) | 5 | Planned |
| DL4 | 10-minute presentation | `docs/presentation.md` | 5 | Planned |
