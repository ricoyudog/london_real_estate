# DoneClaim — F2 fixes
- task: "Fix F2 findings 1-4 (citation lineage, approval continuation, smoke strictness, EPERM)"
- changed_files: ["agent-runtime/src/app.ts", "agent-runtime/src/approval.ts", "agent-runtime/src/facade-launcher.ts", "agent-runtime/src/finalizer.ts", "agent-runtime/src/turn-runner.ts", "agent-runtime/test/approval.test.ts", "agent-runtime/test/facade-launcher.test.ts", "agent-runtime/test/finalizer.test.ts", "agent-runtime/test/real-model-e2e.test.ts", "agent-runtime/test/turn-runner.test.ts", "agent-runtime/.omo/evidence/task-f2-fix/DONECLAIM.md", "agent-runtime/.omo/evidence/task-f2-fix/failing-first.txt", "agent-runtime/.omo/evidence/task-f2-fix/full-suite.txt", "agent-runtime/.omo/evidence/task-f2-fix/git-commit-stat.txt", "agent-runtime/.omo/evidence/task-f2-fix/key-grep.txt", "agent-runtime/.omo/evidence/task-f2-fix/per-finding-proof.md", "agent-runtime/.omo/evidence/task-f2-fix/real-smoke.txt", "agent-runtime/.omo/evidence/task-f2-fix/tsc.txt"]
- tests: ["failing-first red→green for F2-1, F2-2, F2-4", "focused 64/64, integration 3/3, fixtures 8/8, default 152 pass + 2 skips", "real smoke: RED (documented honest failure: model stopped after citation metadata without finalize_market_brief)"]
- manual_qa: [".omo/evidence/task-f2-fix/*"]
- adversarial: {"misleading_success_output": "real smoke not faked; key grep empty", "stale_state": "fresh suite runs"}
- cleanup: ["env restored; no processes"]
- risks: ["Opt-in GLM-5.2 smoke remains red until the real model reliably invokes finalize_market_brief; the strict gate intentionally exposes this."]
