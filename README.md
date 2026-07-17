# configuration-resilient-ctp
A configuration package that makes a control plane resilient by introducing generic multi-control plane failover functionality.

Exactly **one** control plane in a resilience set is the leader (holds each
managed resource's intended `managementPolicies`); the others are reduced to
`Observe`. Failover/failback is driven by k8gb GSLB health plus a cross-control-plane
heartbeat ledger. Single leader at all times; multiple standbys are fine as long
as they are never promoted simultaneously.

## One claim, minimal prerequisites

An app team applies **one** `ResilientControlPlane` per control plane (see
`examples/`). Everything installable is handled by the package:

- `provider-helm` and `provider-kubernetes` are package **dependencies** (auto-installed).
- k8gb is **auto-installed** by the composition (`k8gb.install: auto`, the default)
  with an idempotent presence check — no manual `helm install`.
- The composition **creates the k8gb `Gslb`** itself (`gslb.manage: true`, default),
  so a single claim yields a working GSLB signal.

The only irreducible prerequisites are **external** — bring a delegated **DNS zone**
and **cloud credentials** — plus a **one-time platform bootstrap** (RBAC so
provider-helm can install k8gb and the crossplane SA can manage `Gslb`s), which a
Kubernetes package cannot self-grant. Those ship as `examples/providerconfig-helm.yaml`
and `examples/rbac-k8gb.yaml` and are meant to be folded into the control-plane
blueprint so operators and end users never apply them by hand.

## Making a workload resilience-aware

The recommended way to have a composition follow this package's leadership
decision is the drop-in [`function-management-policies`](https://github.com/upbound/function-management-policies):
add it late in the composition's pipeline (after the resource-composing
functions). The leader honors each managed resource's own intended
`managementPolicies`; standbys are reduced to `Observe`. See `docs/SPEC.md` §9.0.
