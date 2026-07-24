# Narrow MRAP design — resilient-mgmt cluster

Design + rationale for `hack/mgmt-mrap.yaml`. Goal: cut the ~500+ activated CRDs
(three full provider families under the default `"*"` MRAP) down to only the
MRDs the four Configurations compose, to stop the etcd-WAL-fsync saturation that
crashlooped the kind cluster (`MGMT-CLUSTER-PERFORMANCE.md`). Reviewed through 8
independent lenses; the source-verified findings below shaped the manifest.

## Mechanism (source-verified, Crossplane v2.x)
- `ManagedResourceActivationPolicy` (`apiextensions.crossplane.io/v1alpha1`),
  `spec.activate: []` of MRD names (`<plural>.<group>`) or glob patterns.
- **Match = Go `path/filepath.Match`** (`apis/apiextensions/v1alpha1/mrd_policy_types.go`).
  `*` matches any run of non-`/` chars, and MRD names contain no `/`, so
  **`*` SPANS DOTS**. Therefore:
  - `*.ec2.aws.m.upbound.io` = every ec2 MRD (safe — ec2 is a leaf group), but
  - `*.azure.m.upbound.io` = the ENTIRE azure family (every `<svc>.azure…`) ≡ `"*"`
    for Azure. The base `azure` group is thus enumerated by EXACT MRD name.
- MRAPs are **additive** — an MRD activates if it matches ANY MRAP. So the default
  `"*"` MRAP must be prevented, not just narrowed.
- Per **crossplane/crossplane#6984**, deleting/overwriting the `"*"` MRAP does NOT
  flip already-Active MRDs back to Inactive → prevent it at install
  (`provider.defaultActivations: []`), never "delete after install".
- Activation gates CRD establishment + controller start (inactive MRD ⇒ no CRD ⇒
  no informer/watch/etcd writes) → real load reduction.
- **Complementary only:** MRAP does NOT reduce provider-*pod* leader-election lease
  writes (fsync-bound, every few seconds per installed provider pod). Pair with
  the `MGMT-CLUSTER-PERFORMANCE.md` fixes: trim provider families to specific
  service providers, tmpfs etcd, relaxed leader-election.

## Strategy
- **Small leaf groups → `*.<group>`** (eks, iam, ssm, container, cloudplatform,
  storage, containerservice, managedidentity, authorization, helm, kubernetes):
  robust to a ctp upgrade that composes another resource within the group;
  negligible CRD count.
- **The 3 largest groups → per-MRD exact names** (AWS `ec2`, GCP `compute`, Azure
  `network` — each ~90–120 CRDs; a whole-group glob would re-activate ~300 CRDs ≈
  the original problem).
- **Base `azure` group → exact `resourcegroups.azure.m.upbound.io`** (un-globbable
  in isolation).

## Activated set (evidence: MRs observed live during 2026-07-23/24 teardown + Test 2)
| Cloud | Activated | For |
|-------|-----------|-----|
| AWS | `*.eks`, `*.iam`, `*.ssm`; per-MRD ec2 (vpcs/subnets/internetgateways/routetables/routes/routetableassociations/mainroutetableassociations/securitygroups/securitygrouprules) | EKS + IAM (aws-ctp); SSM heartbeat |
| GCP | `*.container`, `*.cloudplatform`, `*.storage`; per-MRD compute (networks/subnetworks) | GKE + Workload Identity (gcp-ctp); GCS heartbeat |
| Azure | `resourcegroups.azure` (exact), `*.containerservice`, `*.managedidentity`, `*.authorization`; per-MRD network (virtualnetworks/subnets) | AKS + identity (azure-ctp); ResourceGroup heartbeat |
| shared | `*.helm.m.crossplane.io`, `*.kubernetes.m.crossplane.io` | UXP/k8gb/cert-manager Releases; k8s Objects (incl. k8gb Gslb) |

Intentionally absent (not MRAP-controlled): `protection.crossplane.io`
Usage/ClusterUsage (core Crossplane teardown guards — stay served), and k8gb
`Gslb` (`k8gb.absa.oss`, a CRD from the k8gb helm chart, not a provider MRD).

## Known-uncertain — confirm on rebuild (all fail LOUD at bootstrap, never silently at failover)
- Azure plurals + full AKS peer set; the four Azure entries carry the load only
  after the base-group fix, so validate them explicitly.
- Exact ec2/compute/network plural MRD names (a wrong plural = "no matches for
  kind" at create, caught by validation step 2).
- Whether aws-ctp composes KMS/CloudWatch; whether gcp-ctp composes
  `services.serviceusage.gcp` (API self-enable) or private-GKE compute peers
  (routers/nat/firewalls). Commented conditional entries are ready in the manifest.
- Crossplane GC behavior when an MRD is deactivated while live CRs exist (the one
  load-bearing safety unknown — decides whether a mid-life re-narrow is ever safe).
- Confirm deactivated MRDs stop provider watches (validates the load premise).

## Operating rules (teardown/operational-safety lenses)
1. Apply on a FRESH cluster BEFORE the ctp configs reconcile (`hack/apply-mgmt-mrap.sh`).
2. **MRAPs must outlive the last MR** — remove/narrow them only after
   `kubectl get managed` is empty. Deactivating a group with live MRs strands
   real cloud resources (finalizers can't run).
3. **Widen-only.** Any narrowing (incl. a future per-MRD tightening of a leaf
   group) is a superset migration: enumerate live MRs of the affected groups and
   confirm coverage before applying.

## Re-validation (repeatable — run whenever a ctp config version is bumped)
1. Under-activation: provision each cloud's CP; ZERO `no matches for kind`.
   Resolve any hit by finding the exact MRD (`kubectl get managedresourcedefinitions
   | grep <cloud>.m.upbound.io`) and adding it — never re-widen to `*`.
2. Over-activation/drift: `kubectl get managed -o custom-columns=G:.apiVersion`
   grouped, diffed against `activate:` — flag activated-but-never-composed entries.
3. Pre-upgrade gate: render/simulate the new ctp tag and diff its composed
   resource groups against `activate:` before promoting it.
4. Load: `kubectl get mrd | wc -l` + etcd fsync latency vs the default-`*` baseline.

## Detection (silent post-upgrade breakage)
Alert on XR/MR `Synced=False` whose message contains `no matches for kind` →
"likely a de-activated MRD; check hack/mgmt-mrap.yaml".
