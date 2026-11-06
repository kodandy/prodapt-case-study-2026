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
| Payments list | `GET /api/gw/v1/payments?limit&cursor&state` | State filter offers exactly the API states (created, succeeded, declined, failed, unknown) with readable labels; risk badge via `GET .../{id}/risk` on row open. Loading, the error (with Retry) and the rows are mutually exclusive. Only the latest request writes the list, so a slow answer for an earlier filter is dropped; changing the filter resets the cursor. Load more is disabled while a page is in flight and appended rows are deduplicated by `payment_id`. |
| Refund | `POST /api/gw/v1/payments/{id}/refunds` | Only from an open payment row of a refundable payment (`succeeded`; any other state shows no refund control), with a confirm dialog showing the refundable amount (`amount − refunded − disputed − chargeback`); full or partial. After a refund the detail shows the running totals from the response (`payment_refunded_minor`, `payment_refundable_minor`) at once, so the old amount cannot be refunded again. |
| Settlement summary | `POST /api/ops/v1/assistant/explain-settlement` | Shows the structured fields: gross, expected, net, gap, lines, `verified`, `contract_version`, `fx_rate_id`. The narrative `text` is secondary. |
| Assistant chat | `POST /api/ops/v1/assistant/ask`, `/investigate`, `/investigate-fraud` | Question box with a mode picker. Shows `text`, `mode` (LLM or fallback), `suggested_fixes`, `tool_calls` and the `evidence` list (ref + summary). In the Ask mode a "Grounded citations" checkbox sends `?grounded=true` and shows each kept sentence with its refs and verbatim excerpts, and the dropped sentences (R1-5, sq1.md §12). |

**Refund from the assistant.** The assistant returns no executable action. When an evidence `ref` names a
payment id from this merchant's list, the evidence item shows "Open payment". That opens the same row and the same
confirm dialog. Nothing in an assistant answer can trigger a gateway call by itself. The backend still enforces
authz, refundable amount and idempotency, so client checks are only convenience.

All assistant text is rendered as plain text (React escaping; no `dangerouslySetInnerHTML`, no markdown renderer).

### Ops console (B13.3, an addition)
Two ways in, both under the "Ops console" tab:

- **Ops staff sign-in (ops principal).** `OpsSignIn` asks for email, password and the 6-digit authenticator code and
  calls the existing gateway login `POST /api/gw/v1/ops/sessions` (argon2id + TOTP, 900 s session, lockout answers
  429). The client refuses to send without a 6-digit code, but the server stays the authority for TOTP. With the
  session token it calls the existing `POST /api/gw/v1/ops/ops-ai-tokens` and keeps both tokens in a separate
  memory-only store (`web/src/lib/opsSession.ts`, never web storage, never logged, never rendered). The user is
  signed in only when both calls succeed. The console then shows cross-merchant anomaly alerts
  (`GET /api/ops/v1/ops/analytics/alerts`, polled every 15 s with the `since` cursor) and, for a merchant id typed in
  or picked from an alert, the settlement forecast and chargeback risk (`/v1/ops/analytics/forecast` and
  `/chargeback-risk` with `merchant_id`). Every ops-ai call carries the ops-ai token, never the session token and never
  the merchant key. A 401 on the ops-ai token triggers one refresh through the session; a 401 that survives, a 401
  on the refresh, or a 401 on the session clears the ops store and returns to the sign-in form. A refresh that fails
  otherwise (503, network error, 403) keeps the session and shows the original error. Each request remembers the
  session it started under, so a late 401 from before a sign-out and re-sign-in never clears or refreshes the newer
  session. The client refuses (throws) to send the session token anywhere but the gateway or the ops-ai token
  anywhere but ops-ai, and a body that cannot be read counts as a network error. A 403 keeps the session and shows
  "Your role cannot open this view." No server endpoint was added and nginx needed no change: `/api/gw/` and
  `/api/ops/` already proxy these paths, and the routes nginx 404s on purpose stay blocked.
- **Merchant-scoped demo.** Without an ops account, the merchant API key opens the earlier panels (NL failure query,
  flagged transactions, ticket queue, alerts) scoped to that one merchant and labelled as having no ops role.

Residuals: there is no ops logout endpoint, so "Sign out" drops the tokens in the browser but the session stays valid
on the server for up to 900 s, and the stateless ops-ai token stays valid for up to 900 s after an ops user is
disabled; ops reads and ops-ai token issuance are not audited (`docs/specs/analytics.md` §4); tickets, the failure
query and the flagged scan remain merchant-only routes, so the ops principal does not see them; approvals are not
built; there is no Playwright smoke for ops sign-in because no make target provisions an ops user with a TOTP secret.

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
- Ops console is partial: the ops principal sees cross-merchant alerts and per-merchant forecast and chargeback
  risk only; no ops logout endpoint, no approvals, no Playwright ops smoke (B13.3 Partial).
- Memory-only tokens do not survive a reload and do not protect against XSS.
- `POST /v1/user-tokens` issues an ops-ai user token from email and password without TOTP. The web client does not
  use it, and the gap is recorded for the production design.
