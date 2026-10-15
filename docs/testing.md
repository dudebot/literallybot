# Test guidance and September 2026 audit

The [qualitative test policy](error-handling.md#test-quality-and-development-checks)
defines what belongs in the committed suite and what to remove after development.
Run `venv/bin/python -m pytest tests/` (or `python -m pytest tests/`).
The counts below are historical, not targets.

## Where the growth came from

The audit read all **463 test functions**, their fixtures, and parameter sets:
**1,408 collected cases**, **8,584 Python lines** in ten test files, including the
uncommitted logging work. Baseline: 1,407 passed, one skipped.

Git evidence:

| Commit | Change | Test functions | Test Python lines |
|---|---|---:|---:|
| `ff0588f` | July keyless-provider tests | 3 | 106 |
| `a1f6e87` | Registry properties moved into pytest | 44 | 585 |
| `ce4e26c` | Frontend invariant tests | 100 | 1,602 |
| `35fa2aa` | Bootstrap tests, after cog test additions | 166 | 3,025 |
| `2a0dc02` | History permission fix | 172 | 3,232 |
| `4dc7caa` | 62-op expansion | 334 | 6,109 |
| `810e05a` | 36 destructive ops and agent gates | 407 | 7,588 |
| `cf7fc91` | Introduced AGENTS.md | 446 | 8,261 |

The largest growth preceded AGENTS.md. Its test guidance said to run pytest and
use real Config; neither it nor the preceding CLAUDE.md imposed exhaustive
coverage. The observable mechanism was repeated per-op testing: schema shape,
permission/group metadata, then mock implementation forwarding. Six shared
registry properties were parameterized over all 125 core ops: **750 cases**.
Those automatically grew whenever another op was added. The later owner-tier
section explicitly froze the inventory, despite the file's introduction warning
against inventories. Commit messages advertised rising passing-test counts.
This supports a diagnosis of template propagation and accumulation; git cannot
establish an author's private motivation.

Other contributors were the cog-development guide's instruction to assert every
serializer/guidance pair, duplicated registry/frontend/cog checks, and tests kept
for mid-run cog replacement after live loading was removed. That guide also
incorrectly claimed dict results without serializers were discarded, encouraging
unnecessary serializer tests. Both instructions have been corrected. Decision
records preserving the obsolete tests are superseded by this audit.

The November 2025 commit `8da7a8e` already removed "mock slop tests". Deletion
alone did not prevent recurrence. The first version of this audit introduced a
numeric suite target and pruning threshold. Those were based on a rough estimate,
not an assessment of independent failure paths, and have been removed. The
linked qualitative policy governs retention, including removal of temporary
development checks before commits. The counts below record the original cleanup;
they are not a target or an endorsement of every retention decision.

## Retained suite

| Area | Before cases | After cases | What remains |
|---|---:|---:|---|
| Config and points | 9 | 11 | Real loading, scope isolation, list mutation, removals, external replacement, malformed JSON, failed writes, shutdown, modes, points persistence |
| Bootstrap | 26 | 5 | Token precedence/persistence, noninteractive startup, verified retry, real Team owner, concurrent owner claim |
| Registry | 1,184 | 12 | History privacy, embed search, cross-guild confinement, permission dispatch, precision, mentions/attachments, webhook secrets, purge targeting |
| Cog operations | 72 | 4 | Role retarget conflict, safe search policy, recovery loader, atomic registration and bound instance |
| Agent and frontends | 69 | 9 | Opt-in scope, live gates and shared budget, actual Discord panel limits, stored choices, MCP token and bearer auth |
| Logging | 28 | 8 | Independent destinations, prefix/slash denials and exceptions, retry/dedup, live panel authorization |
| Keyless LLM | 5 | 2 | Both existing model entrypoints; no skip for deleted chat_stream |
| Panel metadata/copy | 15 | 0 | Removed inventories and cosmetic checks; live authorization is exercised above |
| **Total** | **1,408** | **51** | **1,066 Python lines including fixtures** |

This is a reduction in code and independent scenarios, not deselection, marking
old cases skipped, or packing the same 1,408 checks into 51 functions. New tests
fill storage and authorization gaps exposed by the audit. Timers are manually
driven for determinism; file I/O and Config methods remain real. Tests never need
live bot credentials, a Discord connection, or the deployment's config directory.
