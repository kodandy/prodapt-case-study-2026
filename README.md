# AI-Powered Payment Gateway Platform (case study)

A payment gateway with an ops assistant. When a payment fails, is declined, looks fraudulent or settles differently than
expected, merchants and operations staff can find the cause with evidence. See `docs/product-brief.md` for the problem
statement, `docs/architecture-detail.md` for the design and its trade-offs, and `docs/requirements-traceability.md` for the
mapping from the brief to code and tests.

Status (2026-11-03): the Day 1 thin slice (decision D-022) runs end to end and is covered by tests. The HTTP gateway,
the outbox relay, the ops-ai projection, search and the assistant are implemented as libraries and exercised in-process by
the tests. Containerised long-running services and the React UI come next.

## Layout

| Path | What |
|---|---|
| `contracts/` | Versioned event schemas shared by the services (`PaymentEventV1`) |
| `services/gateway/` | FastAPI gateway: sign-up, merchants and API keys, card tokenization, idempotent charges, ledger, outbox, audit log, acquirer simulator |
| `services/ops-ai/` | Stream consumer, payment projection, full-text search, assistant with an LLM port (mock by default) |
| `scripts/generate_dataset.py` | Seeded synthetic dataset (brief fields + extensions) with planted sample-query scenarios |
| `tests/` | Cross-service tests: the end-to-end thin slice and import-boundary rules |
| `docs/specs/` | Test specifications that the tests were written from |

## Requirements

- Docker with Compose v2. All Python tooling (uv, ruff, mypy, pytest, alembic) runs inside the `dev` container.
- No Python install is needed on the host.

## Setup and checks

```bash
make ci        # start Postgres + Redis, sync deps, lint, type-check, migration round-trip, all tests
```

Individual targets: `make up`, `make sync`, `make lint`, `make type`, `make migrate`, `make migrate-check`, `make test`,
`make down`.

## End-to-end example

The thin slice runs inside one test process against the real Postgres and Redis containers:

```bash
make up sync migrate
docker compose run --rm dev uv run --frozen pytest -v tests/e2e/test_thin_slice.py
```

What `test_thin_slice_declined_payment_flows_from_charge_to_a_fallback_answer` does:

1. Signs up a user (argon2id password hash, wallet ledger account) and creates a merchant with an `sk_test_...` API key.
2. Charges two test cards through the charge service, each with its own idempotency key: one approved and one declined
   (`4000000000000002`). The gateway tokenizes the card, storing only the token, BIN, last 4 digits and a keyed fingerprint.
   It calls the acquirer simulator outside the transaction. It then writes the payment state, ledger journal, outbox row
   and audit row in one transaction. The HTTP route `POST /v1/payments` (`Idempotency-Key` header) is covered in
   `services/gateway/src/gateway/__tests__/test_app.py`.
3. The outbox relay publishes the events to the Redis stream `payments.v1`.
4. ops-ai consumes the stream into its own projection and search index.
5. With the LLM down, the assistant answers "declined payments" in fallback mode. The answer has no generated text but
   carries the evidence: the declined payment and the decline-code aggregate, and not the approved payment.

Other tests in the same file check that each charge produces exactly one payment through the ledger, outbox and
projection. They also check that no full card number appears in any schema or on the stream, and that charges keep
succeeding while the LLM is down.

## Synthetic dataset

```bash
docker compose run --rm dev uv run --frozen python scripts/generate_dataset.py --seed 42 --records 12000 --out data/generated
```

Writes `transactions.csv`, `transactions.json` and `incidents.json` (git-ignored). The same seed produces byte-identical files.

- The 16 fields from the brief come first, in the brief's order.
- Extension columns: `record_kind` (merchant payment or P2P transfer), `scenario`, `card_bin`, `acquirer`, `rule_id`,
  `sender_balance`. They exist so that each sample query can be answered from the data alone.
- Planted scenarios: a card failure spike on one BIN this afternoon (SQ1), a cross-border transfer blocked by a KYC limit
  despite sufficient balance (SQ2), a card-testing burst (SQ3), and a merchant whose payout is reduced by refunds,
  chargebacks and FX (SQ4).
- No value looks like a full card number.

## Configuration

Settings come from environment variables. Copy `.env.example` to `.env` and fill in the values.

| Variable | Purpose |
|---|---|
| `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`, `POSTGRES_PORT` | Local Postgres (ports bind to 127.0.0.1 only) |
| `REDIS_PORT` | Local Redis |
| `GATEWAY_API_KEY_PEPPER` | HMAC key for stored API key hashes (at least 32 characters) |
| `GATEWAY_CARD_FINGERPRINT_KEY` | HMAC key for card fingerprints (at least 32 characters) |
| `GATEWAY_JWT_PRIVATE_KEY_PEM`, `GATEWAY_TOTP_ENCRYPTION_KEY` | Reserved for staff login with TOTP (not in the thin slice yet) |
| `OPENAI_API_KEY` | Empty means the mock LLM is used |
| `OPENAI_CHAT_MODEL`, `OPENAI_FALLBACK_MODEL`, `OPENAI_EMBEDDING_MODEL` | Model names when a key is set (`text-embedding-3-small` for embeddings) |

Secrets are never logged. SQL errors hide bound parameters, and settings validation errors do not echo input values.

## Security notes

- Only the allow-listed test card numbers are accepted. Full card numbers are never stored, logged or published.
- Passwords are hashed with argon2id. API keys are stored as HMAC-SHA256 hashes.
- Idempotency: the DB row keyed by (merchant, key) is the source of truth, and a Redis lock is only an optimisation. A retry
  returns the stored response. The same key with a different body gives 422, and an in-flight request gives 409. An
  unknown provider outcome is stored as 202 and is never reported as failed.

### Known gaps

These were found by the Day 1 audit and are planned:

- A reconciler that resolves `unknown` payments with `provider.lookup` and reclaims expired attempt leases. Until then, a
  crash between the provider call and finalization leaves that key answering 409.
- Separate migration and app database roles. Today `audit_log` append-only is enforced by a trigger, not by grants, and
  TRUNCATE is not blocked.
- DB-level ledger invariants (unique journal per reference, balanced postings per currency).
- Rate limiting, API key revocation, `maxlen` on the stream, and structured logging.

## Development notes

- On the host, `uv` fails on paths that contain a directory named `private`. That is why everything runs in the container.
- Tests sit next to the code in `__tests__/` directories, per vertical slice.
