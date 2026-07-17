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
