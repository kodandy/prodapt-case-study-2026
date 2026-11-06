# Architecture

Deliverable DL1. This page shows what runs today in `compose.yaml` and what the code under `services/` does.
The design target is in [`architecture-detail.md`](architecture-detail.md). Anything here that exists only in that
design is drawn with dashed lines and labeled **planned**.

Rendered copies of the runtime topology (§1): [`architecture.png`](architecture.png) and
[`architecture.pdf`](architecture.pdf). Rebuild them with `make docs-diagram`, which runs mmdc in a pinned Docker
image and renders the first Mermaid block on this page.

## 1. Runtime topology (`make demo` / `make web-up`)

```mermaid
flowchart LR
  classDef planned stroke-dasharray: 5 5,color:#666,fill:#f6f6f6
  classDef oneshot fill:#fff8e1,stroke:#c9a227
  classDef store fill:#e8f1fb,stroke:#3b78c3
  classDef ext fill:#eef7ee,stroke:#3c8c3c

  browser(["Browser<br/>user / merchant / ops SPA"])
  merchantsrv(["Merchant server<br/>server-to-server"])
  receiver(["Alert receiver<br/>operator-configured HTTPS webhook"]):::ext
  openai(["OpenAI API<br/>Responses: gpt-4.1-mini, nano fallback<br/>Embeddings: text-embedding-3-small<br/>(document vectors by backfill CLI)<br/>only when OPENAI_API_KEY is set,<br/>otherwise MockLlm + LocalHashEmbedder"]):::ext

  subgraph host["compose project payplat · host ports on 127.0.0.1"]
    web["web<br/>nginx + Vite SPA bundle<br/>:8082 → 80 (web-tls :8443, profile tls)"]

    subgraph gw["gateway service"]
      gwapi["gateway-api<br/>FastAPI REST :8080<br/>auth · RBAC · payments · ledger<br/>in-process SimulatorProvider"]
      gwgrpc["gateway-grpc<br/>merchant gRPC API :50051"]
      relay["relay<br/>outbox → Redis Streams"]
      notifier["notifier<br/>payment confirmations"]
    end

    subgraph ops["ops-ai service (AI / ML)"]
      opsapi["ops-ai-api<br/>FastAPI :8081<br/>assistants SQ1–SQ4 · hybrid search<br/>agents · fraud rules · alerts"]
      consumer["ops-ai-consumer<br/>ingest: projections, search index,<br/>fraud_watch, tickets"]
      opsnotifier["ops-ai-notifier<br/>alert outbox → signed webhook"]
    end

    subgraph pg["postgres · pgvector/pgvector:pg16"]
      sgw[("schema gateway<br/>users, merchants, api_keys, ops_users, user_totp, kyc<br/>payments, attempts, refunds, disputes, payouts<br/>idempotency_keys, card_tokens (no PAN)<br/>ledger accounts, journal_entries, postings, fx_rates<br/>outbox, audit_log, notifications")]
      ssim[("schema simulator<br/>charges")]
      sops[("schema ops_ai<br/>payment_facts, transfer_facts, settlement_entries<br/>search_documents (tsvector + pgvector)<br/>incidents, tickets, feedback, merchant_memory<br/>fraud_alerts, fraud_rule_versions, alert_notifications<br/>ops_alerts, anomaly_alerts, agent_messages")]
    end

    redis[("redis 7.4<br/>idem:* in-flight locks<br/>streams gw.payments.v1, gw.notifications.v1, .dead<br/>ops_ai:agent_messages (+ per-tenant)")]

    secrets["demo-secrets<br/>one-shot"]
    migrate["migrate<br/>one-shot: alembic gateway + ops-ai"]
    seed["seed<br/>one-shot: synthetic dataset<br/>payments, transfers, settlement, agents"]
    vol[("volume demo-secrets<br/>read-only mounts")]

    subgraph mon["profile monitoring"]
      prom["Prometheus :9090"]
      graf["Grafana :3000"]
    end
  end

  browser -->|"HTTP :8082"| web
  web -->|"/api/gw/*"| gwapi
  web -->|"/api/ops/*"| opsapi
  merchantsrv -->|"POST /v1/payments<br/>API key + Idempotency-Key"| gwapi
  merchantsrv -->|"gRPC"| gwgrpc

  gwapi -->|"SQL"| sgw
  gwapi -->|"SQL (provider ledger)"| ssim
  gwgrpc -->|"SQL"| sgw
  gwapi -->|"SET NX PX idempotency lock"| redis
  relay -->|"outbox rows<br/>FOR UPDATE SKIP LOCKED"| sgw
  relay -->|"XADD"| redis
  notifier -->|"outbox → notifications"| sgw

  seed -->|"synthetic dataset applied<br/>with the consumer's apply_payment_event"| sops
  consumer -->|"XREADGROUP · XACK · dead-letter"| redis
  consumer -->|"upsert projections, index,<br/>fraud alerts + notification outbox<br/>(one tx with dedupe)"| sops
  opsapi -->|"SQL read scoped by token tenant"| sops
  opsapi -->|"agent stream mirror"| redis
  opsapi -.->|"redacted evidence · query embeddings"| openai
  opsnotifier -->|"claim one row per tx<br/>SKIP LOCKED"| sops
  opsnotifier -->|"POST HMAC-SHA256 signed,<br/>Idempotency-Key"| receiver

  secrets --> vol
  vol -.->|"JWT private key, API-key pepper,<br/>card fingerprint key, TOTP key"| gwapi
  vol -.->|"JWT public key"| opsapi
  migrate --> sgw
  migrate --> ssim
  migrate --> sops

  prom -->|"scrape /metrics"| gwapi
  prom -->|"scrape"| opsapi
  prom -->|"scrape :9100"| relay
  prom -->|"scrape :9100"| consumer
  graf --> prom

  subgraph planned["Planned (not built yet)"]
    qapi["Query / Command API<br/>ops-ai-api → gateway-api"]:::planned
    otel["OTel Collector · trace backend"]:::planned
  end

  class secrets,migrate,seed oneshot
  class sgw,ssim,sops,redis,vol store
```

**Compose services drawn above**

| Service | Kind | Command / image | Talks to |
|---|---|---|---|
| `postgres` | store | `pgvector/pgvector:pg16` | — |
| `redis` | store | `redis:7.4-alpine` | — |
| `demo-secrets` | one-shot | `scripts/demo_secrets.py` | writes the `demo-secrets` volume |
| `migrate` | one-shot | Alembic, `services/gateway` then `services/ops-ai` | Postgres |
| `seed` | one-shot | `ops_ai.seed`, `ops_ai.transfers`, `ops_ai.settlement`, `ops_ai.agents` | Postgres (`ops_ai`) |
| `gateway-api` | long-running | `gateway.main:build_app_from_env` | Postgres (`gateway`, `simulator`), Redis |
| `gateway-grpc` | long-running | `gateway.merchant_api.grpc_server` (:50051) | Postgres (`gateway`) |
| `relay` | long-running | `gateway.outbox.worker` | Postgres (`gateway.outbox`), Redis streams |
| `notifier` | long-running | `gateway.notifications.worker` | Postgres (`gateway`) |
| `ops-ai-api` | long-running | `ops_ai.main:build_app_from_env` | Postgres (`ops_ai`), Redis (readiness, agent streams), optional OpenAI |
| `ops-ai-consumer` | long-running | `ops_ai.ingest.worker` | Redis streams, Postgres (`ops_ai`) |
| `ops-ai-notifier` | long-running | `ops_ai.notify` | Postgres (`ops_ai.alert_notifications`), external webhook receiver when `OPS_AI_ALERT_WEBHOOK_URL` is set |
| `web` | long-running | `web/` build, nginx | `gateway-api`, `ops-ai-api` |
| `web-tls` | profile `tls` | nginx :8443 with a `dev-cert` one-shot | same as `web` |
| `prometheus`, `grafana` | profile `monitoring` | `ops/prometheus/prometheus.yml` | scrapes `gateway-api`, `relay`, `ops-ai-api`, `ops-ai-consumer` |

Not drawn: the profile-only helpers `dev` (toolchain), `demo` (runs `scripts/demo.py` against both APIs), `web-dev`
(Node), `web-smoke` (Playwright) and `k6` (profile `perf`). They are not part of the running platform.

**Boundaries the code enforces**

- The two services share no Python code except `contracts/` (event schemas), and each owns its schema.
  ops-ai never reads the `gateway` schema; the only live data path from gateway to ops-ai is the outbox stream.
- ops-ai verifies the JWTs that gateway issues, using the public key from the shared secrets volume. It issues none.
- nginx is the only browser entry. It returns 404 for `POST /api/gw/v1/payments` (raw PAN, server-to-server only),
  `/api/gw/v1/user-tokens`, `/api/gw/v1/disputes*`, `/api/ops/v1/assistant/investigate-transfer` and the API docs.
- `gw.notifications.v1` is published but has no consumer yet. Notifications are read from `gateway.notifications`.

## 2. Card payment (`POST /v1/payments`, sync, no LLM)

```mermaid
sequenceDiagram
  autonumber
  participant M as Merchant server
  participant G as gateway-api
  participant R as Redis
  participant DB as Postgres gateway
  participant S as SimulatorProvider (simulator.charges)
  M->>G: POST /v1/payments (Bearer API key, Idempotency-Key, card)
  G->>DB: authenticate hashed API key
  G->>G: validate (Luhn, test-PAN allowlist), fingerprint
  G->>R: SET idem:{merchant}:{key} NX PX
  alt lock held by another request
    G->>DB: wait for stored response (≤ wait budget)
    G-->>M: stored body, or 409 idempotency_in_progress
  end
  G->>DB: tx1 – idempotency_keys row, card_tokens (token, BIN, last4), risk score (rules), payment + attempt
  alt risk blocked
    G->>DB: payment declined risk_blocked, outbox + audit, stored response
    G-->>M: 201 declined
  else allowed
    G->>S: charge(provider key = attempt id), outside any transaction, with timeout
    S-->>G: succeeded / declined / failed / no answer (unknown)
    G->>DB: tx2 – UPDATE attempt WHERE state IN (created, unknown)<br/>winner writes balanced journal, outbox gw.payments.v1, audit_log, stored response
    G-->>M: 201 payment
  end
  G->>R: release lock (compare-and-delete)
```

- Correctness does not depend on Redis: the unique `idempotency_keys` row in tx1 still yields one charge with Redis down.
- The PAN is never stored. The event carries BIN, last4 and a keyed `card_ref`, no PAN.
- **Planned:** the background reconciler for attempts left in `created`/`unknown` (only the tx2 guard exists today).

## 3. P2P transfer (`POST /v1/me/transfers`)

```mermaid
sequenceDiagram
  autonumber
  participant B as Browser (user SPA)
  participant N as web (nginx)
  participant G as gateway-api
  participant DB as Postgres gateway
  B->>N: POST /api/gw/v1/sessions (email, password, TOTP if enabled)
  N->>G: POST /v1/sessions
  G-->>B: session token (kept in memory only)
  B->>N: POST /api/gw/v1/me/transfers (recipient email, amount, currency, Idempotency-Key)
  N->>G: POST /v1/me/transfers
  G->>DB: one transaction – idempotency row,<br/>SELECT … FOR UPDATE on both account_balances rows (ascending id),<br/>balance check, transfers row, journal + postings, audit_log, stored response
  G-->>B: transfer result, stored body on retry, 422 on key reuse
```

- Same-currency, internal, synchronous: no hold, no provider call, no outbox event.
- The SQ2 assistant (`/v1/assistant/investigate-transfer`) reads `ops_ai.transfer_facts`, which `seed` loads from the
  synthetic dataset, not from this live path. It needs a user token and nginx does not expose it to the browser.

## 4. Refund (`POST /v1/payments/{id}/refunds`)

```mermaid
sequenceDiagram
  autonumber
  participant B as Browser (merchant SPA)
  participant N as web (nginx)
  participant G as gateway-api
  participant DB as Postgres gateway
  B->>N: POST /api/gw/v1/payments/{id}/refunds (API key, Idempotency-Key, amount_minor?)
  N->>G: POST /v1/payments/{id}/refunds
  G->>DB: one transaction – SELECT … FOR UPDATE on the payment,<br/>check refunded ≤ captured, refunds row, reversal journal, audit_log, stored response
  G-->>B: refund (full or partial), 409 over refundable, stored body on retry, 422 on key reuse
```

- No provider call and no outbox event for refunds today.
- Disputes: `GET /v1/disputes/{id}` and evidence submission exist in gateway-api; nginx keeps them off the browser.

## 5. Events into ops-ai

```mermaid
sequenceDiagram
  autonumber
  participant DB as Postgres gateway.outbox
  participant RL as relay
  participant NT as notifier
  participant R as Redis Streams
  participant C as ops-ai-consumer
  participant O as Postgres ops_ai
  Note over DB: rows written in tx2 of a charge (topic gw.payments.v1)
  loop every 0.2 s
    RL->>DB: SELECT unpublished FOR UPDATE SKIP LOCKED (id order)
    RL->>R: XADD gw.payments.v1 / gw.notifications.v1 {event_id, payload}
    RL->>DB: mark published (rows are kept)
  end
  loop every 0.2 s
    NT->>DB: unprocessed gw.payments.v1 rows
    NT->>DB: notifications (payment_confirmation, suspicious_activity)<br/>+ outbox gw.notifications.v1, deduped by source event
  end
  loop consumer group ops-ai.payment-facts
    C->>R: XREADGROUP (first own pending "0", then new ">")
    alt payload fails PaymentEventV1
      C->>R: XADD gw.payments.v1.dead, XACK
    else valid
      C->>O: one tx – processed_events dedupe, payment_facts (highest entity_version wins),<br/>search_documents, fraud_watch → fraud_alerts + tickets
      C->>R: XACK
    end
  end
```

- Delivery is at least once. A crash between `XADD` and marking published re-sends; the consumer's
  `processed_events` row in the same transaction makes the re-send a no-op.
- Historical data reaches `ops_ai` through `seed`, which runs the same `apply_payment_event` on the synthetic dataset.
  **Planned:** the backfill importer that writes history through the gateway (architecture-detail §2).

## 6. Ops assistant (`/v1/assistant/*`)

```mermaid
sequenceDiagram
  autonumber
  participant B as Browser (merchant SPA)
  participant N as web (nginx)
  participant A as ops-ai-api
  participant O as Postgres ops_ai
  participant L as LlmPort (OpenAiLlm or MockLlm)
  B->>N: POST /api/gw/v1/merchant-tokens (API key)
  N-->>B: merchant JWT (signed by gateway-api)
  B->>N: POST /api/ops/v1/assistant/ask | investigate (Bearer merchant JWT, question)
  N->>A: POST /v1/assistant/...
  A->>A: verify JWT with gateway public key → Scope(merchant_id)
  A->>O: deterministic read-only tools, scoped to merchant or user<br/>(aggregate_failures, compare_baseline, search_incidents,<br/>analyze_fraud, explain_settlement, get_payment_status)
  A->>L: question + evidence items (card-like digit runs masked, evidence quoted as data)
  alt LLM answers within budget
    L-->>A: narrated text citing [ref]s
  else LLM unavailable / over budget
    A->>A: t2_fallback – evidence only
  end
  A-->>B: mode, text, tool_calls, evidence, suggested fixes
```

- Routes: `ask`, `investigate` (SQ1), `investigate-fraud` (SQ3), `explain-settlement` (SQ4) take a merchant token;
  `investigate-transfer` (SQ2) takes a user token. The SPA calls `ask` and `investigate`.
- The tools decide what data is read. The LLM only narrates the evidence it is given; it has no tools and cannot
  write. With no `OPENAI_API_KEY`, `MockLlm` answers (model `mock`).
- **Planned:** model-driven tool loop and answer-contract checker, Redis session memory, `get_payment_status` through
  the gateway Query API (today it reads `ops_ai.transfer_facts`), routing-override proposals through the Command API (architecture-detail §4–§5).
