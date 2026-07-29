#!/usr/bin/env bash
# Test 3 workload setup: install aws-s3 + resilient-ctp on the 3 workload CPs,
# wire the resilient set (unified AWS-SSM heartbeat + fastpoll), and the shared bucket.
# Assumes kc-use1.yaml / kc-aze1.yaml / kc-gkecp01.yaml already point at the 3 CPs.
set -uo pipefail
SCRATCH="$(cd "$(dirname "$0")" && pwd)"
AWS_S3_TAG=xpkg.upbound.io/upbound/configuration-aws-s3:v0.0.0-s3dev9
RESILIENT_TAG=xpkg.upbound.io/upbound/configuration-resilient-ctp:v0.0.0-sc1
BUCKET=resilient-ctp-shared-656321468224
CPS=(use1 aze1 gkecp01)
declare -A KC=( [use1]="$SCRATCH/kc-use1.yaml" [aze1]="$SCRATCH/kc-aze1.yaml" [gkecp01]="$SCRATCH/kc-gkecp01.yaml" )

# Build the [default] AWS creds blob once (creds now live under [default]).
tmp=$(mktemp); printf '[default]\n' > "$tmp"
env -u AWS_ACCESS_KEY_ID -u AWS_SECRET_ACCESS_KEY -u AWS_SESSION_TOKEN -u AWS_SECURITY_TOKEN \
  aws configure export-credentials --format env-no-export 2>/dev/null \
  | sed -E 's/^AWS_ACCESS_KEY_ID=/aws_access_key_id=/;s/^AWS_SECRET_ACCESS_KEY=/aws_secret_access_key=/;s/^AWS_SESSION_TOKEN=/aws_session_token=/' >> "$tmp"

echo "### 1. install aws-s3 + resilient-ctp on each CP ###"
for cp in "${CPS[@]}"; do
  K="KUBECONFIG=${KC[$cp]} kubectl"
  KUBECONFIG="${KC[$cp]}" kubectl apply -f - >/dev/null <<EOF
apiVersion: pkg.crossplane.io/v1
kind: Configuration
metadata: { name: configuration-aws-s3 }
spec: { package: $AWS_S3_TAG }
---
apiVersion: pkg.crossplane.io/v1
kind: Configuration
metadata: { name: configuration-resilient-ctp }
spec: { package: $RESILIENT_TAG }
EOF
  echo "  $cp: configs applied"
done

echo "### 2. AWS creds + namespaced ProviderConfig + aws-fastpoll DRC on each CP ###"
for cp in "${CPS[@]}"; do
  KC1="${KC[$cp]}"
  KUBECONFIG="$KC1" kubectl create secret generic aws-creds -n default --from-file=credentials="$tmp" --dry-run=client -o yaml | KUBECONFIG="$KC1" kubectl apply -f - >/dev/null
  KUBECONFIG="$KC1" kubectl apply -f - >/dev/null <<'EOF'
apiVersion: pkg.crossplane.io/v1beta1
kind: DeploymentRuntimeConfig
metadata: { name: aws-fastpoll }
spec:
  deploymentTemplate:
    spec:
      selector: {}
      template:
        spec:
          containers:
          - name: package-runtime
            args: ["--poll=30s"]
EOF
  echo "  $cp: creds + PC + fastpoll DRC applied"
done
rm -f "$tmp"

echo "### 3. wait for resilient-ctp healthy on each, then patch provider-aws-ssm -> fastpoll ###"
for cp in "${CPS[@]}"; do
  KC1="${KC[$cp]}"
  for i in $(seq 1 16); do
    h=$(KUBECONFIG="$KC1" kubectl get configuration configuration-resilient-ctp -o jsonpath='{.status.conditions[?(@.type=="Healthy")].status}' 2>/dev/null)
    [ "$h" = "True" ] && break; sleep 20
  done
  # AWS ProviderConfig — apply now that provider-aws-s3 has registered its CRD.
  KUBECONFIG="$KC1" kubectl apply -f - >/dev/null 2>&1 <<'PCEOF'
apiVersion: aws.m.upbound.io/v1beta1
kind: ProviderConfig
metadata: { name: default, namespace: default }
spec:
  credentials: { source: Secret, secretRef: { namespace: default, name: aws-creds, key: credentials } }
PCEOF
  ssm=$(KUBECONFIG="$KC1" kubectl get providers.pkg.crossplane.io -o name 2>/dev/null | grep aws-ssm)
  [ -n "$ssm" ] && KUBECONFIG="$KC1" kubectl patch $ssm --type=merge -p '{"spec":{"runtimeConfigRef":{"name":"aws-fastpoll"}}}' >/dev/null 2>&1
  echo "  $cp: resilient-ctp healthy=$h, ssm patched to fastpoll"
done

echo "### 4. apply RCP claims (unified AWS-SSM heartbeat, k8gb never, priorities 1/2/3) ###"
apply_rcp() { # $1=cp $2=id $3=region $4=geo $5=prio
  KUBECONFIG="${KC[$1]}" kubectl apply -f - >/dev/null <<EOF
apiVersion: resilient.platform.upbound.io/v1alpha1
kind: ResilientControlPlane
metadata: { name: $2, namespace: default }
spec:
  identity: { id: $2, provider: aws, region: $3, geoTag: $4, priority: $5 }
  members:
    - { id: use1, provider: aws, region: us-east-1, geoTag: us, priority: 1 }
    - { id: aze1, provider: aws, region: us-west-2, geoTag: eu, priority: 2 }
    - { id: gkecp01, provider: aws, region: eu-west-1, geoTag: ap, priority: 3 }
  gslb: { hostname: app.cloud.example.com, strategy: failover }
  k8gb: { install: never }
  heartbeat: { livenessKey: last-reconciliation-timestamp-utc, freshnessTTLSeconds: 180, writeThrottleSeconds: 60 }
  policyControl: { mode: convention }
  failback: { automatic: true, hysteresisPeriods: 3 }
EOF
  echo "  $1: RCP $2 (priority $5) applied"
}
apply_rcp use1 use1 us-east-1 us 1
apply_rcp aze1 aze1 us-west-2 eu 2
apply_rcp gkecp01 gkecp01 eu-west-1 ap 3

echo "### 5. apply shared Bucket XR on each CP ###"
for cp in "${CPS[@]}"; do
  KUBECONFIG="${KC[$cp]}" kubectl apply -f - >/dev/null <<EOF
apiVersion: s3.aws.platform.upbound.io/v1alpha1
kind: Bucket
metadata: { name: shared-bucket, namespace: default }
spec:
  parameters: { region: us-east-1, bucketName: $BUCKET }
EOF
  echo "  $cp: bucket XR applied"
done
echo "### DONE (workload setup applied) ###"
