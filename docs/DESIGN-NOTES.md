# Design Notes

Answers to recurring design questions about how `configuration-resilient-ctp`
and [`function-management-policies`](https://github.com/upbound/function-management-policies)
relate, what they govern, and where the honest boundaries are. Complements
[`SPEC.md`](./SPEC.md).

## 1. Decision and application are deliberately decoupled

The leadership **decision** and the policy **application** are two separate
components, joined by a thin contract:

- **`configuration-resilient-ctp`** only *decides* a role and publishes it on a
  `ResilientControlPlane` — `status.role` (`leader`/`standby`) and
  `status.managementPolicy` (the coarse `["*"]`/`["Observe"]` signal). It writes
  nothing onto the workload resources.
- **`function-management-policies`** *reads* that signal (via a Crossplane
  required-resources requirement — it declares it needs the local
  `ResilientControlPlane` and Crossplane supplies it) and *applies* policy to
  the composed resources.

The only coupling is: *"some resource carries a leader/standby verdict I can
select."* That is intentionally loose. `function-management-policies` is **not**
wedded to this controller — point its `resilientControlPlane` selector at any
CR that carries an active/passive verdict and it behaves identically.

### Relationship to `function-active-passive`

Upbound's [`function-active-passive`](https://github.com/upbound/function-active-passive)
is a parallel generalization of the k8gb-bluegreen behavior. Because our
decision/application seam is so thin, the two are complementary rather than
competing: `function-active-passive` (or any decision engine) can be the source
of the active/passive verdict, and `function-management-policies` the applier
that turns that verdict into per-resource `managementPolicies`. We have not
integrated against it directly yet; the seam is a selector over a status field,
so wiring one to the other is configuration, not code.

## 2. What gets governed — the selector and non-managed resources

`function-management-policies` governs a composed resource when **either**:

1. `resourceSelector.matchLabels` is set and the resource matches, **or**
2. (default) the resource already declares `spec.managementPolicies`.

`managementPolicies` is a **Managed Resource** field — only MRs have it. In
Crossplane v2 practice that covers almost everything you compose: cloud MRs, but
also `provider-kubernetes` `Object`s and `provider-helm` `Release`s both carry
`managementPolicies`. So the toggle reaches essentially all provider-driven side
effects.

**Non-managed composed resources** — a nested Composite/XR, or a raw object with
no `managementPolicies` — are **left untouched**. We never fabricate a field a
resource doesn't have. This is the opt-in model working as intended: a
composition author opts a resource into resilience by giving it an intended
`managementPolicies` (or a selector label); everything else is ignored.

**Boundary (stated honestly):** if a composition emits a non-MR resource that
itself causes an external write, the `managementPolicies` toggle cannot
neutralize it on a standby — there is no policy surface to flip. That case needs
**composition-level gating** (don't compose the resource on standbys), which is
outside this function's job. The selector is an escape hatch to *include*
resources explicitly; it is not a way to *invent* passivity for resources that
have no policy field.

## 3. external-name synchronization (the hard part)

This is the crux of any active/passive-over-a-shared-resource design, and it is
important to be precise: **`function-management-policies` does not synchronize
external-names.** It toggles policy, nothing else.

For a standby to `Observe` the *same* external object the leader manages, both
sides must resolve to the **same `crossplane.io/external-name`**. We achieve
that the way k8gb-bluegreen does — **the composition sets a deterministic
external-name from the spec.** In Test 1, `spec.parameters.bucketName` maps to a
fixed external-name, so both control planes point at the one shared S3 bucket.

### Where this is a constraint

The deterministic-external-name requirement works cleanly for resources with an
author-chosen identity (buckets, DBs with chosen IDs, DNS records). It does
**not** cover resources whose external-name is **server-generated /
non-deterministic**: if the leader creates the resource and the provider assigns
a random external-name, a standby cannot know it in order to observe the same
object. This is a real, known blocker of the pattern — not something this
function currently solves.

### A promising fix: ledger-based external-name propagation

The resilience layer already maintains a **cross-control-plane channel** — the
heartbeat ledger (a per-CP cloud-resource tag today, or the DNS-TXT backend).
That same channel can carry more than a timestamp:

- the **leader publishes** the external-names it has resolved for the shared
  resources into the ledger;
- **standbys adopt** those external-names before observing.

That turns external-name synchronization into another thing the resilience layer
*distributes*, rather than a constraint the composition author must satisfy by
hand. It is not implemented yet; it is the natural next step and would directly
remove the deterministic-external-name requirement for server-generated IDs.

## 4. Summary of honest boundaries

| Concern | Status |
| --- | --- |
| Decision vs application coupling | Decoupled via a selectable status field; works with any decision engine |
| MR side effects on standbys | Handled — reduced to the passive policy |
| Non-MR side effects on standbys | **Out of scope** for the policy toggle; needs composition-level gating |
| Shared resource with author-set external-name | Handled — deterministic external-name in the composition |
| Shared resource with server-generated external-name | **Open** — needs ledger-based external-name propagation (future) |

## 5. Single-claim UX and where prerequisites are irreducible

The product goal is that an app team applies **one** `ResilientControlPlane`
and nothing else. Everything *installable* is therefore handled by the package,
idempotently:

- `provider-helm` and `provider-kubernetes` are package dependencies.
- k8gb is auto-installed by the composition (`k8gb.install: auto`), gated by a
  presence check so re-installs are no-ops.
- The composition creates the k8gb `Gslb` (`gslb.manage: true`). It is composed
  **directly** as a Crossplane v2 resource — v2 lets a composition manage any
  Kubernetes resource, so no provider-kubernetes `Object` wrapper is needed
  (this matches configuration-k8gb-bluegreen). Creation is gated on the k8gb
  operator Release being `Ready`, so a `Gslb` is never applied before its CRD
  exists.

Two classes of prerequisite are genuinely irreducible and cannot live inside the
claim:

1. **External state** — a delegated DNS zone and cloud credentials. These live
   outside any cluster; "bring a domain + creds" is the accepted minimum.
2. **A one-time RBAC bootstrap** — provider-helm needs broad rights to install
   k8gb's cluster-scoped resources, and the crossplane service account needs
   rights on `k8gb.absa.oss/gslbs`. A Kubernetes package **cannot grant itself**
   these (the API server's privilege-escalation guard forbids it — if it could,
   it would be an exploit). So this bootstrap is applied once per control plane
   by the platform/blueprint, shipped as `examples/providerconfig-helm.yaml` and
   `examples/rbac-k8gb.yaml`. The intent is to fold it into the control-plane
   provisioning layer so operators never run it by hand either.

The `Gslb` is a non-managed resource, so — like other non-MR side effects
(§2) — it sits **outside** the managementPolicies toggle. That is correct: the
`Gslb` is the health *signal* that drives leadership, not a protected workload
resource. Only the shared managed resources are governed by the leader/standby
policy.

## 6. Single leader at all times — in EVERY scenario, EVERY strategy

**Invariant (non-negotiable): a resilience set has exactly ONE management leader
at any instant.** Across every topology (2×AWS, AWS+Azure, tri-cloud) and every
k8gb strategy (`failover`, `roundRobin`, `geoip`), exactly one control plane
holds `managementPolicies: ["*"]` over the shared managed resources; all others
are reduced to `["Observe"]`. Multiple standbys/followers are fine — they must
**never** be promoted simultaneously. Two management leaders reconciling the same
managed resources is the split-brain this entire design exists to prevent.

**"Active-active" is a DATA-PLANE term, not a leadership term.** It is a common
source of confusion, so state it plainly:

- **Data plane (workload traffic).** k8gb `failover` sends app traffic to one geo
  at a time; `roundRobin`/`geoip` send it to *several geos simultaneously*. That
  simultaneous traffic distribution is what "active-active" means.
- **Control-plane management.** Independent of the above. All CPs in the set
  receive the same XRs/claims, but only the single leader reconciles them
  (`["*"]`); followers hold `["Observe"]`.

So "active-active" = **1 management leader + 1..N followers, all receiving the
same requests, with multiple geos serving app traffic**. It does NOT mean
multiple management leaders — that is never permitted.

**How the single leader is enforced.** Priority ordering + the two-factor rule.
The higher-peer promotion gate (functions/…/election.py) blocks a lower-priority
CP unless every higher-priority peer is positively not-leading: an *unreadable*
higher peer holds it at standby (fail-safe — never promote on our own
blindness); a fresh higher peer still advertising `role=leader` holds it (the
independent heartbeat is the partition tie-breaker); only a stepped-down
(`role=standby`) higher peer that GSLB has arbitrated away in `failover` mode, or
a stale/dead higher peer, clears the gate. GSLB is the fast, poll-independent
failure trigger; the cross-CP heartbeat is the slower confirmation term (see
docs/SPEC.md §Gotchas on the provider observe-poll vs. freshness-TTL
relationship). In `roundRobin`/`geoip` GSLB gives no exclusivity (all healthy
geos are "active"), so leadership rests **entirely** on priority + heartbeat —
still exactly one leader.
