# Compliance, roles and audit (P0-9, P0-10, R2-10)

Scope: RBAC policy table and ops principals (P0-9), append-only audit enforced by grants and trigger (P0-10),
AML/KYC re-verification triggers and GDPR data-subject requests (R2-10). Tests are written from this file.

## 1. Roles and the policy table (`gateway.accounts.rbac`)

```python
Role = Literal["consumer", "merchant_admin", "merchant_support", "ops_agent", "ops_lead"]
ROLES: tuple[Role, ...]                  # the five, in this order
PUBLIC = "public"
POLICY: Mapping[str, frozenset[Role]]    # action -> roles allowed
ROUTE_ACTIONS: Mapping[str, str]         # "METHOD /path-template" -> action or PUBLIC
def is_allowed(role: str, action: str) -> bool   # unknown role or action -> False
def action_for(method: str, path_template: str) -> str | None
```

`ops_lead` is the admin role. Ops roles never get merchant or consumer actions (and vice versa): an ops token
on a merchant route is simply not a credential of that route (401).

| action | roles | routes |
|---|---|---|
| public | anyone (no user role) | GET /healthz, GET /readyz, POST /v1/signup, POST /v1/sessions, POST /v1/user-tokens, POST /v1/ops/sessions; simulated card network (guarded by `X-Network-Token`, not a user role): POST /v1/network/disputes, POST /v1/network/disputes/{dispute_id}/resolution |
| me.mfa | consumer | POST /v1/me/mfa/totp, POST /v1/me/mfa/totp/verify |
| me.wallet.read | consumer | GET /v1/me/wallets, POST /v1/me/wallets |
| me.transfer.read | consumer | GET /v1/me/transfers, GET /v1/me/payouts/{payout_id}, GET /v1/me/payments, GET /v1/me/disputes, GET /v1/me/notifications, POST /v1/me/notifications/{notification_id}/read |
| me.transfer.create | consumer | POST /v1/me/transfers, POST /v1/me/top-ups, POST /v1/me/payouts, POST /v1/me/payments, POST /v1/me/payments/{payment_id}/refund-requests, POST /v1/me/payments/{payment_id}/disputes, POST /v1/me/notifications/{notification_id}/response |
| me.profile.read / me.profile.rectify | consumer | GET / PATCH /v1/me/profile |
| me.data.export / me.data.erase / me.data_requests.read | consumer | POST /v1/me/data-requests/export, POST /v1/me/data-requests/erasure, GET /v1/me/data-requests |
| merchant.token.issue | merchant_admin, merchant_support | POST /v1/merchant-tokens |
| payment.create | merchant_admin | POST /v1/payments |
| payment.read | merchant_admin, merchant_support | GET /v1/payments, GET /v1/payments/{payment_id}, GET /v1/payments/{payment_id}/risk, GET /v1/notifications, POST /v1/notifications/{notification_id}/read |
| refund.create | merchant_admin | POST /v1/payments/{payment_id}/refunds |
| merchant.dispute.read | merchant_admin, merchant_support | GET /v1/disputes/{dispute_id}, GET /v1/disputes |
| merchant.dispute.evidence | merchant_admin, merchant_support | POST /v1/disputes/{dispute_id}/evidence |
| dispute.read | ops_agent, ops_lead | (no route yet) |
| dispute.resolve | ops_lead | POST /v1/ops/disputes/{dispute_id}/resolution |
| ops.token.issue | ops_agent, ops_lead | POST /v1/ops/ops-ai-tokens |
| kyc.review.read | ops_agent, ops_lead | GET /v1/ops/kyc-reviews |
| kyc.verify | ops_agent, ops_lead | POST /v1/ops/users/{user_id}/kyc/verify |
| dsr.read | ops_agent, ops_lead | GET /v1/ops/data-requests |
| audit.read | ops_lead | GET /v1/ops/audit |

Routes merged after the policy table was fixed (wallet management, top-ups, payouts, consumer payments,
disputes, notifications) reuse the existing action whose role set is the least privilege for them: consumer
routes a `me.*` action (reads `me.transfer.read`, money-moving or dispute-opening writes `me.transfer.create`,
wallet creation `me.wallet.read`), merchant reads `payment.read` / `merchant.dispute.read` (both merchant
roles; marking a notification read changes only its read state). No new merchant money-moving route was added.
Finer-grained action names (`me.wallet.create`, `me.payout.create`, `me.payment.create`, `me.dispute.open`,
`me.notification.read`, `merchant.notification.read`, ...) need the policy table and its test extended.

Ops dispute resolution: a Bearer credential must be an ops session whose role is allowed `dispute.resolve`
(401 "invalid ops session" / 403 "forbidden", audited as `ops:<id>` with the session role); a request with no
Bearer credential falls back to the interim `X-Ops-Admin-Token` (audited as `ops:admin`). The simulated card
network routes carry no user role and accept only `X-Network-Token`; a user credential is not one (401, or 503 while no network token is configured).

Enforcement: every credential dependency (merchant API key, consumer session, ops session) looks up the
matched route's action via `ROUTE_ACTIONS` and answers 403 `{"detail": "forbidden"}` when the principal's
role is not allowed. A route with no entry is denied to every role except `merchant_admin` on API-key routes
(compatibility for routes added on other branches); the generated matrix test fails for any route without an
entry, so the gap is caught at merge.

Merchant roles: `api_keys.role` (`merchant_admin` default, `merchant_support`). `issue_api_key(..., role=)`
and `create_merchant(..., role=)` take the role; `authenticate_api_key_role(session, api_key, pepper) ->
tuple[uuid.UUID, MerchantRole] | None`. A support key's ops-ai token carries `roles: ["merchant_support"]`.

## 2. Ops principals (`gateway.admin`)

```python
OpsRole = Literal["ops_agent", "ops_lead"]
@dataclass(frozen=True) class OpsPrincipal: ops_user_id: uuid.UUID; role: OpsRole
@dataclass(frozen=True) class OpsEnrolment: ops_user_id: uuid.UUID; totp_secret: str (repr=False)
def bootstrap_ops_user(session_factory, clock, email, password, role, totp_encryption_key, ops_user_id=None) -> OpsEnrolment
def authenticate_ops(session_factory, clock, totp_encryption_key, email, password, totp_code) -> OpsPrincipal
OPS_SESSION_AUDIENCE = "gateway-ops"; OPS_SESSION_TTL_SECONDS = 900
OPS_MAX_FAILURES = 5; OPS_LOCKOUT = timedelta(minutes=15)
def issue_ops_session_token(private_key_pem, principal, now) -> str
def verify_ops_session_token(private_key_pem, token, now) -> OpsPrincipal   # InvalidOpsSessionError
class OpsGuard(session_factory, clock, private_key_pem: str | None):
    def require(self, action: str) -> Callable[..., OpsPrincipal]   # FastAPI dependency
```

- Ops users live in `gateway.ops_users` (argon2id password, AES-GCM TOTP secret, role, lockout counter,
  `disabled_at`). TOTP is mandatory for ops: there is no password-only ops login.
- `authenticate_ops`: unknown email, wrong password and wrong/replayed TOTP code all raise
  `InvalidOpsCredentialsError` (unknown email still costs one argon2 verification). The 5th failure locks
  for 15 min: `OpsLockedError(retry_after)`; a locked account is refused before the password check.
  Success resets the counter. Audit: `ops.login` / `ops.login_failed` / `ops.login_locked`.
- Ops session token: RS256, `aud "gateway-ops"`, `sub "ops:<id>"`, `ops_user_id`, `role`, `amr ["pwd","otp"]`,
  15 min. `OpsGuard.require(action)`: missing/invalid token, unknown or disabled ops user, or a role in the
  token different from the stored role → 401 `{"detail": "invalid ops session"}`; role not allowed for the
  action → 403 `{"detail": "forbidden"}`.
- A consumer session or merchant token is never accepted as an ops session (different audience).
- ops-ai token for ops: `gateway.accounts.tokens.issue_ops_token(private_key_pem, ops_user_id, role, now)`:
  `aud "ops-ai"`, `sub "ops:<id>"`, `ops_user_id`, `roles [role]`, 15 min.

Swap for the dispute routes built on another branch: replace `actor_id: Annotated[str, Depends(ops_actor)]`
with `principal: Annotated[OpsPrincipal, Depends(ops_guard.require("dispute.resolve"))]` and pass
`f"ops:{principal.ops_user_id}"` / `principal.role` as the audit actor.

### Gateway routes

- `POST /v1/ops/sessions {email, password, totp_code}` → 201 `{access_token, token_type, expires_in, role}`,
  no-store. Bad credentials → 401 `{"detail": "invalid credentials"}`; locked → 429 with Retry-After; no JWT
  key or no TOTP key → 503.
- `POST /v1/ops/ops-ai-tokens` (ops session) → 201 `{access_token, token_type, expires_in}`.
- `GET /v1/ops/kyc-reviews?status=open|verified|rejected` → 200 `{items: [{review_id, user_id, status,
  reasons, opened_at, resolved_at, resolved_by}]}` newest first, at most 100.
- `POST /v1/ops/users/{user_id}/kyc/verify {outcome: "verified"|"rejected"}` → 200 `{user_id, kyc_status,
  kyc_expires_at}`; unknown user → 404. Simulated re-verification (no document check).
- `GET /v1/ops/data-requests?overdue=true|false` → 200 `{items: [{request_id, user_id, kind, status,
  received_at, due_at, completed_at}]}`; `overdue=true` = status `received` and `due_at < now`.
- `GET /v1/ops/audit?target=<str>&limit=<1..100>` (ops_lead only) → 200 `{items: [{id, actor_id, actor_role,
  action, target, summary, correlation_id, occurred_at}]}` newest first.

## 3. ops-ai principal (`ops_ai.scope`, `ops_ai.auth`)

- `Scope(merchant_id, role: MerchantRole = "merchant_admin")`; merchant tokens with roles exactly
  `["merchant_admin"]` or `["merchant_support"]` are accepted.
- `OpsScope(ops_user_id, role: OpsRole)`; `Principal = Scope | UserScope | OpsScope`. A token carries exactly
  one of `merchant_id` / `user_id` / `ops_user_id`; an ops token needs `sub == "ops:<ops_user_id>"` and roles
  exactly `[role]` with role in `OPS_ROLES`.
- `require_ops_role(principal, allowed=OPS_ROLES) -> OpsScope`, raises `ForbiddenPrincipalError`.
- `create_app` gains `ops_scope` (403 for non-ops); merchant and user routes answer 403 to an ops token.

## 4. Append-only audit (P0-10)

- The trigger `audit_log_append_only` rejects UPDATE and DELETE for every role (owner included).
- Role `gateway_app` (NOLOGIN; the connecting user is granted membership): USAGE on schemas `gateway` and
  `simulator`; SELECT/INSERT/UPDATE/DELETE on all tables there (default privileges cover tables created by
  later migrations) except `gateway.audit_log`, where it has SELECT and INSERT only; USAGE/SELECT on
  sequences. No TRUNCATE.
- `gateway.audit.db_role.use_db_role(engine, role)`: every pooled connection runs `SET ROLE <role>`
  (identifier-quoted). `GATEWAY_DB_ROLE` (compose default `gateway_app` for `gateway-api`) enables it.
- Audit rows are added in the same transaction as the operation (`gateway.audit.service.record_audit`); a
  failing audit insert raises and the whole operation rolls back.

## 5. AML / KYC re-verification (`gateway.compliance.kyc`)

User columns: `full_name`, `country` (ISO 3166 alpha-2), `kyc_status` (`unverified` default, `verified`,
`pending`, `rejected`), `kyc_verified_at`, `kyc_expires_at`, `erased_at`.

Rules (pure: `evaluate_kyc_rules(facts: KycFacts, now) -> tuple[KycRule, ...]`, in this order):

| rule | fires when |
|---|---|
| amount_threshold | amount_minor ≥ AMOUNT_THRESHOLD_MINOR[currency] (USD/EUR 1_000_000, INR 80_000_000, JPY 1_500_000) |
| volume_24h | prior 24 h sent volume (succeeded transfers + non-failed payouts) in that currency + amount ≥ 2 × amount threshold |
| high_risk_country_pair | sender or recipient country in HIGH_RISK_COUNTRIES (`KP`, `IR`, `MM`) and the two differ |
| kyc_expired | status `verified` and kyc_expires_at ≤ now |
| kyc_stale | status `verified` and kyc_verified_at + 365 d ≤ now |
| velocity | ≥ 10 prior transfers sent or non-failed payouts made in the last hour |
| name_change | (not from facts) a rectification changed a non-null `full_name` |

`screen_operation(session_factory, clock, user_id, OperationRequest(kind, amount_minor, currency,
recipient_email=None)) -> ScreeningResult(triggered, cross_border, high_risk, blocked, kyc_status)` runs in
its own committed transaction (user row locked): when a rule fires the user moves to `pending` (unless already
`pending`), one open `kyc_reviews` row holds the union of fired rules, an audit row `kyc.review_opened`
(summary: rules, operation kind; no amounts tied to names, no email) and an outbox event
`compliance.kyc_review_required` are written. High-risk = cross-border transfer (both countries known and
different) or a payout whose amount plus the user's same-currency payouts of the last 24 h (pending, unknown or
succeeded; failed ones do not count) reaches the amount threshold. `blocked` = high-risk and status after
screening is not `verified`, or any payout by a `rejected` user. Non-failed payouts also count as sent volume
(volume_24h) and as sent operations (velocity); a blocked attempt writes `kyc.operation_blocked`. `POST /v1/me/transfers` screens first and
answers 403 `{"error": "kyc_reverification_required"}` (no-store) when blocked. `POST /v1/me/payouts` screens the same
way (kind `payout`) under the same per-user operation lock: blocked → 403
`{"error": "kyc_reverification_required"}`, no payout row and no funds held; the lock held by another
operation of the user → 409 `{"error": "operation_in_progress"}` with Retry-After: 1 (no-store).

`resolve_kyc(session_factory, clock, principal, user_id, outcome)`: `verified` → status verified,
`kyc_verified_at = now`, `kyc_expires_at = now + 365 d`; `rejected` → status rejected; open reviews take the
outcome with `resolved_by "ops:<id>"`; audit `kyc.reverified` / `kyc.rejected` by the ops actor.

ops-ai opens a ticket per review (queue `compliance`, source `kyc_review`, source_key
`kyc_review:<review_id>`).

## 6. GDPR data-subject requests (`gateway.compliance.gdpr`)

Table `data_subject_requests(id, user_id, kind access|erasure|rectification, status received|completed|
rejected, received_at, due_at = received_at + 30 d, completed_at, detail)`.

- `GET /v1/me/profile` → `{user_id, email, full_name, country, kyc_status, kyc_expires_at}`.
- `PATCH /v1/me/profile {full_name?, country?}` (extra fields 422, country `^[A-Z]{2}$`, name 1..200) →
  200 profile; a completed `rectification` request with `detail.fields`, audit `gdpr.rectified` (field names
  only, never values). Changing an existing non-null `full_name` fires `name_change`.
- `POST /v1/me/data-requests/export` → 200 `{request_id, generated_at, data: {profile, wallets, transfers,
  kyc_reviews, data_requests}}`, no-store; transfers carry direction, amount, currency, created_at and no
  counterparty email; completed `access` request; audit `gdpr.exported`.
- `POST /v1/me/data-requests/erasure` → 202 `{request_id, status: "completed"}`: email → 
  `erased-<random hex>@erased.invalid`, `full_name` and `country` → NULL, password hash replaced by an unusable
  one, TOTP secret and login throttle rows deleted (crypto-shred: the secret ciphertext is gone),
  `erased_at = now`. Ledger, transfers, balances, KYC status and audit rows are kept (AML retention; they hold
  pseudonymous ids only). Audit `gdpr.erased`; outbox `compliance.subject_erased {request_id, user_id}`. Any
  later session of that user → 401.
- `GET /v1/me/data-requests` → own requests with deadlines.

ops-ai (`ops_ai.compliance`): consumes `gw.compliance.v1` (group `ops-ai.compliance`, dedupe in
`processed_events`, malformed → `gw.compliance.v1.dead`). `subject_erased` replaces the user id in
`transfer_facts.sender_user_id` and in compliance ticket text with a fresh random pseudonym and records
`subject_erasures(request_id, processed_at, rows_pseudonymised)` (no user id kept);
`GET /v1/ops/compliance/erasures` (ops token) lists them.

## Events (`contracts.compliance`)

`COMPLIANCE_TOPIC = "gw.compliance.v1"`; `KycReviewRequiredV1 {event_id, event_type
"compliance.kyc_review_required", occurred_at, origin, review_id, user_id, rules}`; `SubjectErasedV1
{event_id, event_type "compliance.subject_erased", occurred_at, origin, request_id, user_id}`;
`ComplianceEventV1` = discriminated union on `event_type`; `parse_compliance_event(payload) ->
ComplianceEventV1`. Pseudonymous ids only.
