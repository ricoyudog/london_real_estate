# WIKI KNOWLEDGE BASE

## OVERVIEW
`wiki/` is a tracked Obsidian vault, a first class version controlled knowledge base, not disposable notes.
It records architecture, decisions, research, and current project context.

## STRUCTURE
| Path | Purpose | Maintenance rule |
|---|---|---|
| `hot.md` | Current status, pinned entry point | Update every session; stay within cap |
| `index.md` | Vault navigation | Keep links current; stay within cap |
| `schema.md` | Page metadata and update contract | Follow it; edit only for contract changes |
| `log.md` | Change event history | Append only |
| `architecture/` | System design and implicit contracts | Update `_index.md` with page changes |
| `decisions/` | Accepted, proposed, superseded choices | Update `_index.md` with page changes |
| `research/` | Source and product research | Update `_index.md` with page changes |
| `sessions/`, `questions/`, `patterns/`, `meta/` | Supporting knowledge | Update the matching `_index.md` |
| Root briefs | Project reference material | Preserve as source documents |

## PAGE TYPES
| Type | Required frontmatter | Optional frontmatter |
|---|---|---|
| `pattern` | `type: wiki`, `created`, `source_change` | `tags` |
| `decision` | `type: wiki`, `updated`, `status` | `source`, `tags` |
| `session` | `type: wiki`, `created`, `source_change`, `status` | `tags` |
| `research` | `type: wiki`, `updated`, `source` | `tags` |
| `meta` | `type: wiki`, `generated` | `tags` |

## UPDATE CONTRACT
| Trigger | Required sync action |
|---|---|
| Add or delete a subdirectory `.md` | Update its `_index.md` |
| `corgispec-archive` | Append `log.md`; check whether a decision page is needed |
| `corgispec-memory-extract` | Update the matching `_index.md` |
| Finish a session | Update `memory/session-bridge.md` and `hot.md` |
| Run `/corgi-lint` | Check orphans, frontmatter, indexes, log coverage |

## SIZE CAPS
| File | Target | Hard cap | Overflow action |
|---|---:|---:|---|
| `wiki/hot.md` | 500 words | 600 words | Trim oldest entries |
| `wiki/index.md` | 40 lines | 80 lines | Archive completed entries |
| `memory/pitfalls.md` | 10 active | 20 active | Rotate oldest 10 |
| `memory/session-bridge.md` | 30 lines | 50 lines | Archive old Done items |

## LOG FORMAT
`YYYY-MM-DD | action change-name | +added-file -removed-file`

## WHERE TO LOOK
| Task | Location |
|---|---|
| Add an architecture decision | `decisions/` and `decisions/_index.md` |
| Add a session record | `sessions/` and `sessions/_index.md` |
| Add research | `research/` and the matching `_index.md` |
| Update current status | `hot.md` |

## ANTI-PATTERNS (THIS DIRECTORY)
- Don't skip `_index.md` when adding or deleting a subdirectory `.md`.
- Don't exceed size caps without trimming or archiving.
- Don't edit root files, `hot.md`, `index.md`, `schema.md`, or `log.md`, outside their contract.
- Don't remove frontmatter, break `[[wiki links]]`, or add untyped pages.
