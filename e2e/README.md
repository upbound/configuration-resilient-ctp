# Test 1 — 2× AWS control plane failover/failback (runbook)

Validates that two control planes sharing one S3 bucket fail over and back:
the leader holds `managementPolicies: ["*"]`, the standby `["Observe"]`, driven
by the SSM heartbeat ledger + priority (GSLB degraded via `k8gb.install: never`).

## Topology
- Management plane: kind `resilient-mgmt` + UXP + `configuration-aws-ctp`.
- CP A: EKS in **us-east-1**, id `resilient-use1`, priority 1 (primary).
- CP B: EKS in **us-west-2**, id `resilient-usw2`, priority 2 (standby).
- Shared bucket: one S3 bucket in us-east-1 (same `bucketName` on both CPs).
- Heartbeats: SSM Parameter `recon-heartbeat-<id>` in each CP's region; peers
  observe each other cross-region (same AWS account).

## Prerequisites
- Management plane healthy (done) and both `ControlPlane` XRs `READY=True`.
- `up` re-authenticated so packages can be pushed (`up login`), OR an
  alternative registry — see "Package delivery" below.

## Steps

### 1. Get kubeconfigs for both EKS control planes
```bash
aws eks update-kubeconfig --name <use1-eks-name> --region us-east-1 --alias use1
aws eks update-kubeconfig --name <usw2-eks-name> --region us-west-2 --alias usw2
# EKS names: kubectl --context kind-resilient-mgmt get cluster.eks.aws.m.upbound.io
```

### 2. Package delivery
Push the two packages to a registry the EKS CPs can pull:
```bash
cd configuration-resilient-ctp && up project push --tag v0.0.0-resilientdev1
cd configuration-aws-s3        && up project push --tag v0.0.0-s3dev1
```
Then reference those image tags in the Configuration installs in step 3.

### 3. On EACH EKS control plane (contexts use1, usw2)
```bash
for CTX in use1 usw2; do
  kubectl --context $CTX create ns crossplane-system 2>/dev/null || true
  # UXP is already installed by configuration-aws-ctp.
  kubectl --context $CTX create secret generic aws-creds -n default \
    --from-file=credentials=$HOME/.aws/credentials
  kubectl --context $CTX apply -f e2e/manifests/providerconfig.yaml
  kubectl --context $CTX apply -f e2e/manifests/packages.yaml   # providers + configs
done
```

### 4. Apply the resilience member + shared bucket
```bash
kubectl --context use1 apply -f e2e/manifests/resilient-use1.yaml
kubectl --context usw2 apply -f e2e/manifests/resilient-usw2.yaml
kubectl --context use1 apply -f e2e/manifests/bucket.yaml
kubectl --context usw2 apply -f e2e/manifests/bucket.yaml
```

### 5. Verify steady state
- `use1`: `ResilientControlPlane member` → `status.role: leader`, `status.managementPolicy: ["*"]`; Bucket MR `["*"]`, Ready.
- `usw2`: `status.role: standby`, `["Observe"]`; Bucket MR `["Observe"]`.
- Bucket exists once in AWS: `aws s3api head-bucket --bucket <bucketName>`.

### 6. Failover (simulate use1 outage)
```bash
kubectl --context use1 annotate resilientcontrolplane member -n default \
  crossplane.io/paused=true --overwrite
```
use1's heartbeat SSM parameter goes stale; after `freshnessTTLSeconds` +
hysteresis, `usw2` → `role: leader`, Bucket → `["*"]` (takes over the bucket).

### 7. Failback
```bash
kubectl --context use1 annotate resilientcontrolplane member -n default \
  crossplane.io/paused- --overwrite
```
use1 resumes heartbeating; two-phase handoff: `usw2` demotes to standby, then
`use1` re-assumes leader `["*"]`.

The Python harness `e2e/harness.py` automates steps 4–7 with assertions
(kubernetes client + boto3). See "Package delivery" for the registry choice.
