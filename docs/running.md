# Running, testing and configuration

Details behind the README quick start.

## Setup and checks

```bash
git clone <repo-url> && cd <repo>
make demo-down   # only if a demo stack is running: its relay and consumer would take the events the tests wait for
make ci          # the same command the GitHub Actions workflow runs (.github/workflows/ci.yml)
```

`make ci` runs, in order: `up` (Postgres + Redis), `sync` (build the `dev` image, `uv sync --frozen`), `proto-check`
(generated gRPC code is current), `lint` (ruff check + format check), `type` (mypy), `migrate-check` (each Alembic
history upgrade → downgrade one step → upgrade → `alembic check`), `test` (pytest), `eval` (golden SQ1–SQ4 E2E cases,
custom metrics, cross-tenant leakage gate and judge goldens; LLM-judged metrics are reported as skipped without a key)
and `web-ci` (`npm ci`, then typecheck, lint, unit tests and build in the pinned node image). No `.env` and no
`OPENAI_API_KEY` are needed, here or for `make up`, `make demo`, the sample queries and the e2e tests: compose has
defaults for every setting and the demo secrets are generated into the `demo-secrets` volume.

Last full run (2026-11-05, on a laptop running several other compose stacks): every step passed except one
timing-sensitive test, `test_complete_gives_the_fallback_only_the_remaining_budget` in
`services/ops-ai/src/ops_ai/assistant/__tests__/test_openai_llm.py`, which failed once under load (it got `openai: http 503`
where it expected `openai: timeout`) and passed on three reruns; `eval` and `web-ci` were then run on their own and
passed. The pytest step took about 90 minutes on that machine.

Individual targets: `make up`, `make sync`, `make proto-check`, `make lint`, `make type`, `make migrate`,
`make migrate-check`, `make test`, `make eval`, `make web-ci`, `make down`.

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
| `notifier` | Gateway notification worker (`gateway.notifications.worker`): delivers pending notification records. Started by `make demo` as a dependency of the `demo` service |
| `ops-ai-notifier` | Outbound fraud alert notifications (`ops_ai.notify`); log sink unless `OPS_AI_ALERT_WEBHOOK_URL` is set. Started by `make demo` as a dependency of the `demo` service |
| `gateway-grpc` | Merchant gRPC API on `127.0.0.1:50051`, plaintext in the demo (`GATEWAY_GRPC_INSECURE=true`). Not started by `make demo`: `docker compose up -d gateway-grpc` |
| `web` | nginx serving the Vite SPA and proxying `/api/gw/` and `/api/ops/` to the APIs, on `127.0.0.1:8082`. Started by `make web-up`, not by `make demo` |

All Python services use one image (`docker/app.Dockerfile`, non-root); `web` is built from `web/Dockerfile`
(Node build stage, `nginx:1.28.0-alpine` runtime). The APIs have a healthcheck on `/readyz`, which checks
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
9. SQ4 for the seeded merchant `mch_0003`: asks "Why is my October payout lower than expected?" on
   `POST /v1/assistant/explain-settlement` (merchant token). The tool `explain_settlement` builds the typed statement
   (gross, MDR, fixed fees, FX, refunds, chargebacks, chargeback fees, reserve → net) and cross-checks it against the
   payment projection before any text is written. The demo checks the eight lines in order, that they sum to the net,
   that the net is below the expected payout, the evidence refs, and that the SQ2 user token gets 403. See
   `docs/specs/sq4.md`.

Only the first 8 characters of the API key are printed. Passwords and tokens are never printed.

## Web client: `make web-up`

A Vite + React SPA (`web/`) served by nginx on http://127.0.0.1:8082 (`WEB_PORT`). nginx proxies `/api/gw/` to the gateway and
`/api/ops/` to ops-ai on the same origin, so the browser never needs CORS. See `docs/specs/web.md`.

```bash
make demo              # run first: it seeds the data and the demo secrets
make web-up            # build and start the web container
make web-merchant-key  # print a fresh demo merchant API key once (paste it into the Merchant tab)
make web-ci            # typecheck, lint, unit tests, build
make web-smoke         # Playwright smoke against the running stack
```

- User tab: sign-up/login (with TOTP when enabled), wallets, P2P transfer with a confirm step, transfer history (needs the gateway route, see [Known gaps](known-gaps.md#known-gaps)).
- Merchant tab: payments list and detail, refund with an explicit amount and a confirm step, support assistant with
  a guarded "Open payment" action, settlement summary.
- Credentials live in memory only (never storage, cookies or the URL). Money writes keep the same Idempotency-Key until
  the server gives a final answer, even across a tab switch, Cancel/Close or a re-render; a merchant-side failure
  clears only the merchant credentials.
- nginx sets CSP, `nosniff` and `no-referrer` on every response, `no-store` on the API, and blocks routes the browser
  must not reach (`/v1/user-tokens`, ops investigation, disputes, the OpenAPI docs, `POST /v1/payments`).

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

## Configuration

Settings come from environment variables. Copy `.env.example` to `.env` and fill in the values.

A `.env` is optional: `make up`, `make demo`, the sample queries and the e2e tests run without one (compose defaults
plus the generated `demo-secrets` volume). Use it only to override the values below.

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

## Development notes

- On the host, `uv` fails on paths that contain a directory named `private`. That is why everything runs in the container.
- Tests sit next to the code in `__tests__/` directories, per vertical slice.
- Run `make demo-down` before `make ci`. The demo relay and consumer share Postgres and Redis with the tests and will
  take the events the tests wait for.
