# Disputes over HTTP, notifications, consumer payments, risk gaps (B3.3, B7.1, B7.3, B11.1, B11.2, B13.1, B13.2)

Builds on `gateway-basic.md`. Existing routes and service functions keep their behaviour; everything here is
additive. Same conventions as the rest of the gateway: unknown and other-owner ids give the same 404, keyset
cursors (`created_at DESC, id DESC`, opaque base64, bad cursor → 400 `{"detail": "invalid cursor"}`),
`limit` 1..100 (default 20), money in minor units.

## 1. Data

Migration `d7a3c9e15b42` on top of `c41e7a9d2b85`:

- `payments.payer_user_id uuid NULL → users.id`, index `(payer_user_id, created_at, id)`. Set only when a
  signed-in user pays a merchant (`POST /v1/me/payments`). It is the consumer's ownership of the payment.
- `payments.chargeback_flag boolean NOT NULL DEFAULT false`. Set to true when a dispute on the payment is
  resolved `lost` (never reset).
- `notifications.read_at timestamptz NULL`, `notifications.response varchar(16) NULL`
  (`confirmed | not_me`), `notifications.responded_at timestamptz NULL`, index
  `(recipient_type, recipient_id, created_at, id)`.

## 2. Consumer payments (B13.1 "pay a merchant", consumer history)

- `POST /v1/me/payments` (Bearer session, `Idempotency-Key` required, visible ASCII, max 200 chars)
  body `{merchant_id, amount_minor, currency, card: {number, exp_month, exp_year, cvc}}` (test PANs only,
  same `CardInput` rules). Charges the card for that merchant through the normal charge path with
  `payer_user_id` = the session user. The idempotency key is stored namespaced as `user:<user_id>:<key>` so it
  never collides with the merchant's own keys. Response: the charge body (201) or the charge error outcome.
  Unknown merchant → 404 `{"error": "merchant_not_found"}`. Missing key → 400.
  All responses `Cache-Control: no-store`.
- `GET /v1/me/payments?limit&cursor&state&created_from&created_to` (Bearer session) → 200
  `{items: [ConsumerPayment], next_cursor}`. Only payments with `payer_user_id` = the user. Same filter rules
  as `GET /v1/payments` (half-open range, aware ISO 8601, bad value → 400).
  `ConsumerPayment = {payment_id, merchant_id, state, amount_minor, currency, card: {bin,last4}|null,
  decline_code, refunded_minor, disputed_minor, chargeback_minor, chargeback_flag, created_at}`.
- `POST /v1/me/payments/{payment_id}/refund-requests` (Bearer session) body `{amount_minor?, note?}`
  (`note` ≤ 280 chars, no control/format chars except none) → 201 `{payment_id, requested_minor, state:
  "requested"}`. Creates a `refund_requested` notification for the merchant (no money moves; the merchant
  refunds through `POST /v1/payments/{id}/refunds`). Not the payer / unknown → 404 `payment_not_found`;
  payment not `succeeded` or nothing refundable → 409 `payment_not_refundable`; amount > refundable → 409
  `refund_exceeds_refundable`; a second request for the same payment → 409 `refund_already_requested`.

## 3. Dispute workflow over HTTP (B3.3)

State machine unchanged: `opened → under_review → won | lost`. Any other transition → 409
`{"detail": "invalid dispute transition"}`. `lost` posts the chargeback journal + fee, adds to
`chargeback_minor`, and sets `payments.chargeback_flag = true`. `refunded + disputed + chargeback ≤ amount`
always (DB check constraint + `refundable_minor`).

Service changes in `gateway/payments/disputes.py` (backward compatible):

- `open_dispute(session_factory, clock, payment_id, reason, amount_minor=None, *, payer_user_id=None,
  actor_id="network", actor_role="card_network")`. With `payer_user_id`, a payment whose `payer_user_id`
  differs raises `PaymentNotFoundError`. Also inserts a `dispute_opened` notification for the merchant.
- `resolve_dispute(..., actor_id, actor_role="ops_lead")`. Inserts `dispute_resolved` for the merchant and,
  when the payment has a payer, `dispute_update` for the payer.
- Every successful open, evidence submission and resolution also adds one `gw.disputes.v1` outbox row
  (`contracts.events.DisputeEventV1`) in the same transaction (R2-1 / R2-2, contract and ops-ai projection in
  `docs/specs/evaluation-judge.md` §6.4.1). Actor category: `consumer` for the consumer route, otherwise
  `card_network` on open; `merchant` on evidence; `card_network` for the network resolution, otherwise
  `ops_lead`. An unknown actor role, or `consumer` without a payer, raises `UnknownDisputeActorError` before any
  write (fail closed). The reason is sent only when the dispute was opened by `card_network` and it is shaped
  like a network code; free text and evidence text stay in the gateway.
- `list_merchant_disputes(session, merchant_id, limit, cursor, state=None) -> DisputePage`,
  `list_user_disputes(session, user_id, limit, cursor) -> DisputePage` (`DisputePage{items: list[DisputeView],
  next_cursor}`).

Routes (errors: not found 404 `{"detail": "dispute not found"}` / `{"detail": "payment not found"}`, not
disputable 409 `{"detail": "payment not disputable"}`, already open 409 `{"detail": "dispute already open"}`,
amount 409 `{"detail": "dispute amount exceeds"}`, bad reason 422):

| Route | Caller | Notes |
|---|---|---|
| `POST /v1/me/payments/{payment_id}/disputes` `{reason, amount_minor?}` | consumer session | own payments only (payer); 201 DisputeView |
| `GET /v1/me/disputes?limit&cursor` | consumer session | disputes on the user's payments |
| `GET /v1/disputes?limit&cursor&state` | merchant API key | own disputes (bad state → 422) |
| `POST /v1/disputes/{id}/evidence` | merchant API key | existing |
| `POST /v1/network/disputes` `{payment_id, reason, amount_minor?}` | simulated network, header `X-Network-Token` | 201 |
| `POST /v1/network/disputes/{id}/resolution` `{outcome: won\|lost}` | simulated network | 200 DisputeView; actor `network`, role `card_network` |
| `POST /v1/ops/disputes/{id}/resolution` `{outcome}` | ops, header `X-Ops-Admin-Token` | 200; actor `ops:admin`, role `ops_lead`. To be swapped for the RBAC `require_ops("dispute.resolve")` principal |

`X-Network-Token` / `X-Ops-Admin-Token` are compared in constant time with `GatewayAppDeps.network_token` /
`ops_admin_token` (env `GATEWAY_NETWORK_TOKEN`, `GATEWAY_OPS_ADMIN_TOKEN`). Not configured → 503
`{"detail": "network not configured"}` / `{"detail": "ops not configured"}`; missing or wrong → 401
`{"detail": "invalid network token"}` / `{"detail": "invalid ops token"}`. The merchant API key never opens or
resolves a dispute (`POST /v1/disputes` stays 405, `/v1/disputes/{id}/resolve` stays 404).

## 4. Notifications (B11.1, B11.2)

Kinds (column `kind`):

| recipient | kind | created by |
|---|---|---|
| merchant | `payment_confirmation`, `suspicious_activity` | notifier (unchanged) |
| merchant | `dispute_opened`, `dispute_resolved` | dispute service, same transaction |
| merchant | `refund_requested` | consumer refund request |
| user (payer) | `payment_receipt` | notifier, live `payment.succeeded` with a payer |
| user (payer) | `was_this_you` | notifier, payer payment that is `risk_flagged` or `risk_blocked` |
| user (payer) | `dispute_update` | dispute resolution |

User-recipient records have `recipient_type = "user"` and get no `gw.notifications.v1` outbox row (that
contract is merchant-only). Payloads stay pseudonymous (no PAN/BIN/last4/email).

`NotificationView = {notification_id, kind, payment_id, payload, created_at, read: bool, response:
"confirmed"|"not_me"|null}`.

- `GET /v1/me/notifications?limit&cursor&unread_only` (session) / `GET /v1/notifications?...` (merchant API
  key) → 200 `{items, next_cursor}`, newest first, own records only (recipient_type + recipient_id).
- `POST /v1/me/notifications/{id}/read` / `POST /v1/notifications/{id}/read` → 200 NotificationView with
  `read: true`; idempotent; another recipient's or unknown → 404 `{"detail": "notification not found"}`.
- `POST /v1/me/notifications/{id}/response` `{answer: "confirmed" | "not_me"}` (session) → 200
  `{notification_id, response, dispute_id}`. Only on `was_this_you` (other kinds → 409
  `{"detail": "not a was-this-you notice"}`); answered once (second answer → 409
  `{"detail": "already answered"}`). Also marks it read.

**Decision (B11.2 not-me):** `not_me` opens a dispute (reason `fraudulent_not_me`, full refundable amount,
actor `user:<id>`, role `consumer`) when the payment is `succeeded`, has a refundable amount and no active
dispute; `dispute_id` is returned. A risk-blocked / declined payment moved no money, so `not_me` only records
the answer (`dispute_id: null`); likewise when a dispute is already active. We do not block the card token:
test PANs are shared across users, so a token block would deny other cardholders.

## 5. Risk gaps (B7.1, B7.3)

**B7.1 velocity-lookup failure policy.** `assess_charge_risk(session, now, fingerprint, amount, currency,
velocity_lookup=None)`; `None` → `card_activity` (module attribute, resolved at call time). The lookup runs
in a SAVEPOINT. If it raises `SQLAlchemyError`, the savepoint is rolled back and the charge is scored
**degraded**: reason `degraded_scoring` is added, only the amount rule is applied, the payment is always
`flagged` (so ops and the cardholder see it), and a high-amount charge is **blocked** with decline code
`risk_unavailable` (fail-closed for high value, fail-open-but-flagged for the rest). `RiskAssessment` gains
`degraded: bool` and `decline_code: str` (`risk_blocked` normally). The charge uses `risk.decline_code`.

**B7.3 false-positive measurement.** `gateway/payments/risk_eval.py`: `score_activity(amount_minor, currency,
recent, declines)` is the pure rule (shared with `assess_charge_risk`), `labeled_groups(seed)` builds a fixed
labeled set (legitimate micro-payment groups: transit taps, coffee runs, in-app purchase bursts with an
occasional decline; card-testing bursts: many sub-$2 charges on one card within minutes, mostly declined),
`replay(group)` replays a group through the rule with the same window semantics as `card_activity`, and
`measure(groups) -> RiskEvalResult(legit_groups, burst_groups, false_positives, detected,
false_positive_rate, detection_rate)`. A group counts as positive if any charge is flagged or blocked.
`python -m gateway.payments.risk_eval` prints the result. The measured numbers are recorded in B7.3.

## 6. Web (B13.1, B13.2)

User area (`web/src/features/user/`), each panel self-contained and loading its own data:
`PayMerchantPanel` (merchant id, amount, currency, test card; review → confirm; one idempotency key per
confirmed payment), `PaymentsHistoryPanel` (`GET /v1/me/payments` with state + date filters, "load more";
each row offers "Request refund" and "Open dispute"), `UserNotificationsPanel` (list, mark read, "Yes, it
was me" / "No, it wasn't me" for `was_this_you`). Merchant area (`web/src/features/merchant/`):
`DisputesPanel` (`GET /v1/disputes`, evidence textarea for `opened` disputes) and
`MerchantNotificationsPanel`.

Panels that load data on mount (`PaymentsHistoryPanel`, both notification panels, `DisputesPanel`) and
`PayMerchantPanel` sit behind `web/src/components/Disclosure.tsx` ("Show …" / "Hide …", `aria-expanded`): the
content mounts only when opened, so the area's first render makes no extra requests and existing area tests
keep their fetch sequence.

## 7. Security review outcome and remaining items

Applied after the `security-auditor` review: unset or empty network/ops token → 503; equal
`GATEWAY_NETWORK_TOKEN` and `GATEWAY_OPS_ADMIN_TOKEN` → startup error; `lock_payment(..., payer_user_id=...)`
filters by payer in the `SELECT … FOR UPDATE`, so another user's payment is never locked; degraded risk
scoring logs a warning (exception type only); CVC input is `type="password"`; `POST /v1/payments` rejects a merchant
`Idempotency-Key` starting with `user:` (400), the prefix reserved for consumer payments.

Remaining (not done): no rate limit on consumer money endpoints (`POST /v1/me/payments`, refund requests,
disputes); the migration creates the FK and indexes in one blocking step (fine for the demo dataset; production
would use `NOT VALID` + `VALIDATE` and `CREATE INDEX CONCURRENTLY`); consumer dispute `reason` and refund `note`
are free text shown to the merchant (no PAN-pattern filter); the notification unique key
`(source_event_id, kind)` does not include the recipient; an `opened` dispute whose merchant never submits
evidence cannot be resolved (no timeout transition).
