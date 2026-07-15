# configuration-resilient-ctp — Technical Specification

Status: **Draft v0.1** (design agreed 2026-07-14)
Function language: **Python** (generalized from the KCL-based `configuration-k8gb-bluegreen`)

---

## 1. Purpose

`configuration-resilient-ctp` is a Crossplane v2 Configuration package installed on **every control
plane in a resilience set**. It makes exactly **one** control plane the *main* (holding
`managementPolicies: ["*"]` over the shared managed resources) while the others sit at
`["Observe"]`, and it performs **autonomous failover and failback** when control planes suffer
outages — with no human in the loop.

It does **not** provision the control plane or own the workload resources. It only:

1. **writes** this control plane's liveness heartbeat,
2. **reads** peers' heartbeats and the local k8gb GSLB health,
3. **decides + publishes** the management policy this control plane should apply, and toggles it, and
4. **optionally installs k8gb** (opt-in, see §4.1) when the ctp packages don't yet provide it.

## 2. Background — the pattern being generalized

`configuration-k8gb-bluegreen` (KCL) runs the same `GlobalApp` XR on every cluster. Standbys sit at
`["Observe"]`, the active cluster at `["*"]`, and **the same cloud resource is linked across clusters
by a deterministic `crossplane.io/external-name`**. `functions/gslb-monitoring/main.k` reads the
local k8gb `Gslb.status` (`serviceHealth`, `healthyRecords` vs `exposedIps`) to compute
`status.gslb.recommendedPolicy` = `"*"` when healthy+active else `"Observe"`; when
`autoApplyRecommendedPolicy: true`, `functions/infrastructure/main.k` stamps that onto the managed
resources. There is **no shared claim and no replication** — every cluster holds its own copy of
the XR and only the policy differs.

`configuration-resilient-ctp` keeps that "standing copy on every CP, flip the policy" model and
generalizes it in three ways:

- the **arbitrary** workload package is decoupled from the resilience logic (it no longer has to be
  the same composition that computes the policy);
- the leadership signal is **two-factor** (k8gb GSLB **plus** a cross-CP heartbeat ledger), for
  split-brain safety and cross-provider reach; and
- it works across **AWS / Azure / GCP** (and Alibaba later).

## 3. Scope & non-goals

**In scope**
- Per-CP heartbeat write + peer/GSLB read.
- Leadership election (single main) via the AND rule (§6) with failover + failback.
- Publishing the leadership decision for consumption (convention path) and, later, patching foreign
  MRs (fallback path).

**Non-goals**
- Provisioning control planes (owned by `configuration-{aws,azure,gcp}-ctp`).
- Installing itself (the ctp packages install `resilient-ctp` — in progress as of 2026-07-14).

**Note on k8gb:** installing k8gb is *optional* here (§4.1). It is not the primary responsibility of
this package — the ctp packages are expected to own it eventually — but `resilient-ctp` can install
it on demand so a control plane that lacks k8gb is still usable, and skip it when k8gb is present.
- Replicating workload claims across control planes (explicitly out — model is standing copies).
- Cross-cloud data replication of the workload's backing store.

## 4. Deployment model

```
configuration-{aws,azure,gcp}-ctp   (per control plane)
   └─ provisions EKS/AKS/GKE + UXP
   └─ installs k8gb            (parameters.k8gb.enabled: "yes")
   └─ installs resilient-ctp   (being added to the ctp packages)

resilient-ctp assumes: it runs on a ctp-built control plane with k8gb present.
```

The workload package (e.g. the rewritten `configuration-aws-s3`, §10) and a `ResilientControlPlane`
XR (§8) are applied to **each** participating control plane.

### 4.1 Optional k8gb install (`spec.k8gb.install`)

Because no ctp package installs k8gb yet (SPEC §12), `resilient-ctp` can install it — **opt-in and
specifiable**:

| Value | Behavior |
|---|---|
| `never` (default) | Assume k8gb is already present; only consume its `Gslb` status. |
| `auto` | Install k8gb **only if** the k8gb operator / `Gslb` CRD is **not detected** on the cluster; skip otherwise. |
| `always` | Always render the k8gb install resources. |

When installing, `resilient-ctp` ports the source package's `functions/k8gb-operator` logic to Python:
an nginx-ingress `Release`, the k8gb operator `Release` (with `clusterGeoTag`,
`extGslbClustersGeoTags`, `dnsZones`, `edgeDNSServers`, external-dns), and an init-ingress for IP
discovery. This requires **`provider-helm`** as a package dependency. All k8gb parameters live under
`spec.k8gb` (§8) and mirror the source `K8gbCluster` schema. Detection for `auto` uses an
`Observe`-only probe of the k8gb `Gslb` CRD / operator Deployment.

## 5. The two signals

| Signal | Source | Answers | Strength / weakness |
|---|---|---|---|
| **k8gb GSLB** | local `Gslb.status` | "Is my geo healthy / serving?" (and, in `failover` strategy, "am I THE active one?") | Globally-distributed DNS arbitration → partition-resistant. Cannot, by itself, enforce single-writer in `roundRobin`/`geoip` (all healthy clusters are "active"). |
| **Heartbeat ledger** | one lightweight cloud resource **per CP**, tag/label `last-reconciliation-timestamp-utc` = **Unix epoch seconds** | "Which peers are alive, and what is their priority?" | Sole-writer per resource → no contention, fully declarative. Individually weak in a partition (an unreachable peer is *unknown*, not *dead*) — which is why it is ANDed with GSLB. |

### 5.1 Heartbeat resource (per cloud, cheapest taggable/labelable)

| Cloud | Resource | Cost at rest | Notes |
|---|---|---|---|
| AWS | SSM Parameter (Standard) | free | tag `last-reconciliation-timestamp-utc` |
| Azure | Resource Group | free | tag |
| GCP | Pub/Sub Topic (or empty GCS bucket) | ~free | **label** (GCP label values forbid `:` → epoch seconds is portable) |
| Alibaba (later) | OSS bucket (empty) | ~free | tag |

- **Value encoding = Unix epoch UTC seconds** (pure digits) — valid as a tag *and* a GCP label, and
  trivially comparable: `now - value > freshnessTTLSeconds ⇒ stale`.
- **Naming = `recon-heartbeat-<id>`**, deterministic from the CP id.
- **Write throttling:** the composition observes its **own** heartbeat resource and only bumps the
  timestamp when the observed value is older than `writeThrottleSeconds`, to avoid a cloud write on
  every reconcile poll.
- **"Successful reconciliation"** = the `ResilientControlPlane` reconcile completed without error and
  the CP could reach its own provider to write the heartbeat.
- ⚠️ **To verify before finalizing each provider:** that the tag/label actually surfaces in
  `status.atProvider` on an `Observe`-only MR. If a chosen type does not expose it, fall back to the
  next candidate. Do not assume.

### 5.2 Peer coordinates

Each peer's heartbeat resource coordinates (`id`, `region`, `provider`, resource name) come from the
**member list** (§8, `spec.members`). If the `ControlPlane` XRs are present locally (management-plane
deployment or GitOps-replicated), the same fields may instead be auto-derived from `allControlPlanes`
via `function-extra-resources` — the member list is simply the explicit carrier of that data and is
the **default** so we never depend on `ControlPlane` XR visibility on a workload CP.

## 6. Leadership decision (the AND rule)

A control plane **holds/takes `["*"]` on the shared resource iff all hold**:

1. **GSLB-healthy** for its geo (local `Gslb.status` shows this cluster serving/healthy), **and**
2. **self-heartbeat fresh** (it can write its own heartbeat), **and**
3. **no higher-priority peer is alive**, where a higher-priority peer `A` is considered **alive**
   unless it is *definitively down*.

`A` is **definitively down** ⇔ (`A`'s heartbeat is readable **and** stale) **OR** (GSLB reports
`A`'s geo unhealthy). An **unreadable** higher-priority heartbeat (missing perms / partition) is
**not** "down" on its own — promotion then requires GSLB to independently confirm `A`'s geo
unhealthy. Otherwise: **stay `["Observe"]`.**

**Invariants**
- **Never fail open.** Any ambiguity ⇒ `["Observe"]`.
- **At most one `["*"]` owner** at any time (enforced by priority + the handoff protocol, §7).
- Priority is total and configured (`spec.members[].priority`, lower = higher priority = preferred
  primary).

### 6.1 Strategy-dependence of the GSLB term
- **`failover` strategy (Tests 1–2):** GSLB yields exclusive activeness (one `primaryGeoTag`), so
  term (1) already implies most of the exclusivity; priority+heartbeat break residual ties.
- **`roundRobin`/`geoip` strategy (Test 3, active-active):** GSLB marks *all* healthy geos active,
  so term (1) degrades to a pure **health** check and **exclusivity is carried entirely by
  priority + heartbeat**. The rule above is written to work identically in both modes.

## 7. Failover & failback

- **Failover** (owner dies): its heartbeat goes stale and/or GSLB marks its geo unhealthy → the
  next-priority CP satisfies the AND rule and promotes to `["*"]`.
- **Failback** (higher-priority owner recovers): **automatic, with hysteresis** — the recovered peer
  must be GSLB-healthy **and** heartbeat-fresh for `failback.hysteresisPeriods` consecutive periods
  before a handoff is initiated (anti-flap).
- **Two-phase handoff (guarantees ≤1 owner):**
  1. Current owner observes a healthy higher-priority peer → **demotes to `["Observe"]`** and records
     the demotion in its status/heartbeat.
  2. The higher-priority peer observes the demotion (owner at `Observe` / released) → **promotes to
     `["*"]`.**
  A brief *both-Observe* window is acceptable (safe); a *both-`*`* window is not and is prevented by
  requiring the release to be observed before promotion.

## 8. API — `ResilientControlPlane` XR

```yaml
apiVersion: resilient.platform.upbound.io/v1alpha1
kind: ResilientControlPlane            # Namespaced (Crossplane v2)
metadata:
  name: my-set-member
  namespace: default
spec:
  identity:
    id: ctp-us-east-1                  # this CP's id (matches its ControlPlane id)
    provider: aws                      # aws | azure | gcp | alibaba
    region: us-east-1
    geoTag: us
    priority: 1                        # lower = higher priority (preferred primary)
  members:                             # explicit membership (default mechanism)
    - { id: ctp-us-east-1, provider: aws,   region: us-east-1, geoTag: us, priority: 1 }
    - { id: ctp-us-west-2, provider: aws,   region: us-west-2, geoTag: us, priority: 2 }
  gslb:
    hostname: app.cloud.example.com    # the GSLB record whose health is watched
    strategy: failover                 # failover | roundRobin | geoip
  k8gb:                                # optional install (§4.1)
    install: auto                      # XRD default is `never`; examples use `auto` for now
                                       # never (default) | auto | always
    version: v0.15.0
    dnsZones:
      - { parentZone: example.com, loadBalancedZone: cloud.example.com, negTTL: 30 }
    edgeDNSServers: ["1.1.1.1"]
    logLevel: info
    # clusterGeoTag / extGslbClustersGeoTags are derived from spec.identity.geoTag + spec.members
  heartbeat:
    tagKey: last-reconciliation-timestamp-utc
    freshnessTTLSeconds: 180
    writeThrottleSeconds: 60
    # resource type is derived from identity.provider
  policyControl:
    mode: convention                   # convention (default) | patch | both
    managedResourceSelector:           # only used in patch/both mode
      matchLabels: { resilient.crossplane.io/managed: "true" }
  failback:
    automatic: true
    hysteresisPeriods: 3
status:
  role: leader | standby | unknown
  managementPolicy: ["*"]              # THE published decision (convention contract, §9)
  reason: "GSLB healthy, self fresh, no higher-priority peer alive"
  gslb: { healthy: true, isActiveForGeo: true }
  peers:
    - { id: ctp-us-west-2, ageSeconds: 42, fresh: true, gslbHealthy: true, definitivelyDown: false }
  lastHandoffTime: "2026-07-14T20:00:00Z"
  conditions: [...]
```

## 9. Policy-toggle mechanisms

### 9.1 Convention path (default; Test 1 uses this first)
`resilient-ctp` publishes the decision at **`ResilientControlPlane.status.managementPolicy`**. A
**resilience-aware** workload package fetches that XR via `function-extra-resources` and applies the
value to its own managed resources' `spec.managementPolicies`. Clean ownership; the workload package
stays the sole writer of its MRs. This is exactly the `GlobalApp`→GSLB-status consumption pattern,
with `resilient-ctp` as the producer.

**Convention contract:** consumers read `status.managementPolicy` (a string array, `["*"]` or
`["Observe"]`) of the `ResilientControlPlane` in their namespace and set their MRs' management
policies from it. When absent/unknown, consumers default to `["Observe"]` (fail safe).

### 9.2 Patch fallback (later; for unmodified/arbitrary packages)
`resilient-ctp` selects MRs by `policyControl.managedResourceSelector` and patches
`spec.managementPolicies` directly, regardless of owning composite. Enables truly unmodified
third-party packages at the cost of coexisting with the owner's server-side-apply field manager.
Deferred to a later phase; requires an imperative execution surface (a Crossplane v2 Operation or a
small controller) since a pure composition cannot patch resources it does not compose.

## 10. Workload package for Test 1 — rewritten `configuration-aws-s3`

The existing `configuration-aws-s3` is ~2 years old and unsuitable. It will be **rewritten as a
Crossplane v2 package** that lets a user **claim a bucket XR in a region**, and made
**resilience-aware** (consumes `ResilientControlPlane.status.managementPolicy` per §9.1). Two CPs
each hold this XR with the **same deterministic `crossplane.io/external-name`** so both reference the
**same S3 bucket**; leader manages, standby observes; failover flips the policy.

## 11. Credential / read-access model

Per CP, to read a peer's heartbeat tag it needs, for the peer's cloud/region: the **provider
package**, a **ProviderConfig + credential** with *read-tags* permission on the heartbeat resource,
and an **`Observe` MR** pointed at the derived coordinates.

| Scenario | Cross-CP read requirement |
|---|---|
| Test 1 — 2×AWS, same account | one AWS credential w/ `ssm:GetParameters`+`tag:GetResources`; region per MR. Trivial. |
| 2×AWS, different accounts | resource policy or shared read-only assumable role. |
| Test 2/3 — cross-cloud | each CP holds *both/all* providers + a **least-privilege** read cred scoped (by name convention / dedicated container) to the peer heartbeat resources only. |

## 12. Assumptions, dependencies, risks

- **A (target state):** ctp packages install k8gb and `resilient-ctp`; a workload CP has k8gb `Gslb`
  status available.
- **D (k8gb availability):** k8gb install is **not implemented in ANY ctp package yet** (confirmed
  by the user 2026-07-14). `aws-ctp`'s `examples/controlplane/with-k8gb.yaml` shows a `k8gb.enabled`
  knob, but the composition has no k8gb module; `azure-ctp`/`gcp-ctp` have 0 k8gb references. Because
  `resilient-ctp` consumes the k8gb `Gslb` status, k8gb must exist on every test CP.
  - **Resolution (chosen):** `resilient-ctp` **optionally installs k8gb itself** via
    `spec.k8gb.install` (§4.1) — `auto` installs only when k8gb is absent, `always` forces it,
    `never` assumes it exists. This removes the external blocker; no manual Helm step required.
  - **Target (later):** move k8gb ownership into the ctp packages and default `spec.k8gb.install:
    never`, retiring the built-in install.
  - Adds a **`provider-helm`** dependency to this package.
- **D:** cross-cloud read credentials (least-privilege) provisioned per CP.
- **R1 (verify):** tag/label surfacing in `atProvider` on Observe per provider (§5.1).
- **R2:** active-active geo-LB (k8gb `roundRobin`/`geoip`, 3 geo-tags) is **new work** — the source
  package hard-codes the `failover` strategy and ships only a 2-cluster (`eu,us`) example.
- **R3:** patch-fallback field-manager coexistence (deferred phase).
- **Constraint:** author cannot run cloud e2e — all cloud provisioning/e2e is user-run.

## 13. Open items
- Confirm heartbeat resource type per provider after R1 verification.
- Decide patch-fallback execution surface (Operation vs controller) when that phase starts.
- Membership auto-derivation from replicated `ControlPlane` XRs (GitOps) — future enhancement.

---

## 14. Validation — Test 1 result & operational learnings (2026-07-15)

**Test 1 PASSED end-to-end on real AWS.** Two EKS control planes provisioned by
`configuration-aws-ctp` (us-east-1 priority 1, us-west-2 priority 2), each running
`resilient-ctp` + `configuration-aws-s3`, sharing one S3 bucket:

- **Steady state:** us-east-1 = leader (`["*"]`, manages the bucket); us-west-2 =
  standby (`["Observe"]`, observes the *same* bucket).
- **Failover:** pausing the primary → its heartbeat goes stale → the standby
  promotes to leader and its bucket flips to `["*"]`, taking over the same bucket.
- **Failback:** resuming the primary → it reclaims leader, standby returns to
  `["Observe"]`. The bucket survived the whole cycle.

### Learnings folded back into the design

1. **v2 namespaced MRs require `spec.providerConfigRef.kind`** (e.g. `ProviderConfig`).
   Omitting it fails composition apply (`providerConfigRef.kind: Required value`) and
   no MR is ever created. All composed MRs set it.
2. **Do not write `status.conditions` from the function.** The live XRD rejects a
   condition without `lastTransitionTime`; `function-auto-ready` owns the Ready
   condition. (Offline `up test` render does not enforce this — the live apiserver does.)
3. **Heartbeat freshness depends on the provider's *observe poll*, not on watches.**
   There is no AWS→provider event stream, so upjet providers poll (default `--poll=10m`)
   to detect drift. If the poll interval exceeds the freshness TTL, a peer's heartbeat
   *always looks stale* → split-brain. **Architectural note:** globally lowering the
   provider poll (what Test 1 does via a `DeploymentRuntimeConfig --poll=30s`) is **not a
   realistic customer ask** (it multiplies API calls/cost across all that provider's MRs).
   Preferred directions: a **per-MR `crossplane.io/poll-interval`** scoped to only the
   heartbeat resources, or — better long term — observe peers via **provider-kubernetes**
   (a watch-based Kubernetes heartbeat CR on each control plane) instead of cloud-tag
   polling. Set `heartbeat.freshnessTTLSeconds` comfortably above the effective observe lag.
4. **Cross-package signal fetch:** the workload package reads the RCP's
   `status.managementPolicy` via `function-extra-resources` with a **`Selector`,
   `minMatch: 0`** (optional/fail-safe: absent RCP → `Observe`, never a fatal). A
   `Reference` type makes it *required* and breaks the fail-safe path. The namespaced
   composite must be fetched with the `namespace` field set.
5. **Operational caveat (not a product bug):** repeatedly deleting/recreating a
   Configuration can orphan its XRD's ownerReference, leaving a new revision unable to
   "establish control" (config stuck `Healthy=False`, stale composition keeps running).
   Bump image tags cleanly instead of churning; if hit, delete the orphaned XRD so the
   healthy revision re-adopts it.
