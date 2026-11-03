# SQ2 vertical — test specification (11-03)

Source: `docs/submission-outline.md` §3 (SQ2 scope) and §4, `docs/requirements-traceability.md` SQ2 / B1 / B9.1,
`docs/specs/dataset.md` (planted SQ2). Design agreed with Sol on 2026-11-03
(`~/.claude/logs/consults/20261103-180205-consult.md`). Query: "My international transfer was declined even though I
had enough money — what triggered it?"

Done = `make demo` logs in as the seeded SQ2 sender over HTTP, gets a user token, asks SQ2, and prints the cause code,
who can fix it, the amount / limit / balance numbers, the tool call and the evidence.

## Decisions

1. **The sender asks, about their own transfers only.** Visibility is fixed by ownership (`sender_user_id`), never by
   the cause. A P2P transfer has no merchant, so a merchant token cannot see it.
2. **The cause and who can fix it are separate fields.** `cause_code` says what the record shows;
   `remediation_owner ∈ {sender, transfer_provider, unknown}` says who can act. A correctly applied limit is not the
   sender's fault, and a failure in the external transfer system is not necessarily ours.
3. **No forced classification.** A cause is named only when the external system's structured reason code says so and
   the recorded numbers agree with it. Anything missing, contradictory or unknown → `unclassified`, owner `unknown`.
   A rule misfire is never claimed: we do not hold the rule table of the external system.
4. **The platform does not execute transfers or make KYC decisions.** Transfer records come from an external transfer
   system. Here a fixture stands in for it (`scripts/transfer_fixture.py`), and the seed loads it into ops-ai
   (`ingest_source = "seed"`). There is no stream event for transfers.
5. **What the sender is told.** Amount, the limit and the KYC tier it belongs to, the sender balance at decision time,
   and the fix. The internal `rule_id` is stored for audit but is never in the response or the evidence. No advice to
   split a transfer (that would be structuring). The fix is the KYC upgrade, and only when the next tier's limit covers
   the amount.
6. **Login is real (B1 Thin).** Email + password (argon2id) → a short-lived RS256 user token. No endpoint takes a
   `user_id` from the caller.

## 1. Gateway — login and user token

`gateway/accounts/service.py`:
- `sign_up(session, clock, email, password, currency="USD", user_id: uuid.UUID | None = None)`: `user_id` given → the
  user row has that id. Otherwise unchanged.
- `authenticate_user(session, email: str, password: str) -> uuid.UUID | None`: the user id for a matching
  case-insensitive email and correct password, else None. For an unknown email it still runs one argon2 verification
  against a fixed dummy hash, so both failures cost the same.
- `set_password(session, user_id, password) -> None`: operator-side; `UnknownUserError` for an unknown id. Does not
  commit.

`gateway/admin.py`: `bootstrap_user(session_factory, clock, email, password, user_id) -> uuid.UUID`. Operator-side, no
HTTP route. User with that id exists → its password is replaced (the demo uses a fresh random password per run).
Otherwise it signs the user up with that id. Commits.

`gateway/accounts/tokens.py`: `issue_user_token(private_key_pem, user_id, now, ttl_seconds=USER_TOKEN_TTL_SECONDS)`,
`USER_TOKEN_TTL_SECONDS = 900`. Claims: `iss "gateway"`, `aud "ops-ai"`, `sub f"user:{user_id}"`,
`user_id str(user_id)`, `roles ["consumer"]`, `iat`, `exp = iat + ttl`, `jti`. RS256. No `merchant_id` claim.

`gateway/app.py`: `POST /v1/user-tokens` body `{email, password}` (extra fields 422; same field rules as sign-up) →
201 `{access_token, token_type: "Bearer", expires_in: 900}` with `Cache-Control: no-store`.
- Unknown email and wrong password both → 401 with the identical body `{"detail": "invalid credentials"}`.
- No private key configured → 503 (as `/v1/merchant-tokens`).
- The password is never echoed: a 422 on this route does not contain the submitted values.

## 2. ops-ai — token verification and scopes

`ops_ai/scope.py`: `Scope(merchant_id)` stays the merchant scope. New `UserScope(user_id: uuid.UUID)` (frozen).
`Principal = Scope | UserScope`.

`ops_ai/auth.py`: `verify_token(token, public_key_pem, now) -> Principal`. Everything `verify_merchant_token` checks
today (RS256 only, signature, `iss`, `aud`, `exp` against the injected `now`), plus:
- exactly one of `merchant_id` / `user_id` is present. Both or neither → `InvalidTokenError`;
- the present one is a non-empty string that parses as a UUID;
- `roles` is a list and equals `["merchant_admin"]` for a merchant token, `["consumer"]` for a user token. Any other
  value, a missing `roles`, or a merchant role on a user token (and the reverse) → `InvalidTokenError`;
- a user token's `sub` must equal `f"user:{user_id}"`.
`verify_merchant_token` = `verify_token`, then `InvalidTokenError` unless the result is a `Scope`. Its existing
behaviour and tests are unchanged.

Routes:
- `/v1/assistant/ask` and `/v1/assistant/investigate` accept only a merchant token. A valid user token → 403
  `{"detail": "forbidden"}`.
- `/v1/assistant/investigate-transfer` accepts only a user token. A valid merchant token → 403
  `{"detail": "forbidden"}`.
- Invalid / expired / missing token on any of them → 401 `{"detail": "invalid token"}` (unchanged).

## 3. Transfer fixture — `scripts/transfer_fixture.py`

Stands in for the external transfer system's decision record. `CanonicalTransaction` is unchanged.

```python
TransferReasonCode = Literal["cross_border_limit_kyc_tier", "insufficient_funds", "kyc_verification_failed",
                             "beneficiary_rejected", "provider_error"]
KYC_TIER_LIMITS_MINOR = {1: 100_000, 2: 1_000_000}      # USD minor units, per transaction, cross-border
@dataclass(frozen=True) class TransferDecision(transfer_id: uuid.UUID, reason_code: TransferReasonCode | None,
    rule_id: str | None, kyc_tier: int | None, limit_minor: int | None, limit_currency: str | None,
    next_tier_limit_minor: int | None, sender_balance_minor: int | None)
def transfer_decision(tx: CanonicalTransaction) -> TransferDecision | None
```

- None unless `tx.record_kind == "p2p_transfer"`.
- `rule_id == "xb_limit_kyc_tier1_v3"` → `cross_border_limit_kyc_tier`, `kyc_tier 1`, `limit_minor 100_000`,
  `limit_currency "USD"`, `next_tier_limit_minor 1_000_000`.
- `rule_id == "kyc_gate_v1"` → `kyc_verification_failed`, limit fields None.
- `failure_reason == "Insufficient balance"` → `insufficient_funds`.
- `"Beneficiary account closed"` / `"Invalid beneficiary details"` → `beneficiary_rejected`.
- Any other failed transfer → `provider_error`. Not failed → `reason_code` None.
- `sender_balance_minor` is copied from the record (the balance at decision time, in `tx.currency`).
- Planted SQ2 record → `cross_border_limit_kyc_tier`, tier 1, limit 100_000 USD, balance 525_000, amount 180_000.

## 4. Transfer facts — `ops_ai/transfers/`

`models.py`: `ops_ai.transfer_facts` — `transfer_id` (UUID PK), `sender_user_id` (UUID, indexed), `amount_minor`,
`currency` (3), `method` (16), `status` (16), `country_sender`, `country_receiver` (2), `reason_code` (nullable),
`rule_id` (nullable), `kyc_tier` (nullable int), `limit_minor`, `limit_currency`, `next_tier_limit_minor`,
`sender_balance_minor` (nullable), `decided_at`, `ingest_source` (16). Alembic migration with upgrade/downgrade
(existing migration round-trip test).

`seed.py` + `__main__.py`: `seed_transfers(session_factory, log, anchor) -> int` inserts every p2p transfer whose
shifted time (same `seed_shift` as the payment seed) is ≤ anchor, with `ingest_source "seed"`, `ON CONFLICT DO
NOTHING`; returns rows inserted (a re-run returns 0). `python -m ops_ai.transfers [--seed 42] [--records 12000]
[--anchor ISO]` prints one line with that count only.

## 5. Tool and classifier — `ops_ai/transfers/`

```python
@dataclass(frozen=True) class TransferStatus(transfer_id, amount_minor, currency, method, status, country_sender,
    country_receiver, reason_code, kyc_tier, limit_minor, limit_currency, next_tier_limit_minor,
    sender_balance_minor, decided_at)                                  # no rule_id
def get_payment_status(session, scope: UserScope, transfer_id: uuid.UUID | None) -> TransferStatus | None
CauseCode = Literal["cross_border_limit_for_kyc_tier", "insufficient_funds", "kyc_verification_failed",
                    "not_declined", "unclassified"]
RemediationOwner = Literal["sender", "transfer_provider", "unknown"]
def classify_transfer(status: TransferStatus) -> tuple[CauseCode, RemediationOwner]
```

`get_payment_status`: the query itself filters `sender_user_id == scope.user_id`. `transfer_id` None → the newest
failed transfer of that sender. Another sender's id and a non-existent id both return None.

`classify_transfer`, first match wins:
- `status != "failed"` → (`not_declined`, `unknown`).
- `cross_border_limit_kyc_tier` and `country_sender != country_receiver` and `limit_currency == currency` and
  `amount_minor > limit_minor` and `sender_balance_minor >= amount_minor` → (`cross_border_limit_for_kyc_tier`,
  `sender`).
- `insufficient_funds` and `sender_balance_minor < amount_minor` → (`insufficient_funds`, `sender`).
- `kyc_verification_failed` → (`kyc_verification_failed`, `sender`).
- Anything else, including a reason code whose numbers do not agree, a missing number, or another currency →
  (`unclassified`, `unknown`).

## 6. Investigation — `ops_ai/transfers/service.py`

```python
@dataclass(frozen=True) class TransferInvestigation(cause_code, remediation_owner, transfer: TransferStatus,
    balance_covered_amount: bool | None, suggested_fixes: tuple[str, ...], tool_calls: tuple[ToolCall, ...],
    evidence: tuple[EvidenceItem, ...], mode: AnswerMode, text: str | None, model: str | None)
def investigate_transfer(session, scope: UserScope, question: str, transfer_id: uuid.UUID | None,
    llm: LlmPort) -> TransferInvestigation | None                     # None = not found for this sender
```

- One tool call: `ToolCall("get_payment_status", {"transfer_id": str(id) or "latest_failed"})`.
- `balance_covered_amount = sender_balance_minor >= amount_minor` (None without a balance). It says the balance was
  above the amount. It does not claim that fees were covered.
- `suggested_fixes`: for `cross_border_limit_for_kyc_tier`, one fix naming KYC tier 2 and its limit, only when
  `next_tier_limit_minor >= amount_minor`; else empty. `insufficient_funds` → add funds. `kyc_verification_failed` →
  complete identity verification. `unclassified` → "contact support with reference <transfer_id>". No fix mentions
  splitting the transfer.
- Evidence, in order: `transfer:<id>` (amount and currency, corridor, decision time), `balance:<id>` (balance vs
  amount), and for the limit case `limit:<id>` (the KYC tier and its limit vs the amount). Formatted as major units
  with the currency, e.g. `1800.00 USD`. Never the `rule_id`.
- The LLM narrates the evidence only; `LlmUnavailableError` → `t2_fallback`, same fields, `text` None.

## 7. API — `POST /v1/assistant/investigate-transfer`

Bearer user token. Body `{question (1..2000), transfer_id?: UUID}`, extra fields 422. Not found → 404
`{"detail": "transfer not found"}`, identical for another sender's transfer and a missing one. Response:

```json
{"cause_code": "...", "remediation_owner": "...", "mode": "...", "text": null, "model": null,
 "transfer": {"transfer_id": "...", "amount_minor": 0, "currency": "...", "method": "...", "status": "...",
              "country_sender": "..", "country_receiver": "..", "kyc_tier": 1, "limit_minor": 0,
              "limit_currency": "...", "next_tier_limit_minor": 0, "sender_balance_minor": 0, "decided_at": "..."},
 "balance_covered_amount": true, "suggested_fixes": ["..."],
 "tool_calls": [{"name": "get_payment_status", "args": {"transfer_id": "..."}}],
 "evidence": [{"ref": "...", "summary": "..."}]}
```

No `rule_id`, no `reason_code` in the response.

Golden (seed 42, fixed anchor): the planted SQ2 sender, `transfer_id` omitted → the planted transfer;
`cause_code == "cross_border_limit_for_kyc_tier"`, owner `sender`, amount 180_000 USD, limit 100_000, tier 1,
balance 525_000, `balance_covered_amount` true, one fix naming tier 2, evidence refs
`transfer:`, `balance:`, `limit:` for that id. Another sender asking for that id → 404.

## 8. Demo and compose

- compose `seed`: `python -m ops_ai.seed && python -m ops_ai.transfers`.
- `scripts/demo.py`: `bootstrap_user(email "sq2-sender@example.com", random password, user_id = planted SQ2 sender)`,
  then `POST /v1/user-tokens` over HTTP and `/v1/assistant/investigate-transfer` with the SQ2 question. Checks: cause
  code, owner `sender`, balance covered, amount > limit, the tier-2 fix, the three evidence refs, the tool name, and
  that the merchant token gets 403 on this route and the user token gets 403 on `/investigate`. Never prints the
  password or the token.

## Out of scope here

ops_agent cross-tenant view and staff TOTP login (Design), tickets (B12), login rate limit / lockout (README known
gap), transfer execution, FX.
