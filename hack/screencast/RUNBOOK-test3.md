# Resilient-CTP Test 3 — Screencast Runbook (validated 2026-07-28)

Tri-cloud (AWS EKS + Azure AKS + GCP GKE) active/passive failover of a shared S3
bucket, driven by configuration-resilient-ctp. Validated end-to-end on real infra.

## Packages (dependency-aligned, tag `v0.0.0-sc1`)
Built from the LATEST approved code with three alignment fixes needed for a clean
CO-INSTALL (the raw merged/published tags are NOT mutually aligned):
- `configuration-aws-ctp:v0.0.0-sc1`   (merged #10, namespaced ControlPlane; aws family v2.6.1)
- `configuration-gcp-ctp:v0.0.0-sc1`   (merged #10, namespaced ControlPlane; gcp family v2.6.0)
- `configuration-azure-ctp:v0.0.0-sc1` (stable main; azure family v2.6.0)
- `configuration-resilient-ctp:v0.0.0-sc1` (floats deps → co-installs with aws-s3)
- `configuration-aws-s3:v0.0.0-s3dev9`  (the governed shared bucket, on the workload CPs)

Alignment fixes applied vs merged code (see FINDINGS):
1. All top ctp configs switched to `upbound/` provider-helm+kubernetes (merged used
   `crossplane-contrib/` while their sub-configs use `upbound/` → CRD-ownership clash).
2. azure-ctp: added `provider-azure-containerservice` + `-network` v2.6.0 pins (were
   drifting to v2.6.1 via the aks/network sub-config `v2` floats → family conflict).
3. Family versions differ across clouds (aws v2.6.1, gcp/azure v2.6.0) — HARMLESS
   (independent provider families); intra-cloud they're uniform. Ideal cleanup: all v2.6.2.

## Prereqs
- `~/.aws/credentials` with creds under `[default]` (NOT a named profile) resolving to
  account 656321468224 when AWS_* env vars are unset.
- GCP SA key `~/.gcp/crossplane-playground-*.json`; Azure SP `~/.azure/sp.json`.
- Tools: kind >=0.30, kubectl, up (logged in), docker (≥20 GB free), gcloud, az.

## Phase 1 — mgmt cluster + 3 ctp packages  (`bringup.sh`, ~15 min, one-shot)
Creates the tuned kind cluster → UXP → narrow MRAP → creds → installs the THREE ctp
configs (NOT resilient-ctp — that goes on the workload CPs) → waits healthy → creates
the namespaced AWS/Azure/GCP `ProviderConfig`s in `default`.
Result: 9/9 configs + 18/18 providers healthy.
⚠️ resilient-ctp is NOT installed on mgmt: its floating `provider-aws-ssm` resolves to
v2.6.2, which collides with aws-ctp's family v2.6.1 pin. It belongs on the workload CPs.

## Phase 2 — provision 3 ControlPlanes  (`controlplanes.yaml`, ~15-20 min)
`kubectl --context kind-resilient-mgmt apply -f controlplanes.yaml` — use1 (EKS us-east-1),
aze1 (AKS eastus), gkecp01 (GKE us-central1). Applied zero-config (no namespace) so
aws/gcp-namespaced + azure-cluster all land in `default` uniformly.
⚠️ GCP ControlPlane `id` must be ≥6 chars (GCP service-account minimum) — `gkecp01`, not `gke1`.
Wait until all 3 XRs are `Ready=True`.

## Phase 3 — workload setup + failover  (`workload-setup.sh`)
Extract kubeconfigs first (EKS: mint a durable 24h SA token — the connection-secret token
is ~15 min; AKS: cert kubeconfig; GKE: `gcloud container clusters get-credentials`).
`workload-setup.sh` then, on each CP: installs aws-s3 + resilient-ctp → AWS creds +
namespaced ProviderConfig (⚠️ apply the PC AFTER provider-aws-s3 registers its CRD) +
`aws-fastpoll` DRC (--poll=30s) patched onto provider-aws-ssm → RCP claim (unified AWS-SSM
heartbeat: all members provider=aws, distinct regions, priorities 1/2/3, k8gb=never) →
shared Bucket XR.
Steady state: use1=leader bucket `["*"]`, aze1+gkecp01=standby bucket `["Observe"]`.

Failover drill:
  # simulate use1 death:
  kubectl --kubeconfig kc-use1.yaml annotate resilientcontrolplane use1 crossplane.io/paused=true --overwrite
  # aze1 (priority 2) promotes; gkecp01 stays standby; aze1 bucket -> ["*"]
  # failback:
  kubectl --kubeconfig kc-use1.yaml annotate resilientcontrolplane use1 crossplane.io/paused-
  # use1 reclaims leader immediately; aze1 -> standby

## ⚠️ Screencast timing tunable
The `sc1` build's failover PROMOTION hysteresis is conservative (~14 min from pause to
promote) — correct but slow for a live demo. Failback (to the higher-priority CP) is fast.
For a snappier demo, lower the RCP timing: `heartbeat.freshnessTTLSeconds` (e.g. 90),
`heartbeat.writeThrottleSeconds` (e.g. 20), `failback.hysteresisPeriods` (e.g. 1), and the
DRC `--poll` (e.g. 15s) — keep `freshnessTTL > poll + writeThrottle + margin` to avoid
false-down. (The earlier `v3elect1` tag promoted in ~5 min with the same TTL.)

## Teardown (safe order — avoid orphaning real cloud infra)
1. On each CP: delete Bucket XR + RCP (drains the real S3 bucket + SSM heartbeat params).
2. On mgmt: delete the 3 ControlPlane XRs (drains EKS/AKS/GKE + VPC/RG/network). Refresh
   mgmt aws-creds first so deletion has valid creds.
3. Confirm cloud is clean (no VPC/EKS/S3/SSM, no RG, no GKE/SA), then `kind delete cluster --name resilient-mgmt`.
