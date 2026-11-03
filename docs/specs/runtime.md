# Runtime services and `make demo` — test specification (Day 1, R1–R8)

Source: `docs/submission-outline.md` §4, `docs/architecture-detail.md` (accounts issue asymmetric JWTs; `/healthz` and
`/readyz` on every process). Goal: the thin slice runs as long-running compose services, and one command shows it working.
Tests go in the `__tests__/` directory next to each module, use the root `conftest.py` fixtures, and follow AAA.

Tag: **auth / crypto** (merchant JWT between the services). The security-auditor and red-team lanes apply.

## R1 — merchant access token (gateway `accounts/tokens.py`)

```python
TOKEN_ISSUER = "gateway"; TOKEN_AUDIENCE = "ops-ai"; TOKEN_ALGORITHM = "RS256"; MERCHANT_TOKEN_TTL_SECONDS = 900
def issue_merchant_token(private_key_pem: str, merchant_id: uuid.UUID, now: datetime,
                         ttl_seconds: int = MERCHANT_TOKEN_TTL_SECONDS) -> str
```
- Claims: `iss`, `aud`, `sub = "merchant:<id>"`, `merchant_id` (str), `roles = ["merchant_admin"]`, `iat`, `exp = iat + ttl`,
  and `jti`, a random UUID that differs per call. The header `alg` is `RS256`.
- It verifies with the matching public key (PyJWT, `algorithms=["RS256"]`, audience and issuer set). Verification fails
  with a different key pair.

## R2 — gateway HTTP additions (`app.py`)

- `GatewayAppDeps` gains `jwt_private_key_pem: str | None = None` and
  `readiness: Callable[[], list[str]] = <always ready>`. `readiness` returns the names of the failed components; an
  empty list means ready. The defaults keep the existing `test_app.py` construction valid. With
  `jwt_private_key_pem` None, `POST /v1/merchant-tokens` returns 503 `{"detail": "token issuing not configured"}`.
- `POST /v1/merchant-tokens` (Bearer API key) returns 201
  `{"access_token": str, "token_type": "Bearer", "expires_in": 900}`.
  - The token's `merchant_id` is the API key's merchant.
  - A missing or invalid API key returns 401, and the body does not contain the submitted key.
- `GET /readyz` returns 200 `{"status": "ready"}` when `readiness()` is empty. Otherwise it returns 503
  `{"status": "not_ready", "failed": [...]}`.
- `gateway.health.check_ready(session_factory, redis) -> list[str]`:
  - It returns `"database"` if `SELECT 1` fails and `"redis"` if `PING` fails.
  - It never raises.
  - Test with a Redis client pointed at a closed port (`socket_connect_timeout` ≤ 0.5 s) and with an engine on a bad
    URL.

## R3 — gateway entry points

- `GatewaySettings` gains `jwt_private_key_pem: SecretStr | None = None`. The existing settings tests keep passing.
- `gateway.main.build_app_from_env() -> FastAPI` reads `GatewaySettings`. The secrets directory is
  `GATEWAY_SECRETS_DIR` if set, passed as `_secrets_dir`.
  - With `jwt_private_key_pem` unset it raises `RuntimeError("GATEWAY_JWT_PRIVATE_KEY_PEM is required")`. The app
    does not start without it.
- `gateway.admin.bootstrap_merchant(session_factory, clock, name, api_key_pepper) -> MerchantCredentials` creates and
  commits a merchant. A second call creates another merchant with a different key.
- `gateway.outbox.worker.run_relay(session_factory, redis, clock, stop: threading.Event, poll_interval_seconds=0.2) -> int`
  - It calls `publish_pending` until `stop` is set and returns the total number published.
  - If an iteration raises a Redis or DB error, the loop logs the error class name (not the message), sleeps and
    continues. It does not exit.
  - Test: one pending outbox row, with a stop event set from a thread after the row is published. The test asserts
    the return value is 1 and the stream holds one entry.
- `gateway.outbox.worker.main()` sets `stop` on SIGTERM or SIGINT.

## R4 — merchant token verification (ops-ai `auth.py`)

```python
class InvalidTokenError(Exception): ...
def verify_merchant_token(token: str, public_key_pem: str, now: datetime) -> Scope
```
- A valid R1 token returns `Scope(merchant_id=<the token's merchant_id>)`.
- The following raise `InvalidTokenError`:
  - an expired token (`now` > `exp`, no leeway beyond 0);
  - the wrong `aud` or the wrong `iss`;
  - a missing `exp` or a missing `merchant_id`;
  - a non-UUID `merchant_id`;
  - a token signed by another key pair;
  - `alg: none`;
  - an HS256 token whose HMAC secret is the public key PEM (algorithm confusion);
  - a token whose payload was edited without re-signing;
  - garbage.
- The `InvalidTokenError` message never contains the token.

## R5 — ops-ai HTTP (`app.py`, `config.py`)

- `OpsAiSettings` (env prefix `OPS_AI_`): `database_url` (alias `DATABASE_URL`), `redis_url` (alias `REDIS_URL`),
  `jwt_public_key_pem: str | None = None` and `openai_api_key: SecretStr | None` (alias `OPENAI_API_KEY`, default None).
  `hide_input_in_errors=True`.
- `ops_ai.app.create_app(OpsAiAppDeps(session_factory, llm, jwt_public_key_pem, clock, readiness)) -> FastAPI`. Routes:
  - `GET /healthz` returns 200.
  - `GET /readyz`: same contract as R2. `ops_ai.health.check_ready(session_factory, redis)` behaves the same way.
  - `POST /v1/assistant/ask` takes Bearer `<merchant token>` and the body `{"question": str}` (1–2000 chars, extra
    fields forbidden). It returns 200
    `{"mode": "llm"|"t2_fallback", "text": str|null, "model": str|null, "evidence": [{"ref": str, "summary": str}]}`.
    - The scope comes from the token only. A body field such as `merchant_id` is rejected with 422.
    - The merchant isolation test: merchant A's token never returns evidence that references merchant B's payments,
      with both merchants' facts present.
    - A missing, invalid or expired token returns 401 `{"detail": "invalid token"}`. The body does not echo the token.
    - An `LlmPort` that raises `LlmUnavailableError` gives 200 with `mode == "t2_fallback"`.
- `ops_ai.main.build_app_from_env()`:
  - It uses `MockLlm` when `openai_api_key` is None.
  - It raises `RuntimeError` when `jwt_public_key_pem` is unset.
  - The secrets directory comes from `OPS_AI_SECRETS_DIR`.

## R6 — ops-ai consumer worker (`ingest/worker.py`)

- `run_consumer(session_factory, redis, clock, consumer_name, stop, block_ms=500) -> int` follows the same loop and
  error contract as R3 `run_relay`, around `consume_payments`.
  - Test: publish one gateway event, run the consumer until the projection has the fact, set stop, and assert one
    `payment_facts` row.
- `main()` stops on SIGTERM or SIGINT. The consumer name is `ops-ai-<hostname>`.

## R7 — demo secrets (`scripts/demo_secrets.py`, stdlib + `cryptography`)

`write_demo_secrets(root: Path) -> bool` is idempotent and creates only missing files. It returns True if it wrote
anything.
- Files written:
  - `root/gateway/gateway_api_key_pepper` and `root/gateway/gateway_card_fingerprint_key`: 48+ random URL-safe chars
    each;
  - `root/gateway/gateway_jwt_private_key_pem`: an RSA 2048 PKCS8 PEM;
  - `root/ops-ai/ops_ai_jwt_public_key_pem`: the matching public key.
- File modes: private files `0o600`, the public key `0o644`.
- A second call changes no byte and returns False.
- Two different roots get different secrets.
- The function prints nothing that contains a secret. `main()` prints only which files were created.

## R8 — compose and `make demo` (verified by running it, not by unit tests)

- One image, `docker/app.Dockerfile`: the uv workspace with `--no-dev`, run as a non-root user.
- Compose services:
  - `demo-secrets` (one-shot, R7 into the named volume `demo-secrets`);
  - `migrate` (one-shot, both Alembic histories);
  - `gateway-api` (`127.0.0.1:8080`), `relay`, `ops-ai-api` (`127.0.0.1:8081`), `ops-ai-consumer`.
  The APIs have a healthcheck on `/readyz`. Each service mounts only its own secrets subdirectory, read-only.
- `make demo` brings the services up with `--wait`, then runs the `demo` service (`scripts/demo.py`).
  `scripts/demo.py`:
  - creates a new merchant via `bootstrap_merchant`, so a re-run never collides;
  - signs up `demo+<random>@example.com`;
  - over HTTP: one approved charge, one declined charge (`4000000000000002`), and a retry of the approved charge with
    the same key;
  - checks the retry body is identical;
  - gets a merchant token;
  - waits until the consumer group lag and pending count on `gw.payments.v1` are both 0 (30 s timeout);
  - asks "declined payments" and checks that the evidence holds the declined payment and not the approved one;
  - prints each step with ✓/✗ and exits 0 only if every check passed.
  - The API key and the token are never printed in full. Only the first 8 characters of the API key are printed.
- `make demo` works from a fresh clone without a `.env` file and without `OPENAI_API_KEY`.
