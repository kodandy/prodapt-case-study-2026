# AI-Powered Payment Gateway Platform (case study)

A payment gateway with an ops assistant. When a payment fails, is declined, looks fraudulent or settles differently than
expected, merchants and operations staff can find the cause with evidence. See `docs/product-brief.md` for the problem
statement, `docs/architecture-detail.md` for the design and its trade-offs, and `docs/requirements-traceability.md` for the
mapping from the brief to code and tests.

Status (2026-11-05): the services run end to end as long-running compose services. `make demo` answers all four sample
queries (SQ1–SQ4) over HTTP against a freshly seeded stack; the captured inputs and outputs are in
[Sample queries](#sample-queries-sq1sq4). A React web client (`web/`) runs with `make web-up`.

## Layout

| Path | What |
|---|---|
| `contracts/` | Versioned event schemas shared by the services (`PaymentEventV1`) |
| `services/gateway/` | FastAPI gateway: sign-up, merchants and API keys, card tokenization, idempotent charges, ledger, outbox, audit log, acquirer simulator |
| `services/ops-ai/` | Stream consumer, payment projection, full-text search, assistant with an LLM port (mock by default) |
| `scripts/generate_dataset.py` | Seeded synthetic dataset (brief fields + extensions) with planted sample-query scenarios |
| `scripts/demo.py`, `scripts/sample_queries.py` | The `make demo` walkthrough with checks, and the raw JSON answers to SQ1–SQ4 |
| `web/` | React web client (user and merchant tabs), served by nginx |
| `tests/` | Cross-service tests: the end-to-end thin slice and import-boundary rules |
| `docs/specs/` | Test specifications that the tests were written from |

## Requirements

- Docker with Compose v2. All Python tooling (uv, ruff, mypy, pytest, alembic) runs inside the `dev` container.
- No Python install is needed on the host.

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
9. SQ4 for the seeded merchant `mch_0003`: asks "Why is my October payout lower than expected?" on
   `POST /v1/assistant/explain-settlement` (merchant token). The tool `explain_settlement` builds the typed statement
   (gross, MDR, fixed fees, FX, refunds, chargebacks, chargeback fees, reserve → net) and cross-checks it against the
   payment projection before any text is written. The demo checks the eight lines in order, that they sum to the net,
   that the net is below the expected payout, the evidence refs, and that the SQ2 user token gets 403. See
   `docs/specs/sq4.md`.

Only the first 8 characters of the API key are printed. Passwords and tokens are never printed.

## Sample queries (SQ1–SQ4)

All four sample queries from the brief run against the live HTTP APIs of a freshly seeded stack. To reproduce:

```bash
make demo-down && docker compose down -v   # start from empty volumes (drops the demo database)
make demo                                  # seed, run SQ1–SQ4 with checks; exits 0 only if every check passed
docker compose --profile demo run --rm --no-deps -T demo python scripts/sample_queries.py   # raw JSON, one line per answer
make demo-down
```

`scripts/sample_queries.py` sends the same questions with the same principals as `scripts/demo.py` (merchant token
from `POST /v1/merchant-tokens`, SQ2 user token from `POST /v1/user-tokens`) and prints each response body. It never
prints an API key, a password or a token.

The outputs below were captured on 2026-11-05 from a fresh stack (`docker compose down -v`, then `make demo` with
`all checks passed`, then `scripts/sample_queries.py`). They are trimmed to the fields that carry the answer; ids are
dropped and timestamps follow the seed, which shifts the dataset so the SQ1 afternoon ends at seed time, so they
differ on every run. A few fields are condensed for reading (`top_bin` is the first `by_bin` entry, `timeline_changes`
lists `kind subject: detail`, `lines` is `[kind, amount_minor, payments]`); the raw objects are in the script output.
No `OPENAI_API_KEY` was set, so every answer reports `"mode": "llm", "model": "mock"`: the
deterministic `MockLlm` writes the text from the evidence. Labels, numbers, fixes and evidence come from the tools in
both cases; a real key changes only the narrated `text`.

### SQ1: card failures spiking this afternoon — root cause and fix

Input: merchant `mch_0001` (merchant token), `POST /v1/assistant/investigate`,
`{"question": "15% of my card payments are failing this afternoon — what is the root cause and how do we fix it?"}`

```json
{
  "cause_label": "issuer_outage_on_bin",
  "window":   {"total": 241, "failed": 36, "rate": 0.1494,
               "top_bin": {"card_bin": "400000", "failed": 32, "total": 52},
               "by_code": [{"decline_code": "issuer_unavailable", "count": 32}, {"decline_code": "expired_card", "count": 4}]},
  "baseline": {"total": 390, "failed": 7, "rate": 0.0179},
  "suggested_fixes": ["Rerouted BIN 400000 to acquirer acq_b until the issuer recovered."],
  "tool_calls": ["aggregate_failures", "compare_baseline", "search_incidents"],
  "evidence_refs": ["aggregate:window", "aggregate:baseline", "aggregate:bin:400000", "incident:inc_0001", "payment:…"]
}
```

The same question on the failure assistant, `POST /v1/assistant/failures`, separates observed facts, hypotheses and
how to confirm them, and ranks fixes from the playbook:

```json
{
  "comparison": {"window_rate": 0.1494, "baseline_rate": 0.0179, "ratio": 8.32, "elevated": true},
  "timeline_changes": ["failure_onset card", "bin_onset 400000: 32 of 52 payments failed", "new_decline_code expired_card: 4 failures"],
  "reasons": [{"taxonomy_code": "issuer_unavailable", "owner": "issuer", "retryable": false, "count": 32, "share": 0.889},
              {"taxonomy_code": "expired_card", "owner": "cardholder", "retryable": false, "count": 4, "share": 0.111}],
  "facts": ["Window: 36 of 241 card payments failed (14.9%).", "Baseline: 7 of 390 card payments failed (1.8%).",
            "The failure rate is elevated.", "BIN 400000 accounts for 88.9% of failures."],
  "hypotheses": ["The issuer (or its processor) is not answering authorisation requests, so charges time out or are rejected as issuer unavailable.",
                 "Stored or entered cards are past their expiry date."],
  "how_to_confirm": ["Check whether the failures concentrate on one card BIN or issuer.",
                     "Compare the issuer_unavailable rate in the window with the 7-day baseline.",
                     "Check the acquirer status page and open incidents for the same issuer."],
  "fixes": [{"rank": 1, "taxonomy_code": "issuer_unavailable", "fix": "Route the affected BIN range to the secondary acquirer and do not auto-retry timed-out charges; reconcile their outcome with the issuer first.", "incident_refs": []},
            {"rank": 2, "taxonomy_code": "expired_card", "fix": "Ask the cardholder to update the card, and enable account updater for stored cards.", "incident_refs": []}],
  "tool_calls": ["compare_baseline", "change_timeline", "failure_reasons", "search_incidents", "list_failed_payments"]
}
```

What it shows: the failure rate is 14.9% in the last 6 hours against 1.8% over the 7 days before (8.3×), 32 of the 36
failures are `issuer_unavailable` on BIN 400000, and the fix is to reroute that BIN; `/investigate` cites the past
incident `inc_0001` and the live declined charge from `make demo`. On `/failures` the ranked fixes come from the
playbook and carry no incident reference in this run (`incident_refs` is empty): `/failures` searches incidents by
keyword only and finds none on the seed. `make eval` runs SQ1 on both routes: `sq1_bin_outage` on `/investigate`
(with the incident citation) and `sq1_failure_route` on `/failures` (BIN 400000, `issuer_unavailable` as top reason
and top fix, no `incident:` ref expected).

### SQ2: international transfer declined despite sufficient funds — which rule fired

Input: the seeded SQ2 sender (user token, role `consumer`), `POST /v1/assistant/investigate-transfer`,
`{"question": "My international transfer was declined even though I had enough money — what triggered it?"}`

```json
{
  "cause_code": "cross_border_limit_for_kyc_tier",
  "remediation_owner": "sender",
  "balance_covered_amount": true,
  "transfer": {"amount_minor": 180000, "currency": "USD", "status": "failed", "country_sender": "US", "country_receiver": "IN",
               "kyc_tier": 1, "limit_minor": 100000, "limit_currency": "USD", "next_tier_limit_minor": 1000000,
               "sender_balance_minor": 525000},
  "suggested_fixes": ["Upgrade to KYC tier 2: its cross-border limit of 10000.00 USD per transfer covers 1800.00 USD."],
  "tool_calls": ["get_payment_status"],
  "evidence": ["Transfer of 1800.00 USD from US to IN, failed at …",
               "Sender balance at decision time 5250.00 USD vs amount 1800.00 USD",
               "Cross-border per-transfer limit for KYC tier 1 1000.00 USD vs amount 1800.00 USD"]
}
```

What it shows: the balance (5250.00 USD) covered the 1800.00 USD transfer, so it was not insufficient funds; the rule
that fired is the KYC tier 1 cross-border limit of 1000.00 USD per transfer, and the fix is KYC tier 2. The transfer is a
seeded decision record (`scripts/transfer_fixture.py`), not a transfer executed through the gateway.

### SQ3: card-testing micro-transaction series

Input: merchant `mch_0002` (merchant token), `POST /v1/assistant/investigate-fraud`,
`{"question": "We're seeing a series of tiny card payments — is this card testing, and what should we do?"}`.
`make demo` first sends a live burst of 12 charges of 0.50 USD over 4 test cards through `POST /v1/payments`.

```json
{
  "cause_label": "card_testing",
  "episodes": [
    {"micro_payments": 12, "distinct_cards": 4, "declined": 6, "decline_ratio": 0.5,
     "card_bins": ["400000", "424242", "555555"], "flagged": true, "reasons": ["micro_burst", "many_cards", "high_decline_ratio"]},
    {"micro_payments": 40, "distinct_cards": null, "declined": 40, "decline_ratio": 1.0,
     "card_bins": ["411111", "424242", "522222", "555555", "601100"], "flagged": true,
     "reasons": ["micro_burst", "card_identity_unknown", "high_decline_ratio"]}
  ],
  "alerts": [{"queue": "fraud", "ticket_id": "…"}, {"queue": "fraud", "ticket_id": "…"}],
  "suggested_fixes": ["Add a velocity limit on card payments of 1.00 or less, per card and per client IP.",
                      "Require 3-D Secure for card payments of 1.00 or less until the burst stops.",
                      "Do not refund or contact the cards in the burst from this channel; the fraud team works the ticket."],
  "tool_calls": ["analyze_fraud"]
}
```

What it shows: both the live burst (12 micro payments, 4 cards, half declined) and the seeded burst (40 micro payments,
all declined) are flagged as card testing with the features that tripped the rule, and each raised an alert with a
ticket in the `fraud` queue: the seed CLI delivers the last 24 hours before its anchor as the live stream
(`--live-tail-hours`, default 24; 0 = backfill only), so the planted burst goes through the same watcher as the demo's
live charges. Seeded payments carry no card identity. No alert notification beyond the `ops_alerts` row and the ticket
is shown here; SQ3 stays Partial in `docs/requirements-traceability.md` for that reason.

### SQ4: plain-English settlement summary — why the payout differs

Input: merchant `mch_0003` (merchant token), `POST /v1/assistant/explain-settlement`,
`{"question": "Why is my October payout lower than expected?"}`

```json
{
  "period": "2026-10", "currency": "USD", "verified": true, "mismatches": [], "projection_checked": 202,
  "gross_minor": 1125137, "expected_minor": 1092508, "net_minor": 983882, "gap_minor": 108626,
  "lines": [["gross", 1125137, 202], ["mdr", -32629, 202], ["fixed_fees", -6060, 202], ["fx", -876, 10],
            ["refunds", -28393, 8], ["chargebacks", -12540, 3], ["chargeback_fees", -4500, 3], ["reserve", -56257, 202]],
  "tool_calls": ["explain_settlement"],
  "summary": "Your 2026-10 payout is 9838.82 USD, 1086.26 USD less than the 10925.08 USD you would expect from gross sales of 11251.37 USD minus the 2.90% MDR. Fixed fees: 60.60 USD (202 payments at 0.30 USD each). FX conversion: 8.76 USD on 10 non-USD payments (rate set fx-2026-10). Refunds: 283.93 USD on 8 payments. Chargebacks: 125.40 USD on 3 payments. Chargeback fees: 45.00 USD (3 at 15.00 USD each). Rolling reserve: 562.57 USD held (5.00% of gross), released in a later payout."
}
```

What it shows: the eight typed lines
sum to the net payout, the statement was verified against 202 payments in the payment projection before the summary
was written, and the 1086.26 USD gap is explained line by line. Settlement entries are seeded as the acquirer's report;
the gateway settlement batch and payouts are Design (see Known gaps).

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

- User tab: sign-up/login (with TOTP when enabled), wallets, P2P transfer with a confirm step, transfer history (needs the gateway route, see Known gaps).
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

## Security notes

- Only the allow-listed test card numbers are accepted. Full card numbers are never stored, logged or published.
- Passwords are hashed with argon2id. API keys are stored as HMAC-SHA256 hashes.
- Idempotency: the DB row keyed by (merchant, key) is the source of truth, and a Redis lock is only an optimisation. A retry
  returns the stored response. The same key with a different body gives 422, and an in-flight request gives 409. An
  unknown provider outcome is stored as 202 and is never reported as failed.

### Known gaps

The platform-level open gaps (reconciler, k6 p99, gRPC TLS in the demo, `/metrics` exposure, the interim ops admin
token, provider breaker accounting, FX chargebacks, R2 partials, a load-sensitive test) are collected in
[docs/production-readiness.md](docs/production-readiness.md#known-gaps). The list below started from the Day 1 audit
and is kept current per item:

- A reconciler that resolves `unknown` payments with `provider.lookup` and reclaims expired attempt leases. Until then, a
  crash between the provider call and finalization leaves that key answering 409.
- Database roles: the gateway API and gRPC server run as `gateway_app` through `SET ROLE` (`GATEWAY_DB_ROLE`), which
  has no UPDATE, DELETE or TRUNCATE on `audit_log` (P0-10). The compose demo still logs in as the Postgres superuser,
  so code with SQL access can `RESET ROLE`; the relay, notifier and ops-ai do not use a restricted role.
- DB-level ledger invariants (unique journal per reference, balanced postings per currency).
- API key revocation, `maxlen` on the stream, and structured logging. (Rate limiting exists: token buckets per API
  key and per user, see P1-4 in the traceability matrix.)
- The consumer dead-letters a malformed entry (see "Stream" below) but has no retry with backoff for other failures.
- Merchant tokens cannot be revoked before they expire (15 minutes), and there is no key rotation (`kid`) yet.
- `demo-secrets` recreates only missing files. If only the private key is deleted, the old public key stays and tokens
  stop verifying; drop the volumes (`make demo-down && docker compose --profile demo down -v`, which also drops the
  demo database) to start over.
- The ops-ai engine does not set `hide_parameters` yet.
- RBAC (P0-9): routes merged after the policy table was fixed (wallets, top-ups, payouts, consumer payments,
  disputes, notifications) reuse an existing action with the same role set instead of their own action names, so the
  policy cannot yet grant them separately. The generated route × role matrix checks every route with exact 401/403.
- Redis has no ACL and every process uses the same Postgres superuser. Anyone who can reach Redis can append forged
  payment events. The consumer refuses to move a payment to another merchant, but it can still record payments that
  never happened. Fix: per-service Redis ACL users and DB roles with schema grants.
- No request body size limit in front of the APIs (oversized bodies are rejected only after they are read).
- The token verifier checks `iss`/`aud`/`exp`/`iat` (`iat` at most 30 s ahead, lifetime at most 900 s), exactly one
  canonical `merchant_id`/`user_id`, the role list, and `sub` on user tokens, but does not check `sub` on merchant
  tokens yet.
- `POST /v1/sessions` and `POST /v1/user-tokens` lock an account for 15 minutes after 10 wrong passwords in 15
  minutes (429 with Retry-After), but there is no per-IP limit, so guesses spread across many accounts are not
  slowed, and anyone who knows an email can lock that account out. A locked account answers 429 where an unknown
  email answers 401, so a lockout reveals that the email exists. Unknown email and wrong password otherwise cost the
  same argon2 verification and give the identical 401. The number of concurrent argon2 verifications is capped (4
  per app; a request that waits more than 2 s gets 503). The routes are sync, so a request waiting for a slot holds a
  threadpool worker; a login flood slows other sync routes. Fix: async route with an AnyIO `CapacityLimiter`, plus a
  per-IP rate limit. `POST /v1/signup` hashes with no cap.
- `POST /v1/signup` answers 409 for a registered email, which reveals that the email exists. This bypasses the
  identical 401 on login.
- No password rehash on login when the argon2 parameters change.
- `GET /v1/me/transfers` masks the counterparty email to its first character and domain, but does not strip
  control or bidi characters (such as RLO) from that first character. Its 401 has no `Cache-Control: no-store`
  (the same as `/v1/me/wallets`), and only the recipient side has a `(user, created_at)` index, so a large sent
  history is sorted in full. `POST /v1/me/transfers` still returns the recipient's `recipient_user_id` on 201, and
  its 404 `recipient_not_found` reveals whether an email is registered (like the signup 409).
- Web client: `/healthz` and
  `/readyz` are reachable through nginx (the smoke test uses them). A user-side 401 or enabling TOTP still clears the
  merchant credentials too. The api client has no single-flight on the ops token refresh, does not validate response
  shapes, and there is no error boundary. The nginx container runs with default privileges, and the login form has
  no password-manager hints.
- Accepted risk (dev only): `npm audit` reports GHSA-g3hv-75wg-m249 (minimatch ReDoS) through eslint and
  typescript-eslint. minimatch only reads the repo's own lint globs and is not in the production bundle (which has
  react and react-dom only). `npm audit fix --force` would downgrade eslint, so this waits for an upstream eslint major.
- `bootstrap_user` replaces the password of an existing id, so it is for the demo database only.
- The OpenAI adapter is in place and runs only when `OPENAI_API_KEY` is set, but output guards are not (no split or
  structuring advice, no numbers absent from the evidence, and cited `[ref]`s are not checked against the evidence
  refs). Before sending it masks Luhn-valid card numbers, strips evidence tags and caps sizes, but sends no less data
  than that. It has been tested against a local fake server only, not the real API.
- Card masking is a heuristic: a card number written with separators other than space, tab, `-`, `.`, `/` or `_`
  (such as `,`, `:` or `|`), or with more than three of them between digits, reaches the model unmasked.
- LLM failures fall back to the deterministic answer and increment `ops_ai_llm_fallback_total`, but the counter has no
  reason-code label and no reason is logged.
- `decline_code`, `provider` and `card_bin` have no format pattern in the event contract.
- `OPENAI_API_KEY` reaches the container as a plain environment variable, and `database_url` is a plain string in the
  ops-ai settings (not a secret type).
- User tokens cannot be revoked before they expire (15 minutes).
- SQ2's transfer records in ops-ai are a fixture (`scripts/transfer_fixture.py`) loaded by the seed. The gateway does
  execute transfers (`POST /v1/me/transfers`, with FX legs), but publishes no stream event for them, so ops-ai never
  sees a live transfer.
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
  `make demo-down && docker compose --profile demo down -v` (or truncate `ops_ai`). The seeded SQ3 burst leaves the
  24-hour window about 9 hours after the first seed. Its alert and `fraud` ticket are raised once, by the first seed
  (the seed CLI's live tail); a later seed with a new anchor dedupes on `event_id` and raises nothing new.
- Card testing (SQ3): the gateway blocks on its synchronous card-velocity rule (B7.1, B7.3); the ops-ai watch only
  raises an alert and a ticket after the payment.
- Card testing: an earlier payment arriving after the alert (out-of-order delivery) can start a second alert for one
  burst.
- Card testing: seeded payments carry no card identity, so seeded episodes report distinct cards as unknown.
- Card testing: false-positive feedback raises versioned per-merchant thresholds (R2-7) that the live watch applies,
  falling back to the baseline; the SQ3 `investigate-fraud` assistant applies the same active version
  (`X-Fraud-Rule-Version`). The rule actor is the merchant principal and role, not a person.
- Tickets: the lifecycle (`open → assigned → resolved`) exists on merchant-token routes; ops-staff principals are not
  wired to the ticket routes.
- Card testing: the rule is easy to evade once known (amounts just above 1.00, gaps over 120 s, or diluting with
  larger card payments). A burst of more than 5000 micro payments in the window is analysed on its newest 5000 only.
- Card testing: alerts and tickets are not capped per merchant, and an alert refresh from an older event can move
  `last_seen_at` back (one consumer today, so latent). A failure in the watch rolls back the projection of that event.
- Card testing: `origin == "live"` is self-declared by the event, so anyone who can write to the stream (no Redis ACL
  yet) can raise alerts.
- Card testing: each new alert is sent once to an outbound channel (`ops-ai-notifier`, `docs/specs/sq3.md` N-4):
  a signed HTTPS webhook when `OPS_AI_ALERT_WEBHOOK_URL` and `OPS_AI_ALERT_WEBHOOK_SECRET` (32+ characters) are set,
  otherwise a log line, which is what the demo uses. Delivery is at least once (receivers dedupe on
  `Idempotency-Key`); attempts are retried each pass with no backoff, and after 5 failures the row is `failed` with
  nothing that replays or alerts on it. The payload is the snapshot at raise time, not later alert refreshes. The
  secret is a plain environment variable, like `OPENAI_API_KEY`.
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
