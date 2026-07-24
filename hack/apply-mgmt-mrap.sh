#!/usr/bin/env bash
# Bootstrap the resilient-mgmt kind cluster with a NARROW MRAP set so only the
# MRDs the ctp configs + resilient-ctp compose are activated (etcd-load fix).
# See docs/MGMT-MRAP-DESIGN.md. Run on a FRESH cluster BEFORE the ctp configs
# reconcile — deactivating an MRD with live MRs strands cloud resources.
#
# Ordering matters (crossplane/crossplane#6984: deleting the "*" MRAP does NOT
# deactivate already-Active MRDs — the "*" must be PREVENTED, not removed):
#   1. install UXP/Crossplane with provider.defaultActivations: [] (no "*" MRAP)
#   2. apply hack/mgmt-mrap.yaml
#   3. assert no "*" MRAP exists
#   4. THEN install the ctp Configurations / provision ControlPlanes
set -euo pipefail

CTX="${KUBECONTEXT:-kind-resilient-mgmt}"
KUBECTL="kubectl --context ${CTX}"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "==> Target context: ${CTX}"
${KUBECTL} cluster-info >/dev/null

echo "==> Step 1 reminder: UXP/Crossplane MUST be installed with Helm value"
echo "    provider.defaultActivations: []   (so the catch-all \"*\" MRAP is never created)."
echo "    This script does NOT install Crossplane; it assumes that was done."

echo "==> Step 2: apply the narrow MRAPs"
${KUBECTL} apply -f "${DIR}/mgmt-mrap.yaml"

echo "==> Step 3: assert no catch-all \"*\" MRAP exists (would re-activate everything)"
if ${KUBECTL} get managedresourceactivationpolicy -o json \
    | grep -q '"\*"'; then
  echo "!! FAIL: a MRAP still activates \"*\". Reinstall UXP with" >&2
  echo "!! provider.defaultActivations: [] (deleting the \"*\" MRAP does NOT" >&2
  echo "!! deactivate already-Active MRDs — see #6984)." >&2
  exit 1
fi
echo "    OK: no \"*\" MRAP."

echo "==> Applied MRAPs:"
${KUBECTL} get managedresourceactivationpolicy

cat <<'EOF'

==> Next:
  4. Install the ctp Configurations + provision ControlPlanes.
  5. Validate: every composed MR reaches Established/Synced with ZERO
     "no matches for kind". Resolve any hit by adding the exact MRD name
     (kubectl get managedresourcedefinitions | grep <cloud>.m.upbound.io),
     never by re-widening to "*". See docs/MGMT-MRAP-DESIGN.md.
  Teardown: keep these MRAPs until `kubectl get managed` is empty.
EOF
