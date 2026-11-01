# Product Brief

Status: confirmed 2026-11-01. Source: `docs/caes-study.pdf` (Overview, Requirement 1, Sample queries).

## Purpose

Provide safe and reliable payments, and when a payment fails, is declined, looks fraudulent, or settles
differently than expected, let merchants and operations staff quickly decide the cause and the response
with evidence, and carry out that response within safe limits.

"Within safe limits" is the role of Agentic AI: the assistant may call tools, send notifications and open
tickets. It does not move money on its own (no autonomous refunds).

## Problem (from the brief)

Support teams and merchants struggle to quickly identify transaction failures, fraud signals, settlement
issues, or processing bottlenecks.

The four sample queries are instances of this problem:

| Sample query | Problem type |
|---|---|
| 15% of a merchant's card payments failing this afternoon | Transaction failure |
| International transfer declined despite sufficient funds | Decline / rule trigger |
| Series of micro-transactions that may be card testing | Fraud signal |
| Merchant payout differs from the expected amount | Settlement issue |

## Users

| User | Role in the product |
|---|---|
| Merchant | Primary. Integrates through the API, checks payments and payouts, asks the assistant |
| Operations staff (support, fraud) | Primary. Diagnoses failures, reviews flagged transactions, handles tickets |
| Consumer | Uses wallets, P2P and merchant payments, refunds and disputes |

Financial institutions appear in the brief as counterparties (issuers, acquirers, providers), not as a
primary user of this product.

## Mandatory capabilities (Requirement 1 only)

Every one of the 13 Basic items maps to exactly one group. Detailed sub-items and tests:
`docs/requirements-traceability.md`.

| Group | Basic items |
|---|---|
| 1. Payment core | B1 accounts + MFA + wallets; B2 P2P and merchant payments, multi-currency; B3 history, refunds, disputes |
| 2. Merchant integration | B8 REST/gRPC APIs, input validation guardrails, idempotency enforcement |
| 3. Risk | B7 fraud detection, risk scoring, flagging |
| 4. Operations intelligence | B4 NL failure-reason detection; B5 RAG support assistant; B6 hybrid search; B9 tool calling; B10 memory and session |
| 5. Operations workflow | B11 notifications; B12 ticket routing |
| 6. Interfaces | B13 user and merchant screens (plus a support screen, an addition) |

## Requirements vs design decisions

Only the 13 Basic items above are "mandatory". The following are kept separate so the requirement set is
not inflated:

| Item | Kind |
|---|---|
| Double-entry ledger | Design choice |
| Zero double charge, atomic transactions | Correctness condition (from the NFR table) |
| Card-testing detection | Verification scenario (from a sample query) |

## Scope of this submission

- No real money movement. Payment functions are demonstrated against a simulator and sandbox providers.
- Requirement 2 (Advanced) defines how far operations intelligence acts autonomously; NFRs define how the
  payment core scales. Neither is part of the purpose statement.
- Scale and compliance are reported in two columns: demonstrated in this submission, and production design only.
