# Test policy and September 2026 audit

Keep approximately 50 collected cases protecting failures that would matter
in production. The working agreement is [AGENTS.md](../AGENTS.md#tests).
Run `python -m pytest tests/` (or `venv/bin/python -m pytest tests/`).
No coverage target and no obligation to add tests with every change.

## What earns a test

A test should fail for a plausible, consequential defect: lost settings,
incorrect scope, a permission bypass, private data or credential disclosure,
unusable recovery, or a demonstrated integration failure. Assert an observable
outcome across the real local boundary. Use actual Config/files, permission
helpers, registry dispatch, and library objects where their contracts matter.
Replace network I/O with small doubles; do not replace the behavior being tested.

Prefer one scenario per failure mechanism. A state transition may need several
assertions (write, reopen, compare), and distinct implementations may warrant
parameterization (prefix/slash errors). Neither technique should conceal an
inventory of unrelated tests. Extend an existing scenario before adding another.

Remove schema snapshots, metadata catalogs, cosmetic assertions, trivial wrapper
forwarding, duplicate helper/consumer checks, tests of the fake itself, and tests
for hypothetical future features. A lower-priority check can be removed without
claiming that it was incapable of finding a bug. Review and Discord's own API
validation remain appropriate for simple wrappers; this suite does not validate
every API primitive or replace a deployment smoke check.

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
alone did not prevent recurrence. AGENTS.md now makes additions justify a concrete
failure, counts parameterized cases, rejects per-op test bundles, and calls for
pruning review if the suite grows beyond roughly 60 cases. This is a review rule,
not a CI quota that can be gamed by hiding cases in loops.

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
