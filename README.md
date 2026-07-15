# configuration-resilient-ctp
A configuration package that makes a control plane resilient by introducing generic multi-control plane failover functionality.

## Making a workload resilience-aware

The recommended way to have a composition follow this package's leadership
decision is the drop-in [`function-management-policies`](https://github.com/upbound/function-management-policies):
add it late in the composition's pipeline (after the resource-composing
functions). The leader honors each managed resource's own intended
`managementPolicies`; standbys are reduced to `Observe`. See `docs/SPEC.md` §9.0.
