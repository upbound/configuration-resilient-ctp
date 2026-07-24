# Fix Spec — resilient-ctp review remediation

Source: 12-angle parallel review (2026-07-23). IDs map to the review. Each item: **what**, **where**, **fix**, **acceptance**. Grouped into workstreams (WS) partitioned by **disjoint file ownership** so they can be fixed in parallel without conflicts.

Severity: 🔴 critical · 🟠 high · 🟡 medium · ⚪ low.

## Interface contracts (respected across workstreams)
- `heartbeat.read_peer_direct(member, ts_tag, now, ttl, *, timeout=None, credential=None) -> PeerLiveness` — signature stays call-compatible from `main.py`. Per-member cloud scoping (account/subscription/project/creds) is resolved **inside** WS-2 from the `member` dict, so the WS-3 call site does not change except being wrapped for concurrency. Must be **safe to call concurrently** (WS-3 fans out with threads).
- `election.decide(...)` keeps its current signature/return; WS-1 only changes internal logic + adds inputs it already receives.
- `cloud_read.read_resource_tags(provider, name, *, region, project, subscription_id, credential, client, timeout)` interface is stable; WS-2 hardens internals.

---

## WS-1 — Election correctness  (owns: `functions/resilientcontrolplane/election.py`, `gslb.py`)

### C1 🔴 Recovered-former-leader double-leader
- **Where:** `election.py:122-131` — two-phase-handoff + hysteresis gates skipped when `prior_role == "leader"`.
- **Why bug:** `prior_role` is the CP's own persisted `status.role`; a CP that *was* leader, died, and recovers comes back with stale `role=leader` and promotes immediately while the interim leader still holds `["*"]`. Persistent double-leader under no-GSLB/active-active.
- **Fix:** Do not treat persisted `role=="leader"` as "currently leading". Only take the shortcut when there is evidence of *continuous* leadership — e.g. this CP's own self-heartbeat still shows `role=leader` AND is fresh within TTL. A fresh lower-priority peer advertising `role=leader` must always force the two-phase wait regardless of `prior_role`.
- **Acceptance:** new composition test `recovered-former-leader-holds`: XR with `status.role=leader` + a fresh lower-priority peer `role=leader` ⇒ this CP stays `standby` until the peer steps down. Existing tests still green.

### C2 🔴 directApi unreadable hard-blocks a sole GSLB-active failover geo
- **Where:** `election.py:89-98` (`_blocks`) — `"unreadable"` returned before the `gslb_active_failover` (#29) relaxation.
- **Why bug:** In directApi mode a dead region reads as `CloudReadError → unreadable`; the sole GSLB-active standby then blocks forever. `mr` mode reads the same death as *stale* and promotes → the two modes diverge on the exact failover trigger directApi targets.
- **Fix:** When this CP is GSLB-active under `failover` strategy and the ONLY reason a higher peer blocks is *unreadable* (not `readable+fresh+role=leader`), allow the #29 relaxation to apply — i.e. an unreadable higher peer under sole-GSLB-active-failover is treated like a definitively-down peer, NOT an automatic block. Preserve the partition tie-breaker: a higher peer that is `readable+fresh+role=leader` still blocks.
- **Acceptance:** new test `directapi-unreadable-higher-gslb-active-promotes` (sole GSLB-active, failover, higher peer unreadable ⇒ promote) AND `directapi-unreadable-higher-not-active-holds` (not GSLB-active ⇒ still hold). `mr` and `directApi` give the SAME leadership outcome for a region-death.

### C5 🟡 Unguarded `healthyRecords: null`
- **Where:** `gslb.py:65` — `status.get("healthyRecords", {}).get(hostname, [])` missing `or {}`.
- **Fix:** `(status.get("healthyRecords") or {}).get(hostname, [])`, matching adjacent lines 34/51/66.
- **Acceptance:** unit/composition case: Gslb with `healthyRecords: null` ⇒ no exception, `active=False`.

---

## WS-2 — Cloud-read robustness  (owns: `functions/resilientcontrolplane/cloud_read.py`, `heartbeat.py`)

### H1 🟠 GCP read ignores timeout
- **Where:** `cloud_read.py:133,194,206` — `_gcp_read_labels` takes no `timeout`; `read_resource_tags` doesn't pass one for GCP.
- **Fix:** Thread `timeout` into `_gcp_read_labels` and apply it: `client.get_bucket(name, timeout=timeout)`.
- **Acceptance:** GCP read path bounded by `readTimeoutSeconds`; unit test with a fake client asserting timeout forwarded.

### L7/Perf4 🟠 Azure/GCP retries not disabled; Azure per-call timeout unverified
- **Where:** `cloud_read.py:176-185` (azure), `194-206` (gcp) vs AWS `146-147` (`max_attempts=1`).
- **Fix:** Configure Azure `ResourceManagementClient` and GCP `storage.Client` for **no/one retry** (matching AWS `max_attempts=1`) so effective time ≈ `timeout`, not `timeout×retries`. Verify Azure `.get(..., timeout=)` is honored by the pinned SDK; if not, wrap in a hard deadline.
- **Acceptance:** all three providers bound a slow read to ≈`readTimeoutSeconds`; documented.

### C4 🟡 directApi ignores per-member account/subscription/project/creds
- **Where:** `heartbeat.py:206-245` (`read_peer_direct`) passes only `region`; `main.py:103-107` call site.
- **Fix:** Resolve per-member cloud scoping inside `read_peer_direct` from the `member` dict (region/project/subscription and, where feasible, credential) so cross-account/cross-cloud peers are read against the RIGHT account — not only the function's ambient identity. Where a per-member credential can't be resolved, fail-safe (unreadable) as today but do not silently read the wrong account.
- **Acceptance:** a member with a distinct project/subscription is read against it (fake-client test asserting the scope passed); no regression for same-account.

### M-3 🟡 DRY: shared PeerLiveness builder + client-cache helper; thread-safety
- **Where:** `heartbeat.py:231-245` vs `258-272` (dup); `cloud_read.py:137-212` (three near-identical skeletons); `_CLIENT_CACHE` check-then-act.
- **Fix:** Extract `_liveness_from_tags(member, tags_or_none, ts_tag, now, ttl)` used by both `read_peer` and `read_peer_direct`. Extract a `_cached_client(cache_key, factory)` helper. Make cache access safe for concurrent calls (WS-3 fans out with threads) — e.g. a lock around get-or-create, or accept benign double-build but never corrupt.
- **Acceptance:** one liveness builder; `up test` green; concurrent calls don't corrupt the cache.

### D2 ⚪ Stale docstring
- **Where:** `heartbeat.py:14-18` says GCP unimplemented.
- **Fix:** "AWS, Azure and GCP implemented; Alibaba raises."

---

## WS-3 — Orchestration & packaging  (owns: `functions/resilientcontrolplane/main.py`, `k8gb_install.py`)

### P1 🟠 Vendor sys.path shim hardening
- **Where:** `main.py:28-31` — hardcoded `python3.11`.
- **Fix:** Derive at runtime: `f"python{sys.version_info.major}.{sys.version_info.minor}"` and/or glob `vendor/lib/python3.*/site-packages`. If `heartbeat.read == directApi` is configured but no vendor dir resolves, surface a **loud** signal (a warning in the composition result / status) instead of silently degrading to "every peer unreadable".
- **Acceptance:** shim resolves regardless of 3.11/3.12; a missing vendor dir under directApi produces a visible warning.

### Perf1/Perf8 🟠 Parallel peer reads + aggregate budget; skip unused conversion
- **Where:** `main.py:103-107` (serial list-comp), `main.py:81-84` (`struct_to_dict` over all observed).
- **Fix:** Fan out `read_peer_direct` across peers with a `ThreadPoolExecutor` under a single overall budget/early-exit (don't let `timeout×N` blow the gRPC deadline). In directApi mode, skip converting peer Observe MRs that aren't used for the read (only convert what's needed).
- **Acceptance:** N peers read concurrently (latency ≈ slowest, not sum); reconcile stays within deadline at N=10; behavior unchanged in `mr` mode.

### H2 🟠 `should_install` stickiness (relates to the observed empty-values/uninstall failure)
- **Where:** `main.py:150-151`, `k8gb_install.should_install`.
- **Fix:** Make "already installed" a **sticky** signal that survives a transient missing observation of the operator Release (e.g. also treat a prior status marker or any k8gb-owned observed resource as installed), so `should_install` can't flip to False and drop the Releases (→ uninstall/CRD removal/deadlock, or a stale empty-values Release). ALSO investigate the live "empty Release values on AWS" symptom on the fresh rebuild and add a guard/regression note.
- **Acceptance:** momentarily absent operator Release does not flip `should_install` off; k8gb keeps rendering; fresh-deploy AWS Release carries full `values`.

### M-7 ⚪ Centralize constants; D1 docstring
- **Where:** timeout default duplicated (`main.py:70` `10` vs `cloud_read.py:45` `10.0`); `10**9` sentinel in `heartbeat.py:234,261` + `status.py:14`; chart versions/URLs in `k8gb_install.py`.
- **Fix:** Single source for the read-timeout default; promote `UNKNOWN_AGE_SENTINEL` to `prelude` (WS-2/WS-4 consume). Name chart versions/URLs as module constants. Fix `k8gb_install.py:7-8` docstring: default is **`auto`**, not `never`.
- **Acceptance:** no duplicated default; `status.py` no longer hard-codes `10**9`; docstring correct.

> Note: `_lb_annotations` disproven-hypothesis change already reverted; do NOT re-touch it here.

---

## WS-4 — XRD, docs, examples, security-examples  (owns: `apis/resilientcontrolplane/definition.yaml`, `docs/`, `README.md`, `examples/`)

### C3 🟡 Priority uniqueness
- **Where:** `definition.yaml` members — no uniqueness on `priority`.
- **Fix:** Add `x-kubernetes-validations` requiring distinct `priority` across `members` (and identity in members). Equal priorities break the higher/lower partition → double-promote.
- **Acceptance:** applying a claim with duplicate priorities is rejected by the API server.

### D1/D3/D4/D5/D6/D8 🟠 Documentation truthfulness
- **D1:** `k8gb.install` default is `auto` — fix `docs/SPEC.md:89,211`, `examples/*-simple.yaml` (README already correct).
- **D3:** rewrite `docs/SPEC.md §6` (the AND rule) to match `election.py` — the readable/fresh/role predicate + the #29 `gslb_active_failover` relaxation; remove the non-existent "per-peer GSLB attribution".
- **D4:** `definition.yaml` — mark `policyControl` (patch mode) and `failback.automatic` as **not yet implemented** in their descriptions (code never reads them), or drop the fields; drop dead `status.role` enum value `unknown`.
- **D5:** `SPEC.md:95-96` — k8gb IP discovery is via CoreDNS `serviceType: LoadBalancer`, not an init-ingress.
- **D6:** `docs/ROADMAP.md:64` — AWS read IAM is `ssm:ListTagsForResource` only (not `GetParameters`).
- **D8:** GCP heartbeat is a **GCS Bucket** (align SPEC/ROADMAP wording).
- **Acceptance:** each cited line matches the code; a doc-vs-code spot check passes.

### S1/S4/S7 🟡 Security hardening (examples/docs scope)
- **S1:** `examples/rbac-k8gb.yaml` (the helm cluster-admin binding) — document the risk prominently and provide a **scoped** ClusterRole alternative (only the APIs the k8gb chart installs) as the recommended default; keep cluster-admin only as an explicit opt-in.
- **S4:** `examples/resilientcontrolplane-gslb.yaml` / extdns — prefer IRSA/Workload-Identity; if static Route53 keys are used, mount as a **file**, not env vars. Document.
- **S7:** (coordinate w/ WS-3) user-facing error surfaced by `main.py` fatal path should be **generic**; raw SDK/AAD exception text must not land in XR status. WS-4 documents the contract; WS-3 implements the generic message.
- **Acceptance:** example ships a least-privilege RBAC default; docs warn on creds-in-`default` and env-var keys.

### P3/P5 ⚪ Vendor/deploy docs
- **Fix:** document the vendored-deps requirement + `hack/vendor-deps.sh` where directApi prerequisites are listed (SPEC §11 / README); note the required DeploymentRuntimeConfig cred mounts for directApi (ship the example — `examples/directapi-heartbeat.yaml` exists; cross-link it).
- **Acceptance:** a reader can set up directApi from the docs alone.

---

## WS-5 — Tests + validate  (owns: `tests/`) — runs AFTER WS-1..4
- **T1:** unit tests for `cloud_read.py` using injected fake clients — the `CloudReadError` fail-safe mapping (auth/network/not-found → raise), `read_resource_tag` None-when-absent, per-provider tag/label extraction, timeout forwarded (incl. GCP).
- **T2:** composition test: unreadable higher-priority peer (missing observed heartbeat) ⇒ CP holds `standby`.
- **T3:** composition test for **C1** (recovered-former-leader) and **C2** (directApi unreadable vs #29).
- **T4:** assert `status` contract shape (`managementPolicy`, `peers[].definitivelyDown`, `ageSeconds==-1` sentinel, `gslb.isActiveForGeo`).
- **T5:** `should_install` `never`/external-present branches.
- **Validate:** `py_compile` all modules; `yamllint` YAML; `up test run tests/*` (all existing 27 + new green). Run `up test` **serially** (never two concurrently — Docker contention).
- **Acceptance:** full suite green; new tests cover the critical fixes.

## Out of scope here (separate follow-up)
- S2 (creds-in-`default`) and S3 (claim-chosen ProviderConfig allow-list) are cross-package / design changes — tracked in the ctp-package issues (gcp #9, azure #7) and a new resilient-ctp design issue.
- P2 multi-arch vendor wheels — needs CI/build changes; note in ROADMAP backlog.
