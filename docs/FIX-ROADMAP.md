# Fix Roadmap — resilient-ctp review remediation

Chronology + parallelization for the fixes in [FIX-SPEC.md](./FIX-SPEC.md). Work is partitioned by **disjoint file ownership** so workstreams run concurrently with zero merge conflicts. Validation (`up test`) is serialized (Docker contention).

## Dependency graph
```
        ┌──────────── Phase 1 (parallel) ────────────┐
WS-1 election.py, gslb.py ────┐
WS-2 cloud_read.py, heartbeat.py ─┤
WS-3 main.py, k8gb_install.py ────┼──► Phase 2 validate ──► Phase 3 tests (WS-5) ──► Phase 4 commit/PR
WS-4 definition.yaml, docs/, examples/ ─┘
```
- **WS-1..WS-4 are fully parallel** — no shared files. Each agent edits only its files, runs `py_compile` on Python it touched, and does NOT run git or `up test`.
- **Only real coupling:** WS-3's parallel peer-read fan-out calls WS-2's `read_peer_direct`. Decoupled by the interface contract in the spec (signature unchanged; per-member scoping + thread-safety handled inside WS-2). No file overlap.
- **WS-5 (tests) depends on WS-1..4** landing — Phase 3.

## Phase 1 — parallel fixes (4 sub-agents, disjoint files)
| WS | Agent owns | Fixes | Blocks whom |
|----|-----------|-------|-------------|
| **WS-1** | `election.py`, `gslb.py` | C1 🔴, C2 🔴, C5 🟡 | WS-5 tests T3 |
| **WS-2** | `cloud_read.py`, `heartbeat.py` | H1 🟠, L7/Perf4 🟠, C4 🟡, M-3 🟡, D2 ⚪ | WS-3 read-path, WS-5 T1 |
| **WS-3** | `main.py`, `k8gb_install.py` | P1 🟠, Perf1/Perf8 🟠, H2 🟠, M-7 ⚪, D1-docstring ⚪ | — |
| **WS-4** | `definition.yaml`, `docs/`, `README.md`, `examples/` | C3 🟡, D1/D3/D4/D5/D6/D8 🟠/⚪, S1/S4/S7-docs 🟡, P3/P5 ⚪ | — |

**Rationale for ordering within Phase 1:** none required — all four are independent. Critical-severity items (C1, C2 in WS-1) are highest-value; they land in the same pass as the rest since they don't block anything except their tests.

## Phase 2 — integrate + validate (serial, orchestrator)
1. `py_compile` every module; `yamllint` changed YAML.
2. `up project build` (bundles the gitignored `vendor/` via `hack/vendor-deps.sh` if needed).
3. `up test run tests/*` — the existing 27 composition tests MUST stay green (regression gate). Run **one at a time**.
4. Triage any regression; hand back to the owning WS.

## Phase 3 — tests (WS-5, 1 agent, after Phase 2 green)
- Add T1–T5 (see spec). New composition cases for C1/C2 are the highest value — they guard the two critical bugs and are ~one-field deltas from existing tests.
- `up test run tests/*` green including new cases.

## Phase 4 — commit + PR
- Commit per-workstream (clear messages), push `fix/review-hardening`, open a PR distinct from the GSLB feature PR #12.
- Open follow-up GitHub issues for out-of-scope items: S2 (creds-in-`default`), S3 (ProviderConfig allow-list), P2 (multi-arch vendor), and the live "empty Release values on AWS" investigation (re-check on the fresh rebuild).

## Live re-validation (after code fixes)
Rebuild fresh (fixed package + **narrow MRAP** per the mgmt-perf earmark) and re-run Test 3 to confirm: directApi reliable reads, single-leader invariant across a true tri-cloud failover (incl. AWS leading), and the C1/C2 fixes under real region-death.

## Backlog (not this pass)
- Provider **registry** refactor (M-1) — co-locate per-provider read/write/field; makes a 4th cloud one entry. Larger; do after the correctness fixes settle.
- Multi-arch (amd64+arm64) vendored wheels, or drive Pylon #900 to a real fix so the shim can be deleted.
- Split `_compose` config-parsing into a `parse_config` dataclass (M-6).
