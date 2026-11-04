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

  browser(["Browser<br/>user / merchant SPA"])
  merchantsrv(["Merchant server<br/>server-to-server"])

  subgraph host["compose project payplat · host ports on 127.0.0.1"]
    web["web<br/>nginx + Vite SPA bundle<br/>:8082 → 80"]

    subgraph gw["gateway processes"]
      gwapi["gateway-api<br/>FastAPI / uvicorn<br/>:8080 → 8000<br/>in-process SimulatorProvider"]
      relay["relay<br/>gateway.outbox.worker"]
      notifier["notifier<br/>gateway.notifications.worker"]
    end

    subgraph ops["ops-ai processes"]
      opsapi["ops-ai-api<br/>FastAPI / uvicorn<br/>:8081 → 8000"]
      consumer["ops-ai-consumer<br/>ops_ai.ingest.worker"]
    end

    subgraph pg["postgres · pgvector/pgvector:pg16 · :55432 → 5432"]
      sgw[("schema gateway<br/>users, merchants, api_keys, user_totp<br/>payments, payment_attempts, refunds, disputes<br/>idempotency_keys, card_tokens<br/>ledger_accounts, account_balances, transfers<br/>journal_entries, postings<br/>outbox, audit_log<br/>notifications, notification_processed_events")]
      ssim[("schema simulator<br/>charges")]
      sops[("schema ops_ai<br/>payment_facts, processed_events<br/>search_documents (tsvector)<br/>incidents, transfer_facts, settlement_entries<br/>fraud_alerts, tickets")]
    end

    redis[("redis (7.4-alpine) :56379 → 6379<br/>idem:* in-flight locks<br/>streams gw.payments.v1, gw.notifications.v1<br/>gw.payments.v1.dead")]

    secrets["demo-secrets<br/>one-shot"]
    migrate["migrate<br/>one-shot: alembic gateway + ops-ai"]
    seed["seed<br/>one-shot: ops_ai.seed, .transfers, .settlement"]
    vol[("volume demo-secrets<br/>gateway/ · ops-ai/ (read-only mounts)")]
  end

  openai(["OpenAI Responses API<br/>only when OPENAI_API_KEY is set,<br/>otherwise MockLlm in-process"])

  browser -->|"HTTP :8082"| web
  web -->|"/api/gw/* (POST /v1/payments denied)"| gwapi
  web -->|"/api/ops/*"| opsapi
  merchantsrv -->|"POST /v1/payments<br/>API key + Idempotency-Key"| gwapi

  gwapi -->|"SQL"| sgw
  gwapi -->|"SQL (provider ledger)"| ssim
  gwapi -->|"SET NX PX idempotency lock"| redis
  relay -->|"read unpublished outbox rows<br/>FOR UPDATE SKIP LOCKED"| sgw
  relay -->|"XADD by topic"| redis
  notifier -->|"read gw.payments.v1 outbox rows<br/>write notifications + gw.notifications.v1 outbox rows"| sgw

  consumer -->|"XREADGROUP ops-ai.payment-facts<br/>XACK / dead-letter"| redis
  consumer -->|"upsert projections, fraud_watch,<br/>tickets (one tx with dedupe)"| sops
  opsapi -->|"SQL read (scoped by merchant / user)"| sops
  opsapi -->|"readiness ping"| redis
  opsapi -.->|"evidence only (redacted)"| openai

  secrets --> vol
  vol -.->|"JWT private key, API-key pepper,<br/>card fingerprint key, TOTP key"| gwapi
  vol -.->|"JWT public key"| opsapi
  migrate --> sgw
  migrate --> ssim
  migrate --> sops
  seed -->|"synthetic dataset applied<br/>with the consumer's apply_payment_event"| sops

  subgraph planned["Planned (not built yet)"]
    qapi["Query / Command API<br/>ops-ai-api → gateway-api"]:::planned
    grpc["gRPC merchant API<br/>on gateway-api"]:::planned
    vec["pgvector embeddings + RRF<br/>in ops_ai (FTS only today)"]:::planned
    obs["Prometheus · OTel Collector · Grafana"]:::planned
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
| `seed` | one-shot | `ops_ai.seed`, `ops_ai.transfers`, `ops_ai.settlement` | Postgres (`ops_ai`) |
| `gateway-api` | long-running | `gateway.main:build_app_from_env` | Postgres (`gateway`, `simulator`), Redis |
| `relay` | long-running | `gateway.outbox.worker` | Postgres (`gateway.outbox`), Redis streams |
| `notifier` | long-running | `gateway.notifications.worker` | Postgres (`gateway`) |
| `ops-ai-api` | long-running | `ops_ai.main:build_app_from_env` | Postgres (`ops_ai`), Redis (readiness), optional OpenAI |
| `ops-ai-consumer` | long-running | `ops_ai.ingest.worker` | Redis streams, Postgres (`ops_ai`) |
| `web` | long-running | `web/` build, nginx | `gateway-api`, `ops-ai-api` |

Not drawn: the profile-only helpers `dev` (toolchain), `demo` (runs `scripts/demo.py` against both APIs), `web-dev`
(Node) and `web-smoke` (Playwright). They are not part of the running platform.

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
