# AI-Powered Payment Gateway Platform (case study)

A payment gateway with an ops assistant. When a payment fails, is declined, looks fraudulent or settles differently than
expected, merchants and operations staff can find the cause with evidence. See `docs/product-brief.md` for the problem
statement, `docs/architecture-detail.md` for the design and its trade-offs, and `docs/requirements-traceability.md` for the
mapping from the brief to code and tests.

Status (2026-11-03): the Day 1 thin slice (decision D-022) runs end to end as long-running compose services, and
`make demo` shows it working over HTTP. The React UI comes next.

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

## Run the services: `make demo`

```bash
make demo        # build the image, start every service, run the walkthrough; exits 0 only if every check passed
make demo-down   # stop everything
```

No `.env` file and no `OPENAI_API_KEY` are needed: without a key the assistant uses the mock LLM.

| Service | What |
|---|---|
| `demo-secrets` | One-shot. Generates the demo secrets (two HMAC keys and an RSA 2048 key pair) into the named volume `demo-secrets`, creating only missing files |
| `migrate` | One-shot. Runs both Alembic histories |
| `seed` | One-shot. Loads the synthetic dataset (seed 42, 12000 records) and its incidents into ops-ai, shifted so the planted SQ1 afternoon ends now, then the P2P transfer decision records (`python -m ops_ai.transfers`, same shift). A re-run adds nothing |
| `gateway-api` | Gateway HTTP API on `127.0.0.1:8080` |
| `relay` | Outbox relay: publishes committed outbox rows to the Redis stream |
| `ops-ai-api` | Ops assistant HTTP API on `127.0.0.1:8081` |
| `ops-ai-consumer` | Stream consumer that builds the ops-ai projection and search index |

All services use one image (`docker/app.Dockerfile`, non-root). The APIs have a healthcheck on `/readyz`, which checks
Postgres and Redis. Each service mounts only its own secrets subdirectory, read-only. The gateway gets the HMAC keys and
the JWT private key, and ops-ai gets only the public key.

`scripts/demo.py`, run by the `demo` service:

1. Gets an API key for the seeded SQ1 merchant `mch_0001` (the same id in both services: uuid5 of the dataset id) and
   signs up `demo+<random>@example.com`.
2. Over HTTP: one approved charge, one declined charge on the SQ1 BIN (`4000000000000119`, issuer unavailable), and a
   retry of the approved charge with the same `Idempotency-Key`. The retry must return the identical body.
3. Exchanges the API key for a merchant token (`POST /v1/merchant-tokens`). This is an RS256 JWT, issuer `gateway`,
   audience `ops-ai`, lifetime 15 minutes. ops-ai accepts only RS256 and takes the merchant scope from the token alone.
4. Waits until both payments are on the stream and the consumer group has lag 0 and pending 0.
5. Asks the assistant "declined payments" and checks that the evidence cites the declined payment and not the approved
   one.
6. Asks SQ1 ("15% of my card payments are failing this afternoon — what is the root cause and how do we fix it?") on
   `POST /v1/assistant/investigate`. The tools run first (`aggregate_failures`, `compare_baseline`,
   `search_incidents`), and the label and numbers come from them, never from the LLM. The demo checks the label
   `issuer_outage_on_bin`, top BIN `400000`, a window rate of 10–20% against a baseline under 5%, the cited incident
   `inc_0001` with its fix, and that the live declined charge from step 2 is cited. See `docs/specs/sq1.md`.
7. SQ2 as the seeded sender: sets a fresh random password on the planted SQ2 sender (`bootstrap_user`, operator-side,
   no HTTP route), logs in with email and password (`POST /v1/user-tokens`, an RS256 user token with role
   `consumer`), and asks "My international transfer was declined even though I had enough money — what triggered
   it?" on `POST /v1/assistant/investigate-transfer`. The tool `get_payment_status` reads only the sender's own
   transfers. The demo checks the cause `cross_border_limit_for_kyc_tier`, owner `sender`, a balance that covered the
   amount, an amount above the KYC tier 1 limit, the one fix (upgrade to KYC tier 2), the evidence refs
   `transfer:` / `balance:` / `limit:`, and that the merchant token gets 403 on this route and the user token gets 403
   on `/investigate`. See `docs/specs/sq2.md`.
8. SQ3 for the seeded merchant `mch_0002`: gets an API key and a merchant token for it, then runs a live burst of 12
   charges of 50 USD (minor units) round-robin over 4 test cards, 6 of them declined. The consumer runs the card-testing
   watch on every live micro payment (≤ 1.00 of the currency) in the same transaction as the projection: episodes split
   on a gap over 120 s, flagged at ≥ 10 micro payments, a micro share ≥ 80% and ≥ 3 distinct cards (or unknown card
   identity). A flagged episode upserts one `fraud_alerts` row and, on the first insert only, one ticket routed to the
   `fraud` queue with priority `high`. The demo then asks "We're seeing a series of tiny card payments — is this card
   testing, and what should we do?" on `POST /v1/assistant/investigate-fraud` (merchant token, `Cache-Control:
   no-store`). The tool `analyze_fraud` reads the last 24 hours of the merchant's card payments. The demo checks the
   label `card_testing`, a flagged episode holding exactly the seeded SQ3 burst (card identity unknown), a flagged
   episode holding the 12 live payments with 4 distinct cards and a decline ratio of 0.5, its alert with a ticket in
   the `fraud` queue, both cited in the evidence, and that the SQ2 user token gets 403 on this route. Card identity is
   `card_ref`: an HMAC of the merchant id and the gateway card fingerprint under a subkey of the fingerprint key, so
   it counts distinct cards at one merchant without linking a card across merchants. See `docs/specs/sq3.md`.

Only the first 8 characters of the API key are printed. Passwords and tokens are never printed.

## End-to-end test

The same thin slice also runs inside one test process against the real Postgres and Redis containers:

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
3. The outbox relay publishes the events to the Redis stream `gw.payments.v1`.
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
| `GATEWAY_JWT_PRIVATE_KEY_PEM` | RS256 key that signs merchant tokens for ops-ai. The gateway API refuses to start without it |
| `OPS_AI_JWT_PUBLIC_KEY_PEM` | Matching public key. ops-ai refuses to start without it |
| `GATEWAY_SECRETS_DIR`, `OPS_AI_SECRETS_DIR` | Optional directory with one file per setting (used by compose with the `demo-secrets` volume) |
| `GATEWAY_TOTP_ENCRYPTION_KEY` | Reserved for staff login with TOTP (not in the thin slice yet) |
| `OPENAI_API_KEY` | Empty means the mock LLM is used. Set, the assistant narrates through the OpenAI Responses API (20 s budget, one fallback model, then the deterministic fallback answer) |
| `OPENAI_CHAT_MODEL`, `OPENAI_FALLBACK_MODEL`, `OPENAI_EMBEDDING_MODEL` | Model names when a key is set: pinned snapshots `gpt-4.1-mini-2025-04-14` and `gpt-4.1-nano-2025-04-14` by default, an empty fallback means none, `text-embedding-3-small` for embeddings |
| `OPENAI_BASE_URL` | API base URL (default `https://api.openai.com/v1`; tests point it at a local fake server) |

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
- No dead-letter queue on the consumer. A stream entry that fails validation is retried on every loop and blocks the
  entries behind it in this consumer's pending list.
- Merchant tokens cannot be revoked before they expire (15 minutes), and there is no key rotation (`kid`) yet.
- `demo-secrets` recreates only missing files. If only the private key is deleted, the old public key stays and tokens
  stop verifying; delete the volume (`make demo-down` then `docker volume rm payplat_demo-secrets`) to start over.
- The ops-ai engine does not set `hide_parameters` yet.
- Redis has no ACL and every process uses the same Postgres superuser. Anyone who can reach Redis can append forged
  payment events. The consumer refuses to move a payment to another merchant, but it can still record payments that
  never happened. Fix: per-service Redis ACL users and DB roles with schema grants.
- No request body size limit in front of the APIs (oversized bodies are rejected only after they are read).
- The token verifier checks `iss`/`aud`/`exp`/`iat` (`iat` at most 30 s ahead, lifetime at most 900 s), exactly one
  canonical `merchant_id`/`user_id`, the role list, and `sub` on user tokens, but does not check `sub` on merchant
  tokens yet.
- `POST /v1/user-tokens` has no rate limit and no lockout, so passwords can be guessed online. Unknown email and
  wrong password cost the same argon2 verification and give the identical 401. Only the number of concurrent argon2
  verifications is capped (4 per app; a request that waits more than 2 s gets 503). The route is sync, so a request
  waiting for a slot holds a threadpool worker; a login flood slows other sync routes. Fix: async route with an AnyIO
  `CapacityLimiter`, plus per-IP and per-account rate limits. `POST /v1/signup` hashes with no cap.
- `POST /v1/signup` answers 409 for a registered email, which reveals that the email exists. This bypasses the
  identical 401 on login.
- No password rehash on login when the argon2 parameters change.
- `bootstrap_user` replaces the password of an existing id, so it is for the demo database only.
- The OpenAI adapter is in place and runs only when `OPENAI_API_KEY` is set, but output guards are not (no split or
  structuring advice, no numbers absent from the evidence, and cited `[ref]`s are not checked against the evidence
  refs). Before sending it masks Luhn-valid card numbers, strips evidence tags and caps sizes, but sends no less data
  than that. It has been tested against a local fake server only, not the real API.
- Card masking is a heuristic: a card number written with separators other than space, tab, `-`, `.`, `/` or `_`
  (such as `,`, `:` or `|`), or with more than three of them between digits, reaches the model unmasked.
- LLM failures are silent: the route falls back to the deterministic answer with no reason-code log or counter.
- `decline_code`, `provider` and `card_bin` have no format pattern in the event contract.
- `OPENAI_API_KEY` reaches the container as a plain environment variable, and `database_url` is a plain string in the
  ops-ai settings (not a secret type).
- User tokens cannot be revoked before they expire (15 minutes).
- The transfer system is a fixture (`scripts/transfer_fixture.py`) loaded by the seed. There is no transfer execution,
  no stream event for transfers, and no FX.
- Base images are pinned by tag, not by digest, and CI does not run osv-scanner yet.
- The relay loads the full gateway settings, so it mounts the gateway secrets (JWT private key, HMAC keys) although it
  needs only the database and Redis URLs. Fix: a relay-only settings loader.
- An incident with `merchant_id` NULL is shown to every merchant. A NULL written by mistake would leak that incident.
  Fix: an explicit `visibility` column with a CHECK constraint.
- `card_bin` on the event has no format check and goes into the incident search query (bound parameter, so no SQL
  injection, but it can change which incidents match).
- `/v1/assistant/investigate` and `/v1/assistant/investigate-fraud` have no rate limit (`payment_facts` has a
  `(merchant_id, method, occurred_at)` index since SQ3).
- API keys have no `revoked_at`; every `make demo` adds one more key to the demo merchant.
- The seed is not re-anchored: run more than 6 hours after the first seed, SQ1 answers `no_anomaly`. Start over with
  `make demo-down` and `docker volume rm payplat_pgdata` (or truncate `ops_ai`). The seeded SQ3 burst leaves the
  24-hour window about 9 hours after the first seed.
- Card testing (SQ3): no gateway-side risk score or real-time block yet (B7.1, B7.3 gateway half); the watch only
  raises an alert and a ticket after the payment.
- Card testing: an earlier payment arriving after the alert (out-of-order delivery) can start a second alert for one
  burst.
- Card testing: seeded payments carry no card identity, so seeded episodes report distinct cards as unknown.
- Card testing: the thresholds are fixed constants; tuning them from human verdicts is Design.
- Tickets: the lifecycle (`assigned`, `resolved`) and ops-staff routes are not in this slice (B12 Thin).
- Card testing: the rule is easy to evade once known (amounts just above 1.00, gaps over 120 s, or diluting with
  larger card payments). A burst of more than 5000 micro payments in the window is analysed on its newest 5000 only.
- Card testing: alerts and tickets are not capped per merchant, and an alert refresh from an older event can move
  `last_seen_at` back (one consumer today, so latent). A failure in the watch rolls back the projection of that event.
- Card testing: `origin == "live"` is self-declared by the event, so anyone who can write to the stream (no Redis ACL
  yet) can raise alerts.
- Stream: a malformed entry goes to `gw.payments.v1.dead` and is acked; nothing replays or alerts on that stream yet.
- Settlement (SQ4): the gateway settlement batch, `gw.settlement.v1` and payouts are Design; settlement entries are
  seeded as the acquirer's report.
- Settlement: one contract and one FX snapshot for every merchant; per-merchant terms and live rates are Design.
- Settlement: a chargeback is debited when the dispute opens, whatever its outcome; reversal on a won dispute is Design.
- Settlement: the reserve is held only; its release in a later payout is Design.
- Settlement: the cross-check against the payment projection proves the two sources agree, not that both are right.
  It covers amount, currency, card method and the succeeded state. Refund and chargeback flags come from the
  settlement report only, because the projection has no refund or dispute state until B3.
- Settlement: the fee lines are recomputed per payment from the same contract constants that built them, so a wrong
  contract term is not caught.
- Settlement: a period with more than 5000 entries is always reported unverified (`too_many_entries`); streaming
  aggregation is Design.
- Settlement: the DB session stays open during the LLM call, and the contract and FX snapshot ignore the period.
- Settlement: a negative net is shown as a balance the merchant owes; collecting it is Design.

## Development notes

- On the host, `uv` fails on paths that contain a directory named `private`. That is why everything runs in the container.
- Tests sit next to the code in `__tests__/` directories, per vertical slice.
- Run `make demo-down` before `make ci`. The demo relay and consumer share Postgres and Redis with the tests and will
  take the events the tests wait for.
