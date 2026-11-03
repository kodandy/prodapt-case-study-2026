# Gateway Thin Basic items — test specification (11-04)

Source: `docs/submission-outline.md` §2 ("Target by 11-05"), `docs/architecture-detail.md` §3 (ledger and locking,
state machines, history, risk scoring, identity, audit, notifications). Scope: `services/gateway/` only. Depth: Thin
(tests, not in the live walkthrough).

Shared fixtures: root `conftest.py` (`session_factory`, `redis_client`, `clock` = `FixedClock(TEST_NOW)`,
`TEST_API_KEY_PEPPER`, `TEST_CARD_FINGERPRINT_KEY`). Card charges in tests go through
`gateway.payments.charge.charge_card` with `SimulatorProvider`; `4242424242424242` approves, `4000000000000002` declines.

Ledger sign convention (unchanged): debit > 0, credit < 0, every journal sums to zero per currency. A card charge
posts `provider_receivable +amount / merchant_payable −amount`.

## B3 — history, refunds, disputes

### Decisions

1. **Running totals on the payment row.** `payments` gains `refunded_minor`, `disputed_minor` and `chargeback_minor`
   (BigInteger, NOT NULL, server default 0). A DB check keeps each ≥ 0 and
   `refunded_minor + disputed_minor + chargeback_minor <= amount_minor`. `refundable = amount − refunded − disputed −
   chargeback`. An open dispute reserves its amount, so a refund cannot take money a lost dispute would later claw back.
2. **One lock.** Refunds and dispute changes take `SELECT … FOR UPDATE` on the payment row before reading the totals,
   so concurrent refunds never exceed the captured amount.
3. **Refunds are synchronous in Thin.** The simulator has no refund API; a refund is recorded `succeeded` with its
   reversal journal in the same transaction. `requested → failed` against a provider is Design.
4. **Who moves a dispute.** Opening (`open_dispute`) and the outcome (`resolve_dispute`) are operator/network-side
   functions with no HTTP route (the simulator / `ops_lead` stand-in). The merchant can only read a dispute and submit
   evidence.
5. **No stream events for refunds and disputes.** The ops-ai consumer validates `gw.payments.v1` with
   `extra="forbid"` and a closed `event_type`; new event types there would dead-letter. Audit rows record every
   refund and dispute transition.

### History — `gateway/payments/history.py`

- `list_payments(session, merchant_id, limit=20, cursor: str | None = None, state: PaymentState | None = None) ->
  PaymentPage`. `PaymentPage(items: list[PaymentSummary], next_cursor: str | None)` (frozen dataclass).
  - Only the merchant's own payments. Order: `created_at` DESC, then `id` DESC.
  - `next_cursor` is an opaque string when more rows exist after this page, else None. Passing it back returns the
    next page with no row repeated or skipped (keyset, not offset).
  - A cursor that does not decode → `InvalidCursorError`.
- `PaymentSummary` (pydantic, `gateway/payments/schemas.py`): `payment_id`, `state`, `amount_minor`, `currency`,
  `method`, `card: CardSummary | None` (BIN + last4 from the token; never the PAN or the fingerprint),
  `decline_code`, `refunded_minor`, `disputed_minor`, `chargeback_minor`, `created_at`.
- Index `(merchant_id, created_at, id)` on `payments`.

Route `GET /v1/payments` (Bearer API key) query `limit` (1–100, default 20; outside → 422), `cursor`, `state`
(one of the payment states; else 422) → 200 `{"items": [...], "next_cursor": str | null}`. Missing/invalid key → 401.
Invalid cursor → 400 `{"detail": "invalid cursor"}`.

### Refunds — `gateway/payments/refunds.py`

Table `refunds`: `id`, `payment_id` (FK), `merchant_id` (FK), `amount_minor`, `currency`, `state` (`succeeded`),
`idempotency_key` (≤255), `request_hash`, `response_body` (JSONB), `created_at`; unique `(merchant_id,
idempotency_key)`.

`refund_payment(session_factory, clock, merchant_id, payment_id, idempotency_key, amount_minor: int | None) ->
RefundOutcome(status_code: int, body: dict)`. One transaction:
- Payment missing or owned by another merchant → 404 `{"error": "payment_not_found"}`.
- Same key already used by this merchant: same `(payment_id, amount_minor)` → the stored 201 body again, no second
  journal; different → 422 `{"error": "idempotency_key_reused"}`.
- Payment state ≠ `succeeded` → 409 `{"error": "payment_not_refundable"}`.
- `amount_minor` None → the full refundable amount. Amount > refundable (or refundable = 0) → 409
  `{"error": "refund_exceeds_refundable", "refundable_minor": n}`.
- Error responses are not stored under the key.
- Success → 201 `{"refund_id", "payment_id", "state": "succeeded", "amount_minor", "currency",
  "payment_refunded_minor", "payment_refundable_minor"}`; `payments.refunded_minor += amount`; journal
  `ref_type "refund"`, `ref_id` = refund id: `merchant_payable +amount / provider_receivable −amount`; audit row
  `action "payment.refund"`, `actor_id "merchant:<id>"`, `target "payment:<id>"`.
- Concurrency: N parallel refunds with different keys on one payment never refund more than its amount in total.

Route `POST /v1/payments/{payment_id}/refunds` (Bearer API key, `Idempotency-Key` required → else 400) body
`{"amount_minor": int > 0, <= MAX_AMOUNT_MINOR}` (optional; extra fields 422) → the outcome status and body.
A `payment_id` that is not a UUID → 422.

### Disputes — `gateway/payments/disputes.py`

State machine: `opened → under_review → won | lost`. Any other move → `InvalidDisputeTransitionError`.

Table `disputes`: `id`, `payment_id` (FK), `merchant_id` (FK), `amount_minor`, `currency`, `reason` (≤64), `state`,
`evidence_text` (nullable), `chargeback_fee_minor` (default 0), `created_at`, `updated_at`, `resolved_at`
(nullable). At most one dispute per payment in `opened | under_review` (partial unique index).

`DisputeView` (pydantic): `dispute_id`, `payment_id`, `state`, `amount_minor`, `currency`, `reason`,
`chargeback_fee_minor`, `created_at`, `updated_at`.

- `open_dispute(session_factory, clock, payment_id, reason, amount_minor: int | None = None) -> DisputeView`:
  amount None → the payment's refundable amount. Errors: `PaymentNotFoundError`; `PaymentNotDisputableError` (state ≠
  `succeeded`); `DisputeAlreadyOpenError`; `DisputeAmountExceedsError` (amount ≤ 0 or > refundable). Effect: state
  `opened`, `payments.disputed_minor += amount`, audit `dispute.opened`.
- `submit_evidence(session_factory, clock, merchant_id, dispute_id, text) -> DisputeView`: `opened → under_review`,
  stores the text. Unknown dispute or another merchant's → `DisputeNotFoundError`. Audit `dispute.evidence_submitted`.
- `resolve_dispute(session_factory, clock, dispute_id, outcome: Literal["won", "lost"], actor_id: str) ->
  DisputeView`: `under_review → outcome`, `resolved_at = now`, `disputed_minor −= amount`.
  - `won`: no journal.
  - `lost`: `chargeback_minor += amount`, `chargeback_fee_minor = CHARGEBACK_FEE_MINOR[currency]`; one journal
    `ref_type "dispute"`, `ref_id` = dispute id, lines `merchant_payable +amount / provider_receivable −amount` and
    `merchant_payable +fee / platform chargeback_fee_revenue −fee`. Balanced per currency.
  - Resolving twice → `InvalidDisputeTransitionError`, no second journal. Audit `dispute.resolved` with the outcome.
- `CHARGEBACK_FEE_MINOR = {"USD": 1500, "EUR": 1500, "JPY": 2000, "INR": 120000}`.

Routes (Bearer API key): `GET /v1/disputes/{dispute_id}` → 200 `DisputeView` or 404 for unknown / another merchant's;
`POST /v1/disputes/{dispute_id}/evidence` body `{"text": 1–4000 chars}` → 200 `DisputeView`, 404, or 409
`{"detail": "invalid dispute transition"}`.

### Input hardening (red-team B3, RT-001..004)

- `Idempotency-Key` on `POST /v1/payments` and `POST /v1/payments/{payment_id}/refunds`: only visible ASCII
  (`0x21`–`0x7e`), 1–255 chars. Anything else (space, control char, non-ASCII) → 400
  `{"detail": "invalid Idempotency-Key header"}`, nothing stored. Missing stays 400 "Idempotency-Key header is
  required".
- `RefundRequest.amount_minor` is a strict int: `true`, `"100"`, `100.0` → 422. Omitted (no body, `{}`) → full
  refund; an explicit `"amount_minor": null` or a JSON `null` body → 422.
- `open_dispute` reason: 1–64 chars, no control/format characters (Unicode Cc/Cf), not blank →
  else `InvalidDisputeReasonError` (from `gateway.payments.disputes`) before any DB write.
- `EvidenceRequest.text`: Unicode Cc/Cf characters are rejected except `\n`, `\r`, `\t` (zero-width, bidi
  overrides, other controls → 422); at least one character must be neither whitespace nor Cc/Cf.

## B7.1 — rule-based risk score at charge time

### Decisions

1. **Rules only, no LLM on the payment path** (architecture-detail §3 "Risk scoring"). Velocity comes from Postgres
   over the card fingerprint, in the same transaction as the claim (tx1).
2. **Stored on the payment, read through its own view.** `ChargeResponse`, the stored idempotent body and
   `PaymentSummary` (B3 history) stay as they are; the score is read through `GET /v1/payments/{payment_id}/risk`.
3. **Block = decline before the provider.** A blocked charge never reaches the provider. It finalizes as `declined`
   with `decline_code "risk_blocked"` through the normal tx2 (outbox `payment.declined`, audit, stored 201 response),
   so retries with the same key behave as for any decline.
4. The degraded-scoring policy (velocity lookup down → `risk_unavailable` / `degraded_scoring`) is Design: velocity
   and persistence share one database here, so a velocity failure fails the charge.

### `gateway/payments/risk.py`

- `assess_charge_risk(session, now, card_fingerprint, amount_minor, currency) -> RiskAssessment`.
  `RiskAssessment(score: Decimal, reasons: tuple[str, ...], flagged: bool, blocked: bool)` (frozen dataclass).
- Rules (weights add up, capped at `Decimal("1.00")`; `reasons` in this order):
  - `high_amount` +0.40: `amount_minor >= HIGH_AMOUNT_MINOR[currency]`,
    `HIGH_AMOUNT_MINOR = {"USD": 500_000, "EUR": 500_000, "JPY": 500_000, "INR": 40_000_000}`.
  - `card_velocity` +0.40: at least `VELOCITY_LIMIT = 3` earlier payments with the same card fingerprint (any
    merchant) created in the last `VELOCITY_WINDOW = 10 minutes` (inclusive of the window start).
  - `recent_declines` +0.30: at least `DECLINE_LIMIT = 2` earlier `declined` payments with the same fingerprint in the
    last `DECLINE_WINDOW = 60 minutes`.
- `flagged = score >= FLAG_THRESHOLD (0.50)`; `blocked = score >= BLOCK_THRESHOLD (0.90)`. No rule hit → score
  `Decimal("0.00")`, reasons `()`.

### Charge path changes (`gateway/payments/charge.py`)

- `payments` gains `risk_score` (Numeric(3,2), nullable for rows before this change), `risk_reasons` (JSONB list of
  str, default `[]`), `risk_flagged` (bool, default false). tx1 stores the assessment on the new payment.
- Blocked → no provider call; the payment ends `declined`, `decline_code "risk_blocked"`, no journal.

### Reading the score

- `get_payment_risk(session, merchant_id, payment_id) -> PaymentRiskView` (`gateway/payments/risk.py`); unknown
  payment or another merchant's → `PaymentNotFoundError` (from `gateway.payments.disputes`).
  `PaymentRiskView` (pydantic, `schemas.py`): `payment_id`, `risk_flagged: bool`, `blocked: bool`
  (`decline_code == "risk_blocked"`). The score and reasons stay internal (ops side): `card_velocity` counts the
  card's payments at every merchant, so exposing reasons or the exact score to a merchant would leak other tenants'
  activity and give card testers an oracle for the thresholds (security audit M1).
- Route `GET /v1/payments/{payment_id}/risk` (Bearer API key) → 200 `PaymentRiskView`, 404
  `{"detail": "payment not found"}`, 401 without a valid key, 422 for a non-UUID id.
