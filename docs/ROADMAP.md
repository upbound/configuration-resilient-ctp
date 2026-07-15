# configuration-resilient-ctp — Implementation Roadmap

Companion to [`SPEC.md`](./SPEC.md). Status: **Draft v0.1** (2026-07-14).

## Guiding constraints
- Functions in **Python**; compositions in **Pipeline mode** only.
- **Offline composition tests** (`up test run tests/*`) are author-runnable and gate every phase.
- **Cloud e2e is user-run** — the author has no AWS/Azure/GCP credentials.
- Nothing is called production-ready until unit + e2e tested **and** human sign-off.
- `yamllint` all YAML; keep the project structure tidy.

## Sequencing insight (why the k8gb gap is not blocking)
The entire leadership **decision engine** (heartbeat freshness, the AND rule, failover/failback FSM,
two-phase handoff, convention publish) is a pure function of observed state. It can be built and
**composition-tested offline** by feeding synthetic `Gslb.status` and synthetic peer-heartbeat
observations — **no cloud, no real k8gb**. And since no ctp package installs k8gb yet,
`resilient-ctp` **optionally installs it itself** via `spec.k8gb.install` (`auto`/`always`/`never`,
SPEC §4.1) — the install resources are also composition-testable offline. Only end-to-end validation
needs a live cluster.

---

## Phase 0 — Foundations & decision engine (offline)
_No cloud. Fully author-testable._

- [ ] Scaffold the project (`up project init`, Python function `functions/resilience`).
- [ ] Define `ResilientControlPlane` XRD (Crossplane v2, Namespaced) per SPEC §8.
- [ ] Composition (Pipeline): `function-extra-resources` (fetch members/peers/self) → `resilience`
      (decision + heartbeat desired-state + status) → `function-auto-ready`.
- [ ] Python decision engine modules:
  - [ ] `prelude.py` — struct→dict boundary, epoch-seconds helpers, timestamp stamping (borrow the
        `stamp()` pattern from `configuration-aws-ctp`).
  - [ ] `heartbeat.py` — derive own + peer heartbeat coordinates from `spec.members`; freshness eval;
        write-throttle logic; emit own heartbeat MR (`["*"]`) + peer Observe MRs (`["Observe"]`).
  - [ ] `gslb.py` — read local k8gb `Gslb.status`; per-geo health; strategy-aware activeness.
  - [ ] `election.py` — the AND rule (SPEC §6), "definitively down" logic, priority ordering.
  - [ ] `failover.py` — failback hysteresis + two-phase handoff FSM (SPEC §7).
  - [ ] `status.py` — write `status.role`, `status.managementPolicy` (convention contract), `peers`,
        `gslb`, `reason`, conditions.
  - [ ] `k8gb_install.py` — **optional** k8gb install (SPEC §4.1): port the source
        `functions/k8gb-operator` KCL logic to Python (nginx-ingress `Release`, k8gb operator
        `Release`, init-ingress); gate by `spec.k8gb.install` (`never`/`auto`/`always`); `auto`
        detects the k8gb `Gslb` CRD / operator via an Observe probe. Adds **`provider-helm`** dep.
- [ ] Composition tests (`tests/`) with synthetic inputs:
  - [ ] leader-alone → `["*"]`
  - [ ] standby with fresh higher-priority primary → `["Observe"]`
  - [ ] primary heartbeat stale + GSLB unhealthy → standby promotes
  - [ ] failback hysteresis (promotes only after N healthy periods) + two-phase handoff (no both-`*`)
  - [ ] partition / unreadable higher-priority peer + GSLB-healthy → **stay Observe** (split-brain guard)
  - [ ] ambiguity → Observe (never fail open)
  - [ ] `spec.k8gb.install`: `never` renders no k8gb resources; `always` renders them; `auto` renders
        only when the Observe probe reports k8gb absent
- **Exit:** `up project build` + `up test run tests/*` green; decision engine validated offline.

## Phase 1 — Test 1: 2×AWS, cross-region, convention path
_Deliverable: single-cloud cross-region failover **and** failback of one shared S3 bucket._

- [ ] **Rewrite `configuration-aws-s3`** → Crossplane v2 package: claim a **bucket XR in a region**;
      deterministic `crossplane.io/external-name`; **resilience-aware** — fetch
      `ResilientControlPlane.status.managementPolicy` via `function-extra-resources` and apply to the
      bucket MR (default `["Observe"]` when absent). Offline composition tests.
- [ ] AWS heartbeat = **SSM Parameter** (`recon-heartbeat-<id>`, tag `last-reconciliation-timestamp-utc`
      = epoch seconds). ⚠️ **Verify (R1)** the tag surfaces in `atProvider` on Observe — user-run.
- [ ] AWS read cred / IAM: `ssm:GetParameters*` + `tag:GetResources` (same-account, multi-region).
- [ ] Example manifests: two `ResilientControlPlane` members (us-east-1 priority 1, us-west-2
      priority 2), two identical S3 claims (same external-name), `providerconfig` per CP.
- [ ] k8gb on both CPs via **`spec.k8gb.install: auto`** (installs since aws-ctp doesn't provide it).
- [ ] **e2e (user-run):**
  - [ ] steady: CP-A `["*"]` owns bucket, CP-B `["Observe"]` reads same bucket
  - [ ] kill CP-A (or make its geo unhealthy) → CP-B promotes to `["*"]`, keeps the same bucket
  - [ ] recover CP-A → automatic failback to CP-A; verify **≤1 owner** throughout
- **Exit:** failover + failback demonstrated on AWS; human sign-off.

## Phase 2 — Test 2: AWS + Azure (US + EU)
_Deliverable: cross-cloud pair._

- [ ] Azure heartbeat = **Resource Group** (tag). Verify surfacing (R1).
- [ ] Dual-provider install: `provider-azure` + Azure read cred on the AWS CP; `provider-aws` +
      AWS read cred on the Azure CP. **Least-privilege**, scoped to heartbeat resources (SPEC §11).
- [ ] Shared managed resource stays the **single AWS S3 bucket**, observed by the Azure CP.
- [ ] k8gb on the Azure CP via **`spec.k8gb.install: auto`** (AKS).
- [ ] Member list spans two providers/regions/geoTags (`us`, `eu`).
- [ ] **e2e (user-run):** failover/failback across AWS↔Azure.
- **Exit:** cross-cloud failover demonstrated; human sign-off.

## Phase 3 — Test 3: AWS + Azure + GCP, active-active geo-LB (US/EU/Asia)
_Deliverable: tri-cloud, tri-continent, load-balanced with single-leader shared-resource failover._

- [ ] GCP heartbeat = **Pub/Sub Topic** (label; epoch seconds avoids GCP label `:` restriction).
      Verify surfacing (R1).
- [ ] **k8gb active-active** — NEW work vs source package: `roundRobin`/`geoip` strategy, three
      geo-tags `us,eu,asia`, `externalClustersGeoTags` = all three. (Source hard-codes `failover`
      and ships only a 2-cluster `eu,us` example.)
- [ ] Tri-provider creds matrix; k8gb on the GCP CP via **`spec.k8gb.install: auto`** (GKE).
- [ ] 3-member set (priority 1/2/3); traffic active-active; shared AWS S3 bucket ownership fails over
      across continents.
- [ ] **e2e (user-run):** each continent serves locally; take a continent down → its ownership fails
      over, traffic redistributes; recover → failback.
- **Exit:** tri-cloud tri-continent demonstrated; human sign-off.

## Backlog / later
- [ ] **Patch-fallback path** for unmodified/arbitrary packages (SPEC §9.2) — pick execution surface
      (Crossplane v2 Operation vs small controller); field-manager coexistence.
- [ ] **k8gb install in ctp packages** — once available, default `spec.k8gb.install: never` and
      retire the built-in install path from `resilient-ctp`.
- [ ] **GitOps self-forming membership** — derive members from replicated `ControlPlane` XRs via
      `function-extra-resources` instead of an explicit `spec.members`.
- [ ] **Alibaba** support (OSS-bucket heartbeat) once a fourth region is desired.

## External dependencies to track
| Dependency | Owner | Blocks |
|---|---|---|
| k8gb install in aws/azure/gcp ctp packages | ctp package owners | nothing (resilient-ctp installs k8gb itself via `spec.k8gb.install`); later lets us retire the built-in path |
| `resilient-ctp` install added to ctp packages | ctp package owners | production deployment ergonomics |
| Least-privilege cross-cloud read creds | user / platform | Phases 2–3 |
| `provider-helm` dependency | this package | optional k8gb install |
