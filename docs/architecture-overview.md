# Architecture overview: data flow (DL1)

The high-level view the brief asks for: how data moves from the synthetic dataset through ingestion, the AI/ML layer
and the APIs to the user-facing interface. Rendered as [architecture-overview.pdf](architecture-overview.pdf) and
[architecture-overview.jpg](architecture-overview.jpg). The deployment-level diagram with every container, store, stream
and port is [architecture.pdf](architecture.pdf) ([source](architecture.md)).

```mermaid
flowchart LR
  subgraph S["1 · Data sources"]
    ds["Synthetic dataset<br/>scripts/generate_dataset.py, seed 42<br/>12000 records: data/synthetic/transactions.csv"]
    live["Live traffic<br/>browser, merchant server"]
  end

  subgraph G["2 · Payment gateway (gateway service)"]
    api["REST :8080 and gRPC :50051<br/>validation guardrails, Idempotency-Key"]
    core["Payments, P2P, wallets, multi-currency<br/>double-entry ledger, card tokens (no PAN)<br/>refunds, disputes, rule risk score"]
    prov["Provider port<br/>SimulatorProvider (default)<br/>Stripe test-mode adapter (needs a test key)"]
    outbox["Transactional outbox + audit log"]
  end

  subgraph I["3 · Ingestion"]
    streams[("Redis Streams<br/>gw.payments / disputes /<br/>compliance / notifications")]
    consumer["ops-ai-consumer<br/>deduped projections,<br/>search index, fraud_watch, tickets"]
    seed["seed (one-shot)<br/>same apply_payment_event"]
  end

  subgraph D["Data stores"]
    pg[("Postgres 16 + pgvector<br/>gateway schema · ops_ai schema<br/>tsvector + vector index")]
  end

  subgraph A["4 · AI / ML layer (ops-ai service)"]
    search["Hybrid search<br/>full-text + pgvector, RRF, rerank"]
    tools["Read-only tools<br/>failures, payment status,<br/>fraud, settlement, transfers"]
    assist["Assistants SQ1–SQ4 · RAG /ask<br/>agents: fraud, settlement,<br/>dispute, compliance"]
    judge["LLM-as-judge · eval suite<br/>feedback loop, analytics<br/>(forecast, chargeback, anomaly)"]
    llm["LLM port<br/>OpenAI gpt-4.1-mini (optional)<br/>MockLlm by default"]
  end

  subgraph U["5 · APIs and user interface"]
    opsapi["ops-ai REST :8081<br/>scoped by gateway-issued token"]
    web["Web SPA (nginx)<br/>user · merchant · ops"]
    notify["Notifiers<br/>payment confirmations,<br/>signed alert webhooks"]
  end

  live --> api --> core
  core --> prov
  core --> outbox --> streams --> consumer --> pg
  core --> pg
  ds --> seed --> pg
  pg --> search --> assist
  pg --> tools --> assist
  assist <--> llm
  assist --> judge
  assist --> opsapi --> web
  api --> web
  streams --> notify
  consumer --> notify
```

Numbered groups follow the brief's order: data source → ingestion → AI/ML → APIs → interface. The LLM only narrates
evidence that the tools computed; with no API key the stack runs end to end on MockLlm.
