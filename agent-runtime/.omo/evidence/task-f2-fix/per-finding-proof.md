# F2 per-finding proof

- F2-1: query results now add one ledger entry per record; citation metadata now adds one entry per citation ref; `hostLedger` merges each ref's query numeric fields with that ref's citation metadata. Regression tests cover record B value `6.50`, qualitative ref separation, and production ledger shape.
- F2-2: approval continuation now awaits the original paused outcome, invokes `resumeTurn` with the same `TurnContext`, captures the pre-resume event count, projects resumed tool/artifact/terminal events, and releases only afterward. Denial still bypasses Pi.
- F2-3: real smoke now requires terminal `completed`, a `market_brief.v1` artifact, and query/citation/finalize tool calls. The live run remained honestly RED because GLM-5.2 stopped after citation lookup without finalizing.
- F2-4: only ESRCH is process-missing. EPERM propagates through cleanup and is mapped by `invoke` to typed `PROTOCOL_ERROR`; the regression uses a launcher-local kill dependency to avoid global test contamination.
