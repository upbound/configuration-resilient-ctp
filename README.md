# configuration-resilient-ctp
A configuration package that makes a control plane resilient by introducing generic multi-control plane failover functionality.

Exactly **one** control plane in a resilience set is the leader (holds each
managed resource's intended `managementPolicies`); the others are reduced to
`Observe`. Failover/failback is driven by k8gb GSLB health plus a cross-control-plane
heartbeat ledger. **Single management leader at all times — in every scenario and
every k8gb strategy.** Multiple standbys/followers are fine as long as they are
never promoted simultaneously.

> **"Active-active" refers to workload TRAFFIC, not leadership.** k8gb
> `roundRobin`/`geoip` distribute app traffic across several geos at once, but the
> set still has exactly one *management* leader (`["*"]`) with the rest at
> `["Observe"]`. All control planes receive the same claims/XRs; only the leader
> reconciles them. Two management leaders would be split-brain — never permitted.

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

## Heartbeat read mode: `mr` (default) vs `directApi`

Peers advertise liveness + role by stamping a tag/label on a small cloud
resource. How this control plane *reads* a peer's heartbeat is set by
`spec.heartbeat.read`:

- **`mr` (default)** — reads the provider-observed `Observe` MR. Simple, no extra
  credentials, but gated by the provider observe-poll (`--poll=10m` default): a
  peer that stops updating (or an MR that has never synced) can look stale for up
  to the poll interval. Keep `freshnessTTLSeconds` comfortably above the poll if
  you rely on this mode for takeover.
- **`directApi`** — the composition function reads the peer's cloud API directly,
  so peer liveness is seconds-fresh and independent of `--poll`. This is what
  makes an *app-health* failover (primary alive but stepped down) promote the
  secondary promptly instead of waiting on the poll. **Prerequisite:** the
  function pod needs read-only credentials for every peer's cloud, wired via a
  pre-created `Function` + `DeploymentRuntimeConfig` (the package manager adopts
  the pre-created Function). Apply `examples/directapi-heartbeat.yaml` and set
  `spec.heartbeat.read: directApi`. Without those creds the direct read fails and
  the peer is treated as **unreadable → this CP holds standby** (fail-safe), so
  wire the creds before flipping the mode.

  > The example's `Function` pins an immutable digest that must match the
  > installed Configuration release — **re-pin it on every upgrade** (see the
  > file header). Least-privilege IAM per cloud is documented in `docs/SPEC.md` §11
  > (AWS needs only `ssm:ListTagsForResource`).
