# Web client (B13) — specification (11-04)

Source: `docs/requirements-traceability.md` B13.1–B13.3, `docs/architecture-detail.md` §6, the client architecture
decision of 11-04 (below). Scope: new `web/` app, one new `web` compose service, and one gateway read endpoint
(prerequisite P-1). Depth: Thin — every screen calls the real APIs; no business logic in the client.

## Decisions

1. **Vite + React + TypeScript static SPA, no SSR.** One app, three feature areas
   `web/src/features/{user,merchant,ops}`. Why: the screens are authenticated dashboards with no SEO need, and the
   written plan was already a Vite SPA. A static bundle needs no server runtime, so no extra process ever sees a
   token or PII. Gave up: server rendering and a framework router/data layer (Next.js, Remix). Next.js with static
   export would also work; it is not chosen because nothing here needs it, not because of the hosting target.
2. **Same origin through nginx.** A `web` compose service (nginx, pinned image) serves the built bundle and proxies
   `/api/gw/*` → `gateway-api:8000/*` and `/api/ops/*` → `ops-ai-api:8000/*` (prefix stripped). No CORS is added to
   either service. Port `127.0.0.1:${WEB_PORT:-8082}`.
3. **Production hosting is design-only.** The reference platform stays GCP (Cloud CDN + Cloud Armor in front of
   the services). Cloudflare (Pages for the bundle, Workers routes + WAF in front of the GCP origins) is written in
   `docs/design.md` as an edge alternative. It does not strengthen the 99.999 % claim: it adds a vendor in series.
   If the edge proxies the API and terminates TLS, it **processes** PII and card data in transit, so the design text
   separates where data is processed from where it is stored. Nothing is deployed before submission.
4. **Credentials live in memory only.** Tokens and the merchant API key are held in one module-level store (React
   context backed by a closure). They are never written to `localStorage`, `sessionStorage`, IndexedDB, cookies,
   the URL or the console. Reload or logout drops them. A 401 from any call clears the store and returns to the
   login view. Memory storage does not protect against XSS. CSP is the control for that (decision 7).
5. **Merchant auth: demo-only API key entry.** The gateway merchant routes accept only the merchant API key, so the
   merchant screen asks for it once, keeps it in memory (decision 4), and exchanges it via `POST /v1/merchant-tokens`
   for the 15-minute ops-ai token used by the assistant. The gateway calls still send the API key. This exposes a
   long-lived key to the browser. It is acceptable only for the local demo with the seeded demo merchant and
   fictitious data, and README says so. Production design (written, not built): merchant-user login with TOTP
   issuing a short-lived token with aud `gateway` and scope `merchant:<id>`. Rejected: nginx injecting the key
   server-side, because anyone reaching the port would act as that merchant.
6. **No card entry in the browser.** `POST /v1/payments` takes a raw (test) PAN with the merchant API key: it is a
   server-to-server API. A checkout form would route PAN through the SPA and the page into PCI scope. The card
   charge stays in `make demo` / the API walkthrough. The production design is hosted fields from a separate vault
   origin; the SAQ level depends on the final page layout and is not claimed.
7. **Browser hardening (nginx headers on every response):**
   `Content-Security-Policy: default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:;
   connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'`,
   `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`. `Cache-Control: no-store` on `/api/*`
   responses; the bundle's hashed assets are cacheable and `index.html` is `no-cache`. No inline scripts or styles
   in the build.
8. **Idempotency from the client.** Each confirmed money action (transfer, refund) creates one
   `crypto.randomUUID()` Idempotency-Key when the user confirms. The key is reused for retries of that same
   submission and discarded once a final response arrives. Double-clicking the confirm button sends one request:
   the button is disabled while the request is pending.
9. **Money display.** Amounts are integer minor units end to end. The client formats them with
   `Intl.NumberFormat` and the currency's minor digits (JPY 0, USD/EUR/INR 2). User input is parsed to minor units
   without floating point (string split on the decimal point). Rejected inputs never reach the API.
10. **Toolchain.** Node and npm run only inside a pinned `node` image (compose `web-dev` profile). The lockfile is
    committed, installs use `npm ci`, and nothing is installed on the host. New dependencies go through the
    dependency-auditor lane. Runtime deps: `react`, `react-dom`. Dev deps: `vite`, `@vitejs/plugin-react`,
    `typescript`, `vitest`, `@testing-library/react`, `@playwright/test`. No UI kit and no state library.

## Screens

### User (B13.1)
| Step | API | Notes |
|---|---|---|
| Sign up | `POST /api/gw/v1/signup` | 409 → "email already registered" |
| Log in | `POST /api/gw/v1/sessions` | 401 "mfa required" → show the TOTP field and resubmit; 423 → locked message |
| Enrol TOTP | `POST /api/gw/v1/me/mfa/totp`, `.../verify` | Show `otpauth_uri` as text plus the secret, for an authenticator app. No QR library. |
| Wallets | `GET /api/gw/v1/me/wallets` | available and held per currency |
| Pay (P2P) | `POST /api/gw/v1/me/transfers` | Confirm dialog with recipient, amount and currency; error codes mapped to messages |
| History | `GET /api/gw/v1/me/transfers` | **Prerequisite P-1** |

The user's "pay" is the P2P transfer (decision 6). Refund request and dispute are **not offered on the user
screen**. P2P transfers are final. Card disputes are opened by the card network (`dispute.opened` by
`card_network`). No user-facing refund or dispute API exists. This is listed as a gap in traceability (B13.1:
Partial).

### Merchant (B13.2)
| Area | API | Notes |
|---|---|---|
| Key entry | `POST /api/gw/v1/merchant-tokens` | Validates the key and gets the ops-ai token. Refreshed on 401 from ops-ai. |
| Payments list | `GET /api/gw/v1/payments?limit&cursor&state` | State filter; risk badge via `GET .../{id}/risk` on row open |
| Refund | `POST /api/gw/v1/payments/{id}/refunds` | Only from an open payment row, with a confirm dialog showing the refundable amount (`amount − refunded − disputed − chargeback`); full or partial |
| Settlement summary | `POST /api/ops/v1/assistant/explain-settlement` | Shows the structured fields: gross, expected, net, gap, lines, `verified`, `contract_version`, `fx_rate_id`. The narrative `text` is secondary. |
| Assistant chat | `POST /api/ops/v1/assistant/ask`, `/investigate`, `/investigate-fraud` | Question box with a mode picker. Shows `text`, `mode` (LLM or fallback), `suggested_fixes`, `tool_calls` and the `evidence` list (ref + summary). |

**Refund from the assistant.** The assistant returns no executable action. When an evidence `ref` names a
payment id from this merchant's list, the evidence item shows "Open payment". That opens the same row and the same
confirm dialog. Nothing in an assistant answer can trigger a gateway call by itself. The backend still enforces
authz, refundable amount and idempotency, so client checks are only convenience.

All assistant text is rendered as plain text (React escaping; no `dangerouslySetInnerHTML`, no markdown renderer).

### Ops console (B13.3, an addition)
Not built in this cut. ops-ai has no ops principal (tokens carry exactly one of `merchant_id` / `user_id`) and no
list endpoints for alerts or tickets. Building the console needs a new role and new authz, which is high-risk work
without time for review. The merchant screen's fraud investigation already shows alerts with evidence. Design:
an `ops_analyst` role issued by an SSO-backed login, with read endpoints for alerts, tickets and flagged
transactions. Traceability B13.3: Design.

## Prerequisite P-1 — `GET /v1/me/transfers` (gateway)

- Auth: the same session dependency as `/v1/me/wallets`, including the TOTP rule (amr must carry "otp" once TOTP
  is enabled).
- Returns the user's transfers where the user is the sender or the recipient, newest first:
  `{items: [{transfer_id, direction: "sent"|"received", counterparty_email_masked, amount_minor, currency,
  created_at}], next_cursor}`, with `limit` 1–100 (default 20) and an opaque cursor. An invalid cursor gives 400.
- `counterparty_email_masked` shows the first character and the domain (`a***@example.com`). A user never sees
  another user's full email or account ids.
- `Cache-Control: no-store`.
- Tests: own transfers only (user B's transfers never appear for A), both directions, ordering and paging, the TOTP
  rule, invalid cursor. Review lanes: security-auditor (authz read).

## Tests

- Unit (vitest, `web/src/**/__tests__/`):
  - the credential store never touches the storage APIs: spy on `Storage.prototype.setItem` and `document.cookie`;
  - the API client adds `Authorization` and `Idempotency-Key` and reuses the key on retry;
  - a 401 clears the store;
  - error code → message mapping;
  - the minor-unit parser and formatter (JPY, USD, rejects `1.234` for USD, rejects negatives and exponents);
  - the confirm button sends exactly one request on a double click.
- Smoke (Playwright, `web/e2e/`, against `docker compose up` with the seeded data):
  - user: sign up → enrol TOTP (code computed in the test from the secret) → log in with TOTP → wallets → transfer
    → the transfer shows in history;
  - merchant: enter the demo key → payments list → open a payment → refund with confirm → the row shows refunded;
  - assistant: ask an SQ1 question → the answer shows evidence refs;
  - headers: the CSP header is present on `/`, and `no-store` is present on `/api/gw/v1/me/wallets`.
- `make ci` gains `web-ci` (typecheck `tsc --noEmit`, eslint, vitest, `vite build`) in the node container. The
  Playwright smoke runs from `make web-smoke`, not in `make ci`, because it needs the full stack.

## Known gaps (written into README and traceability)

- The merchant API key is exposed to the browser (local demo only; production design in decision 5).
- No card checkout UI (decision 6).
- No user refund or dispute screen.
- No ops console (B13.3 Design).
- Memory-only tokens do not survive a reload and do not protect against XSS.
- `POST /v1/user-tokens` issues an ops-ai user token from email and password without TOTP. The web client does not
  use it, and the gap is recorded for the production design.
