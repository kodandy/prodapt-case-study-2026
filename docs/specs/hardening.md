# Thin-slice hardening — test specification (Day 1 audit follow-up)

Source: security-auditor report on `fbb7abd` (M1 partial, M3, M5, Low items) and the automated commit review.
Tests go next to the code in the existing `__tests__/` files of each slice. Use the existing fixtures in `conftest.py`.

## H1 — unknown provider outcome is stored and replayed (charge.py)
- When the provider outcome is unknown, `charge_card` returns 202 with the `ChargeResponse` body (`state == "unknown"`), as today.
- New: the idempotency record stores `response_status = 202` and that body. A retry with the same key and body returns 202 with
  an identical body, without waiting `idempotency_wait_seconds` and without calling the provider again
  (the provider is called exactly once in total).
- A retry with the same key and a different body still returns 422.
- No ledger journal, no outbox row and no audit row are written for an unknown outcome (unchanged).

## H2 — only transport-level failures count as unknown (charge.py `_call_provider`)
- `ProviderTimeoutError`, `ConnectionError` and `TimeoutError` raised by `provider.charge` → outcome unknown → 202 (H1 applies).
- Any other exception (e.g. `ValueError`, `RuntimeError`) propagates out of `charge_card` unchanged (same type) and is not turned
  into 202. The payment row is not finalized (its state stays the pre-provider state).

## H3 — concurrent duplicate sign-up maps to the domain error (accounts/service.py)
- If the email unique constraint (`uq_users_email`) fails at flush (a concurrent sign-up won the race after the pre-check),
  `sign_up` raises `EmailAlreadyRegisteredError`, not `IntegrityError`.
  A test may force this by making the pre-check miss (e.g. monkeypatching it) while a committed user with the same email exists.
- HTTP: such a race yields 409 `email already registered` (the existing handler).

## H4 — SQL parameters are hidden in errors (db.py)
- `make_engine(url).hide_parameters is True`. A `DBAPIError` raised through that engine has no bound parameter values in
  `str(exc)` (e.g. a duplicate-email insert's message does not contain the `$argon2id$` hash). Postgres' own `DETAIL` line
  still names the conflicting key (`Key (email)=(...)`); hiding that needs server-side settings and is out of scope here.

## H5 — amount upper bound (payments/schemas.py)
- `MAX_AMOUNT_MINOR = 10**12` exported from `gateway.payments.schemas`.
- `ChargeRequest.amount_minor == MAX_AMOUNT_MINOR` is valid; `MAX_AMOUNT_MINOR + 1` and `2**63` fail validation.
- HTTP `POST /v1/payments` with `amount_minor = 2**63` → 422 `validation_failed` (not 500), and the response does not echo the PAN.

## H6 — Luhn check is ASCII-only (payments/tokenization/cards.py)
- `luhn_valid` returns `False` (never raises) for strings containing non-ASCII digits: superscripts (`"²"`), full-width digits
  (`"４２４２４２４２４２４２４２４２"`), Arabic-Indic digits. ASCII behavior is unchanged.

## H7 — secret keys have a minimum length (config.py)
- `GatewaySettings` rejects `GATEWAY_API_KEY_PEPPER` or `GATEWAY_CARD_FINGERPRINT_KEY` shorter than 32 characters with
  `pydantic.ValidationError`; 32 characters are accepted. Construct it with explicit env (`monkeypatch.setenv`), including
  `DATABASE_URL` and `REDIS_URL`.
- The error message does not contain the rejected secret value.
