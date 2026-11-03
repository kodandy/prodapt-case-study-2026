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
3. **Block = decline before the provider, settled in tx1.** A blocked charge never reaches the provider and has no
   tx2: tx1 itself writes the payment as `declined` with `decline_code "risk_blocked"` and `risk_blocked = true`, the
   outbox `payment.declined` (with `provider: null`), the audit row (summary adds `"decline_code": "risk_blocked"`)
   and the stored 201 response. No `payment_attempts` row is created. Retries with the same key replay the stored
   body. (Security audit LOW-3: no crash window between the decision and its record, no provider attribution.)
4. **One card at a time** (red-team RT-001, audit MEDIUM-1). tx1 takes `pg_advisory_xact_lock` on the card
   fingerprint (`hashtextextended(fingerprint, 0)`) before counting, so concurrent charges on one card are scored
   one after another and each sees the earlier ones. Known limit: a sibling still waiting on the provider is
   counted for `card_velocity` but cannot yet count as a decline.
5. **Velocity reads the payment's own fingerprint** (audit MEDIUM-2). `payments.card_fingerprint` (the HMAC already
   stored on `card_tokens`) with index `(card_fingerprint, created_at)`; both counts come from one query bounded by
   the 60-minute window.
6. The degraded-scoring policy (velocity lookup down → `risk_unavailable` / `degraded_scoring`) is Design: velocity
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
    last `DECLINE_WINDOW = 60 minutes`, not counting risk-blocked ones (audit LOW-2: a block must not extend
    itself).
- `flagged = score >= FLAG_THRESHOLD (0.50)`; `blocked = score >= BLOCK_THRESHOLD (0.90)`. No rule hit → score
  `Decimal("0.00")`, reasons `()`.

### Charge path changes (`gateway/payments/charge.py`)

- `payments` gains `card_fingerprint` (String(64), nullable for rows before this change), `risk_score`
  (Numeric(3,2), nullable likewise), `risk_reasons` (JSONB list of str, default `[]`), `risk_flagged` and
  `risk_blocked` (bool, default false). tx1 stores the fingerprint and the assessment on the new payment.
- Blocked → no provider call, no attempt, no journal; settled in tx1 (Decision 3).
- `ChargeDeps.card_fingerprint_key` and `_Claim.card_fingerprint` are excluded from `repr` (audit LOW-6).

### Reading the score

- `get_payment_risk(session, merchant_id, payment_id) -> PaymentRiskView` (`gateway/payments/risk.py`); unknown
  payment or another merchant's → `PaymentNotFoundError` (from `gateway.payments.disputes`).
  `PaymentRiskView` (pydantic, `schemas.py`): `payment_id`, `risk_flagged: bool`, `blocked: bool`
  (the stored `risk_blocked` column, not the decline code: audit LOW-4). The score and reasons stay internal (ops side): `card_velocity` counts the
  card's payments at every merchant, so exposing reasons or the exact score to a merchant would leak other tenants'
  activity and give card testers an oracle for the thresholds (security audit M1).
- Route `GET /v1/payments/{payment_id}/risk` (Bearer API key) → 200 `PaymentRiskView`, 404
  `{"detail": "payment not found"}`, 401 without a valid key, 422 for a non-UUID id.

## B1 — login, TOTP enrol/verify, wallet balance read

What already exists (SQ2): `POST /v1/signup` (argon2id, one USD wallet), `POST /v1/user-tokens` (an RS256 token
for ops-ai, aud `ops-ai`, login semaphore and timing-equal failures). B1 adds a gateway-side session, TOTP as a second
factor and a wallet read. `/v1/user-tokens` is unchanged (it does not ask for TOTP; noted as a gap).

### Decisions

1. **Session token = RS256 JWT with `aud "gateway"`**, signed with the existing `jwt_private_key_pem`, verified by
   the gateway with the public key derived from it. Claims: `iss "gateway"`, `aud "gateway"`, `sub "user:<id>"`,
   `user_id`, `amr` (`["pwd"]` or `["pwd", "otp"]`), `iat`, `exp = iat + SESSION_TOKEN_TTL_SECONDS (900)`, `jti`.
   An ops-ai token (`aud "ops-ai"`) or a merchant token is not a session.
2. **TOTP secret encrypted at rest** with AES-256-GCM. The key comes from `GATEWAY_TOTP_ENCRYPTION_KEY` (SecretStr,
   optional, ≥ 32 chars); the AES key is `SHA-256(key)`, the nonce is 12 random bytes stored before the ciphertext,
   and the AAD is the user id bytes (a ciphertext copied to another user does not decrypt). Without the key the MFA
   routes answer 503 and login for a user with TOTP enabled answers 503.
3. **TOTP = RFC 6238, SHA-1, 6 digits, 30 s step**, accepting steps −1, 0, +1 around now. **Replay**: a code is
   accepted only if its step is greater than the last accepted step for that user (stored), so a code works once.
4. **Lockout** on the second factor: `TOTP_MAX_FAILURES = 5` consecutive wrong codes (verify or login) lock TOTP
   for `TOTP_LOCKOUT = 15 minutes`; only a success resets the counter, so after a lock expires one more wrong
   code locks again (audit M1: the budget does not refill every 15 minutes). While locked, every TOTP check fails
   with `TotpLockedError` even for a correct code. A stored secret that fails to decrypt answers 503. Password-only brute force stays bounded by the existing login
   semaphore; per-IP / per-email throttling is Design.
5. **Audit** (append-only `audit_log`, actor `user:<id>`, role `consumer`): `auth.login` (success, summary `amr`),
   `auth.login_failed` (known user only, summary `{"factor": "password" | "totp"}`), `mfa.totp_enrolled`,
   `mfa.totp_enabled`, `mfa.totp_locked`. Unknown emails write nothing (no raw email in the audit log). Secrets,
   codes and passwords are never in a summary.

### Data — `gateway.user_totp` (`gateway/accounts/models.py: UserTotp`)

`user_id` (PK, FK users), `secret_ciphertext` (bytea), `confirmed_at` (nullable; set = TOTP enabled),
`last_used_step` (bigint, nullable), `failed_attempts` (int ≥ 0, default 0), `locked_until` (nullable),
`created_at`, `updated_at`.

### `gateway/accounts/mfa.py`

- `encrypt_totp_secret(key, user_id, secret) -> bytes` / `decrypt_totp_secret(key, user_id, blob) -> str`;
  a wrong key or another user's id → `TotpDecryptError`.
- `enrol_totp(session_factory, clock, key, user_id) -> TotpEnrolment(secret: str, otpauth_uri: str)` (secret base32,
  `pyotp.random_base32()`; URI issuer `"PayPlat"`). Re-enrolling before confirmation replaces the pending secret
  (counter and lock reset); enrolling when already confirmed → `TotpAlreadyEnabledError`.
- `confirm_totp(session_factory, clock, key, user_id, code) -> None`: pending enrolment + valid code → `confirmed_at` set.
  No enrolment → `TotpNotEnrolledError`; already confirmed → `TotpAlreadyEnabledError`; wrong / replayed code →
  `InvalidTotpCodeError`; locked → `TotpLockedError`.
- `check_totp(session_factory, clock, key, user_id, code) -> None`: the login-time check on a confirmed enrolment, same
  replay / lockout rules and errors.
- `totp_enabled(session, user_id) -> bool`.
- Codes must match `^[0-9]{6}$`; anything else is a wrong code (counts as a failure).
- `enrol_totp` / `confirm_totp` / `check_totp` take the session factory and run their own transaction, so a failed
  attempt (counter, lock) is committed before the error is raised.
- All checks lock the `user_totp` row (`FOR UPDATE`) so two concurrent uses of one code cannot both pass.

### `gateway/accounts/sessions.py`

- `issue_session_token(private_key_pem, user_id, now, amr) -> str` and
  `verify_session_token(private_key_pem, token, now) -> SessionClaims(user_id: UUID, amr: tuple[str, ...])`;
  bad signature, wrong `aud` / `iss`, expired, or missing claims → `InvalidSessionError`. Algorithm pinned to RS256.

### `gateway/wallets/service.py`

- `list_wallets(session, user_id) -> list[WalletBalanceView]`: the user's `wallet` ledger accounts with their
  balance rows, ordered by currency. `WalletBalanceView`: `account_id`, `currency`, `available_minor`,
  `held_minor`.

### Routes (`GatewayAppDeps.totp_encryption_key: str | None = None`)

- `POST /v1/sessions` `{email, password, totp_code?}` → 201 `{access_token, token_type "Bearer", expires_in,
  amr}` with `Cache-Control: no-store`.
  - Unknown email or wrong password → 401 `{"detail": "invalid credentials"}` (identical, same semaphore path as
    `/v1/user-tokens`, 503 + `Retry-After: 1` when no slot).
  - TOTP enabled and `totp_code` absent → 401 `{"detail": "mfa required"}`; wrong / replayed code → 401
    `{"detail": "invalid credentials"}`; locked → 423 `{"detail": "mfa locked"}`.
  - TOTP not enabled → `amr ["pwd"]` (a supplied `totp_code` is ignored); enabled → `amr ["pwd", "otp"]`.
  - `totp_code` (if present) is a string of 1..16 chars, else 422.
- `POST /v1/me/mfa/totp` (Bearer session) → 201 `{secret, otpauth_uri}`, `Cache-Control: no-store`; 409 when
  already enabled; 503 without the encryption key.
- `POST /v1/me/mfa/totp/verify` (Bearer session) `{code}` → 200 `{"totp_enabled": true}`; 400
  `{"detail": "invalid code"}` for a wrong / replayed code; 404 `{"detail": "totp not enrolled"}`; 409 already
  enabled; 423 locked; 503 without the key.
- `GET /v1/me/wallets` (Bearer session) → 200 `{items: [WalletBalanceView]}`.
- Bearer for `/v1/me/*`: missing → 401 `{"detail": "missing session"}`; invalid / expired / wrong audience
  (an ops-ai user token, a merchant token, an API key) → 401 `{"detail": "invalid session"}`. A user with TOTP
  enabled must present a session with `"otp"` in `amr` for `/v1/me/*`, else 401 `{"detail": "mfa required"}`.

## B2 — P2P wallet transfer, per-currency wallets

Source: `docs/submission-outline.md` §2 row B2 ("P2P wallet transfer, per-currency wallets; FX conversion
**Design**"), `docs/architecture-detail.md` §3 (ledger and locking, funds hold: P2P is internal and synchronous, no
hold). A user can hold one wallet per currency; a transfer moves money between two users' wallets **in the same
currency** in one transaction. There is no FX conversion, no KYC / corridor / tier limit check (SQ2's external
transfer system and the design doc cover those) and no outbox event (B11 adds notifications).

### Decisions

1. **Same currency only.** The sender pays from their wallet in the request currency; the recipient is credited in
   that same currency. A sender with no wallet in that currency has nothing to pay with → `insufficient_funds`
   (`available_minor: 0`). A recipient with no wallet in that currency gets one created inside the transfer
   transaction (ledger account + zero balance row, both race-safe). Cross-currency is Design (FX clearing accounts,
   rate snapshot id on the journal).
2. **Sign convention.** A user wallet is a platform liability: money *into* a wallet is a credit (negative posting),
   money out is a debit (positive). For every wallet, `available_minor + held_minor == -(sum of its postings)`.
   A transfer journal (`ref_type "transfer"`, `ref_id = transfer_id`) has exactly two lines: sender wallet
   `+amount`, recipient wallet `-amount`.
3. **One lock order.** The transfer locks both balance rows with `SELECT … FOR UPDATE` in ascending `account_id`
   order, then checks the sender's `available_minor`, then moves the balances and posts the journal, all in one
   transaction. Two transfers in opposite directions between the same pair cannot deadlock; N parallel transfers
   from one wallet never overdraw it (the `available_non_negative` CHECK is the last line, not the mechanism).
4. **Idempotency per sender.** `Idempotency-Key` is required. The transfer row stores `(sender_user_id,
   idempotency_key)` unique, a request hash (recipient email normalized, amount, currency) and the 201 body. Same
   key + same request → the stored 201 body (no second debit); same key + different request → 422
   `idempotency_key_reused`. Errors are not stored (a retry after topping up can succeed). Same pattern as refunds:
   insert `ON CONFLICT DO NOTHING`, and a concurrent winner is answered from its stored row.
5. **Recipient by email.** `recipient_email` is normalized like signup (strip, lower). Unknown → 404
   `recipient_not_found`. The sender themself → 422 `self_transfer`. (Signup already answers 409 for a taken
   email, so the 404 adds no new enumeration surface; noted for the report.)
6. **Funding.** There is no external funding rail in Basic. `top_up_wallet` is a service function (not an HTTP
   route) for tests and demo seeding: it credits the wallet from the platform `topup_clearing` account with a
   balanced journal (`ref_type "top_up"`), creating the wallet if missing. A real top-up would come through a
   payment method and is Design.
7. **Audit.** One append-only `audit_log` row per created transfer: actor `user:<sender>`, role `consumer`, action
   `transfer.created`, target `transfer:<id>`, summary `{"amount_minor", "currency", "recipient_user_id"}`. No email
   in the summary. Replays write nothing.
8. **Auth.** `/v1/me/transfers` uses the B1 session dependency, so a TOTP-enabled user needs an `otp` session.
   Per-transfer step-up and velocity limits are Design.

### Data — `gateway.transfers` (`gateway/wallets/models.py: Transfer`)

`id` (uuid PK), `sender_user_id` (FK users), `recipient_user_id` (FK users), `sender_account_id` /
`recipient_account_id` (FK ledger_accounts), `amount_minor` (bigint, CHECK `> 0`, name `amount_positive`),
`currency` (3), `state` (16, always `"succeeded"` in Basic), `idempotency_key` (255), `request_hash` (64),
`response_body` (JSONB), `created_at`. Unique `(sender_user_id, idempotency_key)`; index on
`(recipient_user_id, created_at)`.

### `gateway/wallets/service.py`

- `TransferOutcome(status_code: int, body: dict[str, object])` (frozen dataclass), like `RefundOutcome`.
- `transfer(session_factory, clock, sender_user_id, idempotency_key, recipient_email, amount_minor, currency) ->
  TransferOutcome`:
  - 201 `{"transfer_id", "state": "succeeded", "amount_minor", "currency", "recipient_user_id",
    "sender_available_minor"}` (the sender's available balance in that currency after the transfer);
  - 404 `{"error": "recipient_not_found"}`; 422 `{"error": "self_transfer"}`;
  - 409 `{"error": "insufficient_funds", "available_minor": <sender's current available>}`;
  - 422 `{"error": "idempotency_key_reused"}`.
  - Validation order: replay check (a stored key answers first, even if the balance has since changed) →
    recipient → self → lock → balance.
- `top_up_wallet(session, clock, user_id, currency, amount_minor) -> uuid.UUID` (the wallet account id): ensures
  the wallet + balance row, locks the balance row, `available_minor += amount`, posts the balanced top-up journal.
  Does not commit. `amount_minor <= 0` → `ValueError`.
- `ensure_wallet(session, user_id, currency) -> uuid.UUID`: wallet ledger account + zero balance row, race-safe
  (insert `ON CONFLICT DO NOTHING`). Used by `transfer` (recipient) and `top_up_wallet`.
- `list_wallets` (B1) now also lists wallets created by transfers / top-ups (no change in behaviour, just more rows).

### Route

- `POST /v1/me/transfers` (Bearer session, `Idempotency-Key` required, same header rules as `/v1/payments`)
  body `TransferRequest {recipient_email: EmailStr-like str (1..320), amount_minor: int (1..10^12, strict int),
  currency: "USD" | "EUR" | "JPY" | "INR"}`, `extra="forbid"` → the `TransferOutcome` status and body.
  `Cache-Control: no-store` on every answer. Missing / invalid key → 400 as for payments; invalid body → 422.
  `recipient_email` rejects NUL (422, not a Postgres `DataError` 500). The replay check runs again after the
  balance locks, so a same-key retry that waited behind its own winner answers the stored 201, never a 409.
  That late answer (201 replay or key reused) rolls back, so it never leaves a recipient wallet behind.
  `transfer` itself raises `ValueError` for `amount_minor <= 0` (the route already rejects it with 422).

### Known residuals (audit, not fixed in Basic)

- Recipient e-mail is not ownership-verified at sign-up: whoever registers an address first receives transfers
  to it. Verified e-mail or a pending claim is Design.
- No per-sender rate limit, concurrency cap or `lock_timeout` on transfers; a hot balance row can hold pool
  connections. Dust spam into a wallet is possible. Step-up TOTP and daily limits are Design.
- 404 recipient_not_found plus `recipient_user_id` in the 201 body map e-mail → user id; rejected attempts are
  not audited.
- `top_up_wallet` lives in the service module (only tests call it). No DB CHECK for sender ≠ recipient and no
  balance-vs-postings reconciliation job.
