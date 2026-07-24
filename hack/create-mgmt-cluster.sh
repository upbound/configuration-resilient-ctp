#!/usr/bin/env bash
# Create the resilient-mgmt kind cluster load-ready on a single Docker Desktop VM.
#
# kind CANNOT set per-node RAM/CPU (all "nodes" are containers sharing one VM),
# and kubelet systemReserved/kubeReserved do NOT cap host RAM — every node
# reports the full VM memory, so the scheduler overcommits and the HOST
# OOM-killer can kill etcd/apiserver before kubelet eviction fires. The only real
# per-node ceilings are at the Docker layer, applied here after kind creates the
# node containers:
#   * --memory        : hard per-node RAM cap (prevents VM-wide OOM)
#   * --cpuset-cpus    : PINS each node to dedicated physical cores. This is what
#                        actually protects etcd/apiserver: a --cpus quota only
#                        caps a ceiling, whereas pinning the control-plane to its
#                        own cores makes worker/provider CPU bursts physically
#                        unable to starve it (etcd is fsync-bound, but apiserver
#                        gets CPU-heavy under ~116-MRD provider list/watch, and
#                        leader-election renewals depend on it keeping up).
#
# Budget: Docker VM = 14 CPU / 27 GB.
#   control-plane: 6 GiB, cores 0-3   (4 dedicated cores for etcd+apiserver)
#   worker:        9 GiB, cores 4-8   (5 cores)
#   worker2:       9 GiB, cores 9-13  (5 cores)
#   => 24 GiB / all 14 cores; ~3 GiB left for VM/dockerd overhead.
# Tune below if your Docker VM differs (check: docker info).
set -euo pipefail

CLUSTER=resilient-mgmt
CFG="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/resilient-mgmt-kind.yaml"
CP_MEM=6g;  CP_CPUSET=0-3
W_MEM=9g;   W1_CPUSET=4-8;   W2_CPUSET=9-13

echo "==> Recreating kind cluster '${CLUSTER}' (delete any prior — --name is mandatory)"
kind delete cluster --name "${CLUSTER}" || true
kind create cluster --config "${CFG}"

echo "==> Applying per-node Docker caps (real per-node RAM + dedicated cores)"
for n in $(kind get nodes --name "${CLUSTER}"); do
  case "$n" in
    *control-plane) mem=$CP_MEM; cpuset=$CP_CPUSET ;;
    *worker2)       mem=$W_MEM;  cpuset=$W2_CPUSET ;;   # match worker2 BEFORE worker
    *worker)        mem=$W_MEM;  cpuset=$W1_CPUSET ;;
    *)              continue ;;
  esac
  echo "   $n -> --memory=$mem --cpuset-cpus=$cpuset"
  docker update --memory "$mem" --memory-swap "$mem" --cpuset-cpus "$cpuset" "$n" >/dev/null
done

echo "==> Waiting for nodes Ready"
kubectl --context "kind-${CLUSTER}" wait --for=condition=Ready nodes --all --timeout=120s

echo "==> VERIFY the kubeadm tuning actually applied (v1beta3 map extraArgs)"
echo "--- relaxed leader-election on scheduler (expect lease/renew/retry, NOT just leader-elect=true) ---"
kubectl --context "kind-${CLUSTER}" -n kube-system get pod "kube-scheduler-${CLUSTER}-control-plane" \
  -o jsonpath='{range .spec.containers[0].command[*]}{@}{"\n"}{end}' | grep -i 'leader-elect' || true
echo "--- etcd tuning (expect quota-backend-bytes + auto-compaction) ---"
kubectl --context "kind-${CLUSTER}" -n kube-system get pod "etcd-${CLUSTER}-control-plane" \
  -o jsonpath='{range .spec.containers[0].command[*]}{@}{"\n"}{end}' | grep -iE 'data-dir|quota-backend|auto-compaction|snapshot-count' || true
echo "--- nodes + control-plane taint + per-node caps ---"
kubectl --context "kind-${CLUSTER}" get nodes -o wide
kubectl --context "kind-${CLUSTER}" get node "${CLUSTER}-control-plane" -o jsonpath='taints={.spec.taints}{"\n"}'
for n in $(kind get nodes --name "${CLUSTER}"); do
  printf '   %s: ' "$n"; docker inspect -f 'mem={{.HostConfig.Memory}} cpuset={{.HostConfig.CpusetCpus}}' "$n"; echo
done

cat <<EOF

==> NEXT (companion steps the kind config cannot do):
  1. Install UXP/Crossplane with the default "*" MRAP DISABLED
     (Helm value provider.defaultActivations: []), then: hack/apply-mgmt-mrap.sh
  2. RELAX CROSSPLANE-CORE leader-election (this file relaxes only the kubeadm
     scheduler/controller-manager; crossplane-core runs its own LE ~15s/10s).
     Set LEADER_ELECTION_* env / --leader-election-* on the crossplane-core Deployment (~60s/45s/10s).
  3. Install the ctp Configurations + provider creds, then provision CPs.
  Teardown: kind delete cluster --name ${CLUSTER}  (--name MANDATORY; bare delete no-ops)
  then: docker volume prune -f
EOF
