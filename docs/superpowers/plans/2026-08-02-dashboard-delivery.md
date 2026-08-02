# Same-origin dashboard delivery plan

> The accepted product boundary is `wiki/research/chatbot-dashboard-decision.md`.
> The dashboard is deliberately a read-only presentation layer over the existing
> runtime and typed Facade.  At launch, Bank Rate is the only numerically
> supported market signal; all other requested coverage must be shown as
> unavailable rather than invented.

## Success criteria

1. A browser can open one same-origin page, create a runtime session, submit a
   Chinese market question, and render the final host-validated artifact.
2. The dashboard overview calls the typed Facade directly, shows the latest
   Bank Rate with source/as-of/freshness metadata, and explicitly labels
   unsupported rent, vacancy, transaction and news coverage as unavailable.
3. The page never puts the bearer token in a URL and uses authenticated `fetch`
   streaming rather than browser `EventSource`, which cannot attach the header.
4. Unit tests cover the overview API and static-asset serving; a real local
   browser run captures the Bank Rate and TC-01 unavailable flows.

## Implementation steps

### 1. Add a bounded dashboard read projection

Files: `agent-runtime/src/app.ts`, `agent-runtime/src/http.ts`, new focused
test(s).

* Build a versioned `dashboard_overview.v1` response from the already trusted
  `FacadeLauncher`, scoped to the authenticated session principal and scope.
* Query only `uk.bank-rate-current` and `describe_market_data`; preserve Facade
  citations and freshness instead of copying raw database access into Node.
* Add a bearer-authenticated route at
  `GET /v1/sessions/:id/dashboard/overview`.

Verify: HTTP unit tests prove authorization, response shape, canonical Bank
Rate projection, and unavailable coverage projection.

### 2. Add static UI and the executable runtime server

Files: new `agent-runtime/public/index.html`, `app.js`, `styles.css`; new
`agent-runtime/src/server.ts`; `agent-runtime/src/http.ts`; `package.json`.

* Serve a fixed allowlist of UI assets from the same Node server.
* Build the “London Market Desk” UI: overview cards, source/freshness context,
  chat transcript, suggested prompts, turn state, final artifact and source
  drawer.
* Parse SSE through `fetch` with an in-memory bearer token. Render only host
  artifacts as market answers; never manufacture data from model prose.

Verify: typecheck and UI/static-route tests pass; browser opens the real page,
  runs a supported Bank Rate question and TC-01, then screenshots both states.

### 3. Preserve deployment boundary for the next lane

The following Docker, README and slide work is separate and starts only after
the browser evidence above is captured. Docker will run read/runtime service
with a persistent data volume; documented ingestion remains host/macOS-bound
where parser isolation requires `sandbox-exec`.
