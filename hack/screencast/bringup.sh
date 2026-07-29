#!/usr/bin/env bash
# One-go bring-up of resilient-mgmt through config-apply (corrected: gcp/azure=aligned3).
set -euo pipefail
CTX=kind-resilient-mgmt
REPO=~/upbound/dev/go/src/github.com/upbound/configuration-resilient-ctp
AZURE_SP="$HOME/.azure/sp.json"

echo "### 1. cluster (teardown+create+caps) ###"
"$REPO/hack/create-mgmt-cluster.sh" >/dev/null 2>&1
kubectl --context "$CTX" get nodes --no-headers | awk '{print "   node "$1" "$2}'

echo "### 2. UXP ###"
up uxp install --kubecontext "$CTX" 2>&1 | grep -iE 'installed|error'
kubectl --context "$CTX" -n crossplane-system rollout status deploy/crossplane --timeout=180s | tail -1

echo "### 3. wait apollo 3/3 ###"
for i in $(seq 1 16); do
  a=$(kubectl --context "$CTX" -n crossplane-system get pods --no-headers 2>/dev/null | grep apollo | awk '{print $2}')
  [ "$a" = "3/3" ] && { echo "   apollo $a"; break; }; sleep 15
done

echo "### 3a. relax crossplane-core LE ###"
kubectl --context "$CTX" -n crossplane-system set env deploy/crossplane \
  LEADER_ELECTION_LEASE_DURATION=60s LEADER_ELECTION_RENEW_DEADLINE=45s LEADER_ELECTION_RETRY_PERIOD=10s >/dev/null
kubectl --context "$CTX" -n crossplane-system rollout status deploy/crossplane --timeout=120s | tail -1

echo "### 4. narrow MRAP ###"
kubectl --context "$CTX" delete managedresourceactivationpolicy --all >/dev/null 2>&1 || true
KUBECONTEXT="$CTX" "$REPO/hack/apply-mgmt-mrap.sh" >/dev/null 2>&1
echo "   MRAPs: $(kubectl --context "$CTX" get managedresourceactivationpolicy --no-headers | wc -l | tr -d ' ')"

echo "### 5. cred secrets (crossplane-system) ###"
tmp=$(mktemp); printf '[default]\n' > "$tmp"
env -u AWS_ACCESS_KEY_ID -u AWS_SECRET_ACCESS_KEY -u AWS_SESSION_TOKEN -u AWS_SECURITY_TOKEN \
  aws configure export-credentials --format env-no-export 2>/dev/null | sed -E 's/^AWS_ACCESS_KEY_ID=/aws_access_key_id=/;s/^AWS_SECRET_ACCESS_KEY=/aws_secret_access_key=/;s/^AWS_SESSION_TOKEN=/aws_session_token=/' >> "$tmp"
kubectl --context "$CTX" create secret generic aws-creds -n crossplane-system --from-file=credentials="$tmp" --dry-run=client -o yaml | kubectl --context "$CTX" apply -f - >/dev/null; rm -f "$tmp"
kubectl --context "$CTX" create secret generic gcp-creds -n crossplane-system --from-file=credentials="$HOME/.gcp/crossplane-playground-4f420360dda5.json" --dry-run=client -o yaml | kubectl --context "$CTX" apply -f - >/dev/null
kubectl --context "$CTX" create secret generic azure-creds -n crossplane-system --from-file=credentials="$AZURE_SP" --dry-run=client -o yaml | kubectl --context "$CTX" apply -f - >/dev/null
echo "   secrets: $(kubectl --context "$CTX" -n crossplane-system get secret aws-creds gcp-creds azure-creds --no-headers | wc -l | tr -d ' ')/3"

echo "### 6. install 3 ctp configs on mgmt (resilient-ctp goes on the WORKLOAD CPs, not here) ###"
kubectl --context "$CTX" apply -f - <<EOF
apiVersion: pkg.crossplane.io/v1
kind: Configuration
metadata: { name: configuration-aws-ctp }
spec: { package: xpkg.upbound.io/upbound/configuration-aws-ctp:v0.0.0-sc1 }
---
apiVersion: pkg.crossplane.io/v1
kind: Configuration
metadata: { name: configuration-azure-ctp }
spec: { package: xpkg.upbound.io/upbound/configuration-azure-ctp:v0.0.0-sc1 }
---
apiVersion: pkg.crossplane.io/v1
kind: Configuration
metadata: { name: configuration-gcp-ctp }
spec: { package: xpkg.upbound.io/upbound/configuration-gcp-ctp:v0.0.0-sc1 }
EOF

echo "### 7. wait for all configs + providers healthy (3 top + 6 sub configs, ~18 providers) ###"
for i in $(seq 1 40); do
  ch=$(kubectl --context "$CTX" get configuration --no-headers 2>/dev/null | awk '$2=="True"&&$3=="True"' | wc -l | tr -d ' ')
  ct=$(kubectl --context "$CTX" get configuration --no-headers 2>/dev/null | wc -l | tr -d ' ')
  ph=$(kubectl --context "$CTX" get providers.pkg.crossplane.io --no-headers 2>/dev/null | awk '$2=="True"&&$3=="True"' | wc -l | tr -d ' ')
  pt=$(kubectl --context "$CTX" get providers.pkg.crossplane.io --no-headers 2>/dev/null | wc -l | tr -d ' ')
  echo "   [$i] configs=$ch/$ct providers=$ph/$pt"
  [ "$ct" -ge 9 ] && [ "$ch" = "$ct" ] && [ "$pt" -ge 15 ] && [ "$ph" = "$pt" ] && { echo "   HEALTHY"; break; }
  sleep 30
done

echo "### 8. namespaced ProviderConfigs in default (creds + PC) — REQUIRED for CP provisioning ###"
# ctp ControlPlane compositions emit NAMESPACED *.m.upbound.io MRs in `default`
# referencing {kind: ProviderConfig, name: default}; secretRef.namespace not honored
# → creds secret must ALSO be in `default`.
for s in aws-creds gcp-creds azure-creds; do
  kubectl --context "$CTX" -n crossplane-system get secret $s -o json 2>/dev/null \
    | python3 -c 'import json,sys;d=json.load(sys.stdin);[d["metadata"].pop(k,None) for k in ("namespace","resourceVersion","uid","creationTimestamp","ownerReferences")];d["metadata"]["namespace"]="default";print(json.dumps(d))' \
    | kubectl --context "$CTX" apply -f - >/dev/null
done
kubectl --context "$CTX" apply -f - <<EOF
apiVersion: aws.m.upbound.io/v1beta1
kind: ProviderConfig
metadata: { name: default, namespace: default }
spec:
  credentials: { source: Secret, secretRef: { namespace: default, name: aws-creds, key: credentials } }
---
apiVersion: azure.m.upbound.io/v1beta1
kind: ProviderConfig
metadata: { name: default, namespace: default }
spec:
  credentials: { source: Secret, secretRef: { namespace: default, name: azure-creds, key: credentials } }
---
apiVersion: gcp.m.upbound.io/v1beta1
kind: ProviderConfig
metadata: { name: default, namespace: default }
spec:
  projectID: crossplane-playground
  credentials: { source: Secret, secretRef: { namespace: default, name: gcp-creds, key: credentials } }
EOF
echo "   PCs: $(kubectl --context "$CTX" -n default get providerconfig.aws.m.upbound.io,providerconfig.azure.m.upbound.io,providerconfig.gcp.m.upbound.io --no-headers 2>/dev/null | grep -c default)/3"
echo "### DONE (bring-up complete, provisioning-ready) ###"
