# Thin slice — test specification (Day 1, D-022)

Source of truth for behavior: `docs/architecture-detail.md` §3 (sync charge path, ledger, outbox), §4 (streams, consumers), §5 (search, assistant, LLM down). The interfaces below already exist as stubs (`NotImplementedError`); tests import them as they are.

Path: signup → wallet → card charge → ledger + outbox in the same tx → relay → ops-ai projection → search → mock answer. Minimal success path plus the Day 1 proofs.

## Fixtures (root `conftest.py`, already written)

- `session_factory`: migrated Postgres, all data tables truncated before each test.
- `redis_client`: flushed Redis (`decode_responses=True`).
- `clock`: `gateway.clock.FixedClock` at 2026-11-03T12:00Z.
- Constants `TEST_API_KEY_PEPPER`, `TEST_CARD_FINGERPRINT_KEY` (import from `conftest`).
- Test PANs: `gateway.payments.tokenization.cards.TEST_PANS`. `4242424242424242` approves, `4000000000000002` declines with `card_declined`, `4000000000009995` declines with `insufficient_funds`.

## Day 1 exit proofs (must exist)

| # | Proof |
|---|---|
| E1 | 20 threads call `charge_card` with the same key and request → all 201 with identical bodies; provider `charge` **called once** (count with a wrapping spy around `SimulatorProvider`), `SimulatorProvider.charge_count() == 1`, exactly one journal. Run with `redis=redis_client` and again with `redis=None`. |
| E2 | C2: `ChargeHooks(after_tx2_commit=...)` raises → the first call raises; a retry with the same key returns 201 with the stored body; provider call count stays 1; one journal. |
| E3 | `LlmPort` raising `LlmUnavailableError`: card charges still succeed, and `ask` returns `mode == "t2_fallback"`, `text is None`, with the same evidence the healthy path returns. |

## Behavior per module

### gateway.payments.tokenization.cards
- `luhn_valid`: true for every TEST_PAN; false for a one-digit change.
- `card_fingerprint`: deterministic for the same key; different for a different key; 64 hex chars; does not contain the PAN.

### gateway.payments.schemas
- `ChargeRequest` rejects (pydantic `ValidationError`): a non-Luhn number, a Luhn-valid PAN not on the allowlist, `amount_minor <= 0`, unknown fields.

### gateway.accounts.service
- `sign_up` stores an argon2id hash (`$argon2id$` prefix), never the plaintext; `verify_password` true/false.
- `sign_up` creates one `wallet` ledger account in the currency with an `account_balances` row of 0/0.
- Duplicate email, case-insensitive → `EmailAlreadyRegisteredError`.
- `create_merchant` returns a `sk_test_` key; the stored `key_hash` differs from the key and does not contain it; `authenticate_api_key` returns the merchant id for that key and `None` for any other.

### gateway.payments.ledger.service
- `post_journal` raises `UnbalancedJournalError` if any currency does not sum to zero (including a set that sums to zero only across currencies).
- Hypothesis: any generated balanced multi-currency line set is accepted and the stored postings sum to zero per currency.
- `ensure_account` returns the same id on repeat calls (platform account with `owner_id=None` included).

### gateway.payments.charge — `charge_card`
- Approve: 201; body equals `ChargeResponse` JSON (`mode="json"`): `state="succeeded"`, `card={"bin": "424242", "last4": "4242"}`, `decline_code=None`.
- In the same transaction: one balanced journal (2 postings: provider receivable +amount, merchant payable −amount), one `outbox` row on `gw.payments.v1` whose payload validates as `contracts.events.PaymentEventV1` with `event_type="payment.succeeded"`, one `audit_log` row, and the stored response on the idempotency row.
- The full PAN never appears in any gateway/simulator table (scan all text/JSON columns of the rows written).
- Decline (`4000000000000002`): 201, `state="declined"`, `decline_code="card_declined"`; no journal; one outbox event `payment.declined`.
- Same key + same request, sequential: identical body, one provider call.
- Same key + different request (e.g. amount): 422, body `{"error": "idempotency_key_reused"}`.
- Same key while the first call is still at the provider past `idempotency_wait_seconds` (use a blocking spy provider and a small wait budget): the second caller gets 409, body `{"error": "idempotency_in_progress"}`.
- Different merchants may use the same key independently.
- E1 and E2 above.

### gateway.outbox.relay — `publish_pending`
- After one approved charge, returns 1; the stream `gw.payments.v1` has one entry with fields `event_id` and `payload` (JSON of the event); the outbox row has `published_at` set; a second call returns 0.

### gateway.app — `create_app` (FastAPI `TestClient`)
- `POST /v1/signup` 201 `{user_id, wallet_account_id}`; duplicate email 409.
- `POST /v1/payments`: no/invalid bearer key → 401; missing `Idempotency-Key` → 400; invalid card → 422; valid → 201 with the `charge_card` body; a repeat with the same key returns the same body.
- `GET /healthz` 200.

### ops_ai.ingest.consumer
- `apply_payment_event` creates the `payment_facts` row and a `search_documents` row (`kind="payment"`, `ref_id=payment_id`, title `"Card payment <state>"`, body containing the decline code when present, the provider and the currency).
- Same event twice → second returns False, no change.
- An event with a lower `entity_version` does not overwrite a higher one.
- `consume_payments` after XADD of events in the relay's format: facts written, entries acknowledged (`XPENDING` count 0). Re-adding the same event (redelivery) changes nothing.

### ops_ai.search.service
- Returns only documents of `scope.merchant_id` (seed two merchants with matching text).
- A word from the body matches; an unrelated word returns `[]`.

### ops_ai.assistant
- `ask` with `MockLlm`: `mode="llm"`, non-empty `text`, `model="mock"`; evidence contains `aggregate:decline_code:card_declined` whose summary includes the count, and `payment:<id>` refs from search.
- Evidence never refers to another merchant's payment (seed two merchants).
- E3.

### Import rule (`tests/test_import_rules.py`)
- No module under `services/gateway/src` imports `ops_ai`, and none under `services/ops-ai/src` imports `gateway`. Both may import `contracts`.

### End-to-end (`tests/e2e/test_thin_slice.py`)
- sign_up → create_merchant → approve + decline charges → `publish_pending` → `consume_payments` → `search_documents` finds the declined payment → `ask` with a raising LLM returns t2_fallback with that payment in its evidence.
