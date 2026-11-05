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

Filters (B3.1), all optional and combined with AND (and with `state`):
- `created_from`, `created_to`: ISO 8601 date-times with an offset (`2026-11-04T00:00:00Z`, `…+09:00`), compared in
  UTC as a half-open range `created_from <= created_at < created_to`. Either may be given alone. Not parseable, or
  without an offset (naive) → 400 `{"detail": "invalid created_from"}` / `{"detail": "invalid created_to"}`. Both
  given with `created_from >= created_to` → 400 `{"detail": "invalid date range"}`. The route normalises an accepted
  bound to UTC; one that cannot be expressed in UTC (`9999-12-31T23:59:59-23:59`) is also 400 `invalid created_*`,
  never 500. A `+` offset must be URL-encoded as `%2B` (a raw `+` decodes to a space and gives 400).
- `method`: one of `PAYMENT_METHODS` (`{"card"}` in Basic); anything else → 400 `{"detail": "invalid method"}`.
- `list_payments(..., created_from: datetime | None = None, created_to: datetime | None = None, method: str | None =
  None)` applies them in SQL; the route parses and validates (the service takes aware datetimes only).
- Pagination: the cursor stays keyset on `(created_at, id)` and does not carry the filters. The caller resends the
  same filters with `cursor`; every page then holds only matching rows, with none repeated or skipped. A cursor
  outside the range is not an error (that page is empty).

`get_payment(session, merchant_id, payment_id) -> PaymentSummary` (B8.1): the same summary as a `list_payments` item.
Another merchant's payment or an unknown id → `PaymentNotFoundError` (from `gateway.payments.disputes`), so the two
cannot be told apart.

Route `GET /v1/payments/{payment_id}` (Bearer API key) → 200 `PaymentSummary`; another merchant's or unknown → 404
`{"detail": "payment not found"}`; a `payment_id` that is not a UUID → 422; missing/invalid key → 401. The 200 and
the 404 carry `Cache-Control: no-store`.

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
   one after another and each sees the earlier ones. A sibling still waiting on the provider counts as a decline
   (Decision 7).
5. **Velocity reads the payment's own fingerprint** (audit MEDIUM-2). `payments.card_fingerprint` (the HMAC already
   stored on `card_tokens`) with index `(card_fingerprint, created_at)`; both counts come from one query bounded by
   the 60-minute window.
6. The degraded-scoring policy (velocity lookup down → `risk_unavailable` / `degraded_scoring`) is Design: velocity
   and persistence share one database here, so a velocity failure fails the charge.
7. **An in-flight sibling counts as a decline** (red-team R-1, user decision 2026-11-04). Before this, a burst of
   parallel charges on one card was scored while every earlier sibling was still `created`, so none counted as a
   decline and the whole burst reached the provider (20 of 20 with a 0.3 s provider; 3 when sent one by one).
   `recent_declines` now also counts the card's payments in state `created` whose `created_at` is within
   `IN_FLIGHT_WINDOW = timedelta(seconds=ATTEMPT_LEASE_SECONDS)` (30 s, inclusive of the window start) of `now`.
   Older `created` rows are stale (a crash between tx1 and tx2) and stay uncounted, so they cannot block a card
   for the whole decline window. `unknown` and `failed` stay uncounted. The trade-off: a legitimate burst of
   parallel charges on one card scores as if its in-flight siblings had declined.

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
    itself), plus earlier `created` payments with the same fingerprint inside `IN_FLIGHT_WINDOW` (Decision 7).
    `IN_FLIGHT_WINDOW` is exported from `risk.py`.
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
factor and a wallet read. `/v1/user-tokens` asks for the same second factor as `/v1/sessions` (Decision 6).

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
   with `TotpLockedError` even for a correct code. A stored secret that fails to decrypt answers 503. Password brute force on one account is
   throttled by Decision 7; per-IP throttling (credential stuffing across accounts) is Design.
5. **Audit** (append-only `audit_log`, actor `user:<id>`, role `consumer`): `auth.login` (success, summary `amr`),
   `auth.login_failed` (known user only, summary `{"factor": "password" | "totp"}`), `mfa.totp_enrolled`,
   `mfa.totp_enabled`, `mfa.totp_locked`, `auth.login_locked` (Decision 7, summary `{}`). Unknown emails write nothing (no raw email in the audit log). Secrets,
   codes and passwords are never in a summary.
6. **`/v1/user-tokens` requires TOTP once it is enabled** (user decision 2026-11-04). Before, a password alone
   issued an ops-ai token for a user with TOTP on, which bypassed MFA for the ops-ai scope. The route now runs the
   same second-factor check as `/v1/sessions`: same body field, same errors, same replay and lockout counter
   (one budget across both routes), and the same `auth.login_failed {"factor": "totp"}` audit row on a wrong code.
   A user without TOTP is unchanged: the password alone still issues the token, and a supplied `totp_code` is
   ignored. The token's claims do not change (no `amr` is added, ops-ai is untouched). The request change is
   additive: `totp_code` is optional. The TOTP-enabled flag is read after the password check succeeds, not
   before argon2, so a login racing an enrolment confirm cannot use a stale "not enabled" read (security review
   2026-11-04). A wrong password for a known user writes `auth.login_failed {"factor": "password"}`, as on
   `/v1/sessions` (user decision 2026-11-04). An ops-ai token issued before TOTP was enabled stays valid until
   it expires (at most 900 s); ops-ai tokens cannot be revoked.
7. **Per-account password throttle** (user decision 2026-11-04: per account, Postgres, 10 failures in 15
   minutes → 15 minutes of 429). One counter per known user, shared by `/v1/sessions` and `/v1/user-tokens`.
   - A wrong password for a known user adds one failure. The window starts at the first failure; a failure
     after `LOGIN_FAILURE_WINDOW = 15 minutes` starts a new window at 1.
   - The failure that reaches `LOGIN_MAX_FAILURES = 10` sets `locked_until = now + LOGIN_LOCKOUT (15 minutes)`
     and writes `auth.login_locked` (summary `{}`); that request itself still answers the identical 401.
   - While locked, the route answers 429 `{"detail": "too many attempts"}` with `Retry-After` (whole seconds
     left, at least 1) before taking a login slot or running argon2, so a correct password also gets 429 and
     the response does not reveal whether it was correct. Locked requests add no failure and write no audit.
   - The lock is checked again after the request gets a login slot, right before argon2 (security review
     2026-11-04): requests that passed the first check while waiting for a slot (the route is sync, so up to
     the threadpool width can wait) get the same 429 once the lock exists and run no argon2.
   - A request that finishes argon2 after another request locked the account gets the same 429, whatever its
     password: its failure is not counted or audited, and a correct password does not reset the lock or get a
     token (security review 2026-11-04). The row is read under a row lock for this decision.
   - After the lock expires the counter starts again from 0 (a new window); a password success resets it.
   - The failure count and the `auth.login_failed` audit row are written in one transaction. A success resets
     the row only when it has failures, so a login with no prior failures writes nothing extra.
   - Unknown emails are not throttled (there is no account to count against) and still write nothing.
   - Residuals: a 429 reveals that the email is registered (`/v1/signup` already does). Each single answer is
     the same for an unknown and a known email, but an attacker who sends 11 wrong passwords sees a known email
     turn 401 → 429 while an unknown one stays 401 (red-team RT-001, LOW; only per-IP throttling, which is
     Design, removes it). A wrong password for a known email also writes the throttle and audit rows, which an
     unknown email does not; the response-time difference this could leak has not been measured. Requests
     already past the slot re-check when the lock starts run argon2 (at most `LOGIN_CONCURRENCY` per app process) but are
     answered 429; anyone who knows the email can keep the account locked by sending 10 wrong passwords every
     15 minutes; replacing a password (`set_password`) does not clear the throttle row.

### Data — `gateway.login_throttle` (`gateway/accounts/models.py: LoginThrottle`)

`user_id` (PK, FK users), `failed_attempts` (int ≥ 0, default 0), `window_started_at` (nullable),
`locked_until` (nullable), `updated_at`. Added by an additive migration; no existing table changes.

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
  - Account throttled (Decision 7) → 429 `{"detail": "too many attempts"}` + `Retry-After`; same on
    `/v1/user-tokens`.
  - TOTP enabled and `totp_code` absent → 401 `{"detail": "mfa required"}`; wrong / replayed code → 401
    `{"detail": "invalid credentials"}`; locked → 423 `{"detail": "mfa locked"}`.
  - TOTP not enabled → `amr ["pwd"]` (a supplied `totp_code` is ignored); enabled → `amr ["pwd", "otp"]`.
  - `totp_code` (if present) is a string of 1..16 chars, else 422.
- `POST /v1/user-tokens` `{email, password, totp_code?}` → 201 `{access_token, token_type, expires_in}` as before
  (Decision 6). Password failures keep the identical 401 and the same semaphore; a known user's wrong password
  writes `auth.login_failed {"factor": "password"}` as on `/v1/sessions`. For a user with TOTP enabled:
  `totp_code` absent → 401 `{"detail": "mfa required"}`; wrong / replayed code → 401
  `{"detail": "invalid credentials"}`; locked → 423 `{"detail": "mfa locked"}`; no encryption key, or a secret
  that fails to decrypt → 503. `totp_code` field rules as for `/v1/sessions`; other extra fields are still 422.
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
  - 201 `{"transfer_id", "state": "succeeded", "amount_minor", "currency", "sender_available_minor"}` (the
    sender's available balance in that currency after the transfer). No recipient user id (web P-1); a replay
    of a row stored before that change answers only these keys (an allowlist over the stored body);
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
- 404 recipient_not_found tells a signed-in sender whether an e-mail is registered; rejected attempts are not
  audited. (The 201 body no longer carries `recipient_user_id`.)
- `top_up_wallet` lives in the service module (only tests call it). No DB CHECK for sender ≠ recipient and no
  balance-vs-postings reconciliation job.

## B11 — payment confirmation and suspicious-activity notification records

Thin: records only (an in-app table plus a log line and an outbox event to the stream). No delivery channel, no
read route, no transfer notifications (Design).

### Decisions

1. **Fed by the outbox, not by the charge transaction.** The charge path is unchanged: no new synchronous rows in
   tx1/tx2. A notifier pass reads `gateway.outbox` rows on `gw.payments.v1` that it has not processed yet (id order)
   and writes the notification records. This is the "outbox → consumer" shape of architecture-detail §3/§4.
2. **Rules.** For a `PaymentEventV1` with `origin = "live"`:
   - `event_type = "payment.succeeded"` → one `payment_confirmation` for the merchant;
   - the payment row has `risk_flagged` or `risk_blocked` → one `suspicious_activity` ("was this you?") for the
     merchant, carrying only whether it was blocked: score and reasons draw on the card's activity at every
     merchant and stay internal (as in `get_payment_risk`). A flagged success gets both; a risk-blocked decline gets only the
     alert; a plain decline gets nothing.
   - `origin = "backfill"` → nothing (side-effect consumers skip backfill), but the event is still marked processed.
   The recipient is the merchant: a card payer has no gateway account in Basic.
3. **Exactly one record per (source event, kind).** Processing marks the source `event_id` in
   `gateway.notification_processed_events` (PK) inside the same transaction as the records; the insert is
   `ON CONFLICT DO NOTHING RETURNING`, so a concurrent or repeated pass skips an event another pass already took.
   `notifications` also has `UNIQUE (source_event_id, kind)`.
4. **Deterministic ids.** `notification_id = uuid5(NOTIFICATION_NAMESPACE, f"{source_event_id}:{kind}")`; the
   outbox event id is `uuid5(NOTIFICATION_NAMESPACE, f"event:{notification_id}")`. Re-deriving never creates a second
   record or event.
5. **Out to the stream.** Each new record writes one outbox row on `gw.notifications.v1` (`NotificationEventV1`,
   additive in `contracts/events.py`) in the same transaction, and logs one INFO line with ids and kind only.
   Pseudonymous ids only: no e-mail, PAN, BIN or last4 in the record, the event or the log.
6. `occurred_at` of the notification event = the source event's `occurred_at`; `created_at` = the injected clock.

### Contracts (`contracts/src/contracts/events.py`, additive)

- `NOTIFICATIONS_TOPIC = "gw.notifications.v1"`.
- `NotificationKind = Literal["payment_confirmation", "suspicious_activity"]`.
- `NotificationEventV1` (frozen, `extra="forbid"`): `event_id`, `event_type: Literal["notification.created"]`,
  `occurred_at`, `origin: EventOrigin = "live"`, `notification_id`, `kind: NotificationKind`, `source_event_id`,
  `recipient_type: Literal["merchant"]`, `recipient_id`, `payment_id`, `amount_minor >= 0`, `currency` (3 chars).

### Data

- `gateway.notifications` (`gateway/notifications/models.py: Notification`): `id` uuid PK, `kind` (32),
  `recipient_type` (16), `recipient_id` uuid, `source_event_id` uuid, `payment_id` uuid FK `payments.id`,
  `channel` (16, `"in_app"`), `payload` JSONB, `created_at`. `UNIQUE (source_event_id, kind)`, index
  `(recipient_id, created_at)`. Payload: `{amount_minor, currency}` for a confirmation; `{amount_minor, currency,
  blocked}` for an alert.
- `gateway.notification_processed_events`: `event_id` uuid PK, `processed_at`.

### `gateway/notifications/service.py`

- `process_pending_notifications(session_factory, clock, batch_size=100) -> int`: one transaction; selects up to
  `batch_size` unprocessed payment outbox rows (anti-join on the processed table, ordered by outbox id), applies the
  rules, returns the number of notification records created. An outbox payload that does not validate as
  `PaymentEventV1`, or whose payment row is missing, is marked processed with no record and a WARNING log (ids
  only), so one bad row cannot stall the pass. So is an event whose `event_id` differs from its outbox row's or
  whose `merchant_id` differs from the payment row's; recipient, amount and currency come from the payment row.
- The notification outbox insert is `ON CONFLICT (event_id) DO NOTHING`, so a re-derived event id left over from an
  earlier run cannot fail the pass.
- The migration marks every existing `gw.payments.v1` outbox event processed, so the first run on an existing
  database does not notify history.
- `notification_id(source_event_id, kind) -> uuid.UUID` (Decision 4).

### `gateway/notifications/worker.py`

- `run_notifier(session_factory, clock, stop, poll_interval_seconds=0.2) -> int`: same loop contract as
  `run_relay` (DB errors logged by class name and retried; returns the total created). `main()` wires settings.

### Known residuals

- Every poll re-scans the payment outbox history with an anti-join (no cursor, no `(topic, id)` index, no
  retention for `outbox`, `notifications` or the processed table).
- The worker sleeps when a pass creates no records rather than when it processes no events, so a long run of
  plain declines is drained at one batch per poll interval.
- A second, forged `gw.payments.v1` row for a payment (new event id, matching merchant) creates a second
  confirmation: the outbox is trusted, and only the charge writes it, once per payment. No DB role separation.
- No CHECK constraints on `kind`, `recipient_type` and `channel`; parallel notifiers repeat each other's reads.
- The recipient of the "was this you?" alert is the merchant, not the card holder (no payer account in Basic).
- No compose service runs `python -m gateway.notifications.worker`, and there is no read route.

## B1.4 / B2.3 / B2.5 / P0-2 / P0-3 — wallet routes, FX, external-rail payouts, ledger properties

Source: worker brief `gw-wallets-fx` (11-05), `docs/architecture-detail.md` §3 (ledger and locking, funds hold,
P2P / international transfer). This section supersedes B2 Decision 1 ("same currency only") and Decision 6 ("no
top-up route"); everything else in B2 still holds. Supported currencies stay `USD | EUR | JPY | INR` on every
route (`GBP` and others → 422). Minor-unit exponents: `JPY` 0, the others 2.

### Decisions

1. **Balances are proven equal to postings.** The `account_balances` row stays the lockable read model (B1
   `list_wallets` is unchanged). A hold is booked on the ledger, so both numbers derive from postings:
   `available_minor == -(sum of postings on the user's "wallet" account)` and
   `held_minor == -(sum of postings on the user's "wallet_hold" account)` in that currency.
   `ledger_wallet_balances(session, user_id)` computes them from postings in the same shape as `list_wallets`;
   tests assert the two are equal after every operation (P0-2).
2. **FX rate table.** `gateway.fx_rates`: `id` (string PK, e.g. `demo-USDEUR-v1`), `base_currency`,
   `quote_currency`, `rate` (`Numeric(20, 10)`, CHECK `> 0`), `valid_from` (timestamptz). One row per ordered pair
   of the four currencies (12 rows), seeded by the migration with fixed demo rates, `valid_from`
   2026-01-01T00:00Z. Demo rates per 1 USD: EUR 0.92, JPY 151.5, INR 83.25; cross rates are derived from those
   and rounded to 10 decimals. The rate in force at `now` is the row with the latest `valid_from <= now`.
3. **Conversion.** `target_minor = floor(source_minor × rate × 10^(exp_target − exp_source))` (Decimal, never
   float). A result of 0 → `FxAmountTooSmallError`. Rounding residue stays with the platform; each currency still
   nets to zero because the clearing accounts absorb exactly the posted amounts.
4. **FX journal shape.** One journal, four lines, `fx_rate_id` set on the journal row:
   `debit_account (source ccy) +source`, `fx_clearing (source ccy) −source`, `fx_clearing (target ccy) +target`,
   `credit_account (target ccy) −target`. `fx_clearing` is a platform account (`owner_type "platform"`,
   `kind "fx_clearing"`), one per currency, append-only (no balance row). Same-currency journals have
   `fx_rate_id NULL`.
5. **Cross-currency P2P transfer.** `TransferRequest` gains optional `recipient_currency` (same literal set). Absent
   or equal to `currency` → the B2 same-currency transfer, byte-for-byte unchanged (body, request hash, audit
   summary, journal). Different → the sender's `currency` wallet is debited `amount_minor`, the recipient's
   `recipient_currency` wallet (created if missing) is credited the converted amount, journal `ref_type
   "transfer"` with the FX shape above (`debit = sender wallet`, `credit = recipient wallet`). Same locks, order,
   idempotency and error rules as B2. Insufficient funds → 409 and nothing moves.
6. **Cross-currency card charge.** `ChargeRequest` gains optional `settlement_currency` (same literal set). Absent
   or equal to `currency` → unchanged. Different → the provider still charges `amount_minor` in `currency`; on
   `succeeded` the payment journal uses the FX shape (`debit = provider_receivable (currency)`,
   `credit = merchant_payable (settlement_currency)`). The pair and amount are checked before anything is
   claimed: no rate → 422 `{"error": "fx_rate_unavailable"}`, converted amount 0 → 422
   `{"error": "fx_amount_too_small"}`, in both cases no payment row and no provider call. The charge response body
   is unchanged. A declined charge posts nothing.
7. **Top-up (demo funding source).** `POST /v1/me/top-ups` credits a wallet from `topup_clearing` with the
   existing `top_up` journal (`ref_id = top_up_id`). It is off unless `GatewayAppDeps.demo_top_up_enabled` is
   true (env `GATEWAY_DEMO_TOP_UP_ENABLED`, default false; compose sets it true for the demo stack). Off → 404
   `{"detail": "Not Found"}`. Max `amount_minor` per top-up `MAX_TOP_UP_MINOR = 10_000_000`. Idempotent per user
   like transfers.
8. **External-rail payouts (B2.5).** `bank_transfer` and `upi` payouts leave the platform through a rail, so they
   hold first:
   - tx1 (one transaction, balance row locked `FOR UPDATE`): replay check → balance check → `available −= A`,
     `held += A`, payout row `pending`, journal `ref_type "payout_hold"`: `wallet +A`, `wallet_hold −A`, audit
     `payout.created`.
   - The rail is called outside any transaction with the payout id as its idempotency key. A raised
     `TimeoutError` / `ConnectionError` counts as `unknown`.
   - tx2 = `settle_payout(...)` (also the entry point a reconciler or rail callback would use later): locks the
     payout row `FOR UPDATE`, then the balance row.
     - `succeeded` → capture: `held −= A`, journal `"payout_capture"`: `wallet_hold +A`,
       `payout_clearing (platform, currency) −A`; state `succeeded`.
     - `failed` → release: `held −= A`, `available += A`, journal `"payout_release"`: `wallet_hold +A`,
       `wallet −A`; state `failed`, `failure_code` stored.
     - `unknown` → state `unknown`, no journal, funds stay held.
     - A payout already `succeeded` or `failed` is not changed by any later settle (duplicate or contradictory
       capture / release is a no-op: no journal, no balance change, the current view is returned).
   - Capture never takes more than was held (it moves exactly `A` from `wallet_hold`).
9. **Locking (P0-3).** Every operation that checks and debits a user balance (transfer, payout hold) locks the
   user balance rows with `SELECT … FOR UPDATE` in ascending `account_id` order before the check, in the same
   transaction as the journal. Platform accounts (`topup_clearing`, `fx_clearing`, `payout_clearing`,
   `provider_receivable`) take append-only postings and are never locked. N parallel payouts (or transfers) from
   one wallet never overdraw it.

### Data

- `gateway.fx_rates` (`gateway/payments/fx/models.py: FxRate`) as in Decision 2; unique
  `(base_currency, quote_currency, valid_from)`.
- `gateway.journal_entries.fx_rate_id` (string, nullable, FK `fx_rates.id`) (`JournalEntry.fx_rate_id`).
- `gateway.transfers` gains nullable `recipient_currency` (3), `recipient_amount_minor` (bigint), `fx_rate_id`
  (FK `fx_rates.id`); all NULL for same-currency transfers.
- `gateway.wallet_top_ups` (`gateway/wallets/models.py: WalletTopUp`): `id` uuid PK, `user_id` FK users,
  `account_id` FK ledger_accounts, `amount_minor` bigint CHECK `> 0`, `currency`, `idempotency_key` (255),
  `request_hash` (64), `response_body` JSONB, `created_at`; unique `(user_id, idempotency_key)`.
- `gateway.payouts` (`gateway/wallets/models.py: Payout`): `id` uuid PK, `user_id` FK users, `account_id` FK
  ledger_accounts (the wallet), `method` (16), `amount_minor` bigint CHECK `> 0`, `currency`,
  `destination_masked` (16), `state` (16: `pending | succeeded | failed | unknown`), `failure_code` (64, null),
  `idempotency_key` (255), `request_hash` (64), `created_at`, `updated_at`; unique `(user_id, idempotency_key)`.
  The raw destination is never stored: `destination_masked` is `"****"` + its last 4 characters.

### `gateway/payments/fx/service.py`

- `CURRENCY_EXPONENTS: dict[str, int]`, `FX_CLEARING_KIND = "fx_clearing"`.
- `convert_minor(amount_minor, rate: Decimal, source_currency, target_currency) -> int` (Decision 3, pure; 0 is
  returned, not raised).
- `FxQuote(rate_id, source_currency, target_currency, rate: Decimal, source_minor, target_minor)` (frozen).
- `quote(session, source_currency, target_currency, amount_minor, now) -> FxQuote`; same currency → `ValueError`;
  no rate in force → `FxRateNotFoundError`; converted 0 → `FxAmountTooSmallError`.
- `fx_lines(session, quote, debit_account_id, credit_account_id) -> list[PostingLine]` (Decision 4, four lines).
- `post_journal(..., fx_rate_id: str | None = None)` in `gateway/payments/ledger/service.py` stores the id.

### `gateway/wallets/service.py` (additions)

- `SUPPORTED_CURRENCIES = ("EUR", "INR", "JPY", "USD")`, `HOLD_KIND = "wallet_hold"`.
- `create_wallet(session, user_id, currency) -> tuple[WalletBalanceView, bool]` (`True` when created now).
- `ledger_wallet_balances(session, user_id) -> list[WalletBalanceView]` (Decision 1, same ordering as
  `list_wallets`, one item per wallet account that has a balance row).
- `top_up(session_factory, clock, user_id, idempotency_key, currency, amount_minor) -> TopUpOutcome` → 201
  `{"top_up_id", "currency", "amount_minor", "available_minor"}`; same key + same request → the stored 201 body;
  same key + different request → 422 `{"error": "idempotency_key_reused"}`. Audit `wallet.top_up`.
- `transfer(..., recipient_currency: str | None = None)`: Decision 5. A cross-currency 201 body adds
  `"recipient_currency"`, `"recipient_amount_minor"`, `"fx_rate_id"`; 422 `{"error": "fx_amount_too_small"}` /
  `{"error": "fx_rate_unavailable"}` before any lock. The request hash includes `recipient_currency` only when it
  differs from `currency`. The audit summary adds the same three keys for a cross-currency transfer.
- Transfer history (`list_transfers`): for the `received` side of a cross-currency transfer, `amount_minor` /
  `currency` are the recipient's (credited) amount and currency; the `sent` side shows what the sender paid.

### `gateway/payments/rails/`

- `RailResult(status: Literal["succeeded", "failed", "unknown"], failure_code: str | None = None)` (frozen).
- `RailPort` (Protocol): `name: str`; `submit(payout_id: str, method: str, amount_minor: int, currency: str,
  destination: str) -> RailResult`.
- `SimulatedRail`: destination containing `"fail"` → `failed` / `"rail_rejected"`; containing `"unknown"` →
  `unknown`; otherwise `succeeded`. Repeating a payout id returns the first answer (keeps a dict).

### `gateway/wallets/payouts.py`

- `PayoutDeps(session_factory, clock, rail: RailPort)` (frozen).
- `PayoutView(payout_id, state, method, amount_minor, currency, destination_masked, failure_code)` (pydantic).
- `PayoutOutcome(status_code, body)`: 201 when the state is `succeeded` or `failed`, 202 when `pending` or
  `unknown`; 409 `{"error": "insufficient_funds", "available_minor"}` (nothing stored, nothing moved); 422
  `{"error": "idempotency_key_reused"}`. `body` is `PayoutView` dumped in JSON mode on success.
- `create_payout(deps, user_id, idempotency_key, method, amount_minor, currency, destination) -> PayoutOutcome`
  (Decision 8). A same-key same-request replay returns the payout's current view (no second hold, no second rail
  call). The request hash covers method, amount, currency and destination.
- `settle_payout(session_factory, clock, payout_id, result: RailResult) -> PayoutView` (Decision 8 tx2);
  unknown id → `PayoutNotFoundError`.
- `get_payout(session, user_id, payout_id) -> PayoutView`; another user's or unknown → `PayoutNotFoundError`.

### Routes (all Bearer session via `session_user`, `Cache-Control: no-store` on every answer)

- `POST /v1/me/wallets` `{currency}` `extra="forbid"` → 201 `WalletBalanceView` when created, 200 when it already
  existed.
- `POST /v1/me/top-ups` (Idempotency-Key required, same header rules as transfers) `{currency, amount_minor:
  StrictInt 1..MAX_TOP_UP_MINOR}` `extra="forbid"` → `TopUpOutcome`; disabled → 404.
- `POST /v1/me/transfers` body adds optional `recipient_currency`.
- `POST /v1/me/payouts` (Idempotency-Key required) `{method: "bank_transfer" | "upi", amount_minor: StrictInt
  1..10^12, currency, destination: str 4..64 matching ^[A-Za-z0-9@._:-]+$}` `extra="forbid"` → `PayoutOutcome`.
- `GET /v1/me/payouts/{payout_id}` → 200 `PayoutView`; unknown or another user's → 404
  `{"detail": "payout not found"}`.
- `POST /v1/payments` body adds optional `settlement_currency` (Decision 6).
- `GatewayAppDeps` gains `rail: RailPort | None = None` (None → `SimulatedRail()`) and
  `demo_top_up_enabled: bool = False`.

### P0-2 property test (`gateway/payments/ledger/__tests__/test_ledger_sequences.py`)

Hypothesis generates sequences (up to ~12 steps) over 2–3 users and the four currencies of: top-up, same-currency
and cross-currency transfer, card charge (approve / decline, with or without `settlement_currency`), partial
refund of a succeeded charge, payout with a scripted rail answer (`succeeded | failed | unknown`) and a later
settle of an `unknown` payout. After each sequence: every journal sums to zero per currency; every journal with
postings in two currencies carries an `fx_rate_id`; for every user `list_wallets == ledger_wallet_balances`, all
balances ≥ 0.

### Known residuals

- Refunds and dispute losses of a cross-currency card charge are booked in the charge currency on
  `merchant_payable` (still balanced per currency); reversing the FX leg is Design.
- No reconciler drives `unknown` payouts; `settle_payout` is the entry point it would call.
- Demo rates are static; rate refresh, spreads and fees on FX are Design.
