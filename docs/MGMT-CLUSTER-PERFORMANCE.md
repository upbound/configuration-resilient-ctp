# `resilient-mgmt` kind cluster — performance bottleneck & remediation

## Symptom

The `resilient-mgmt` kind cluster (Crossplane + the `provider-family-{aws,azure,gcp}`
families installed to provision EKS/AKS/GKE control planes) repeatedly degrades:

- `kube-scheduler`, `kube-controller-manager`, and `crossplane` (core + apollo) go
  into **CrashLoopBackOff** with hundreds of restarts.
- The crash is always the same:
  ```
  crossplane: error: cannot start controller manager: leader election lost
  Failed to update lease ... leases/crossplane-leader-election-core?timeout=25s:
    context deadline exceeded
  ```
- `kubectl` against the cluster intermittently times out (TLS handshake timeout,
  `context deadline exceeded`), and Crossplane cannot reconcile XRs (e.g. a GKE
  `ControlPlane` sat 90 min with zero managed resources created).

## Root cause: etcd fsync latency, **not** RAM or CPU

Measured on the affected host: **~10 GB of 36 GB RAM used, 14 CPU cores, Docker VM
= 26 GB** — compute was almost entirely idle. Compute is not the constraint.

The constraint is **etcd's write-path durability latency, amplified by write
volume**:

1. **etcd logs** show slow *writes*, not just reads:
   ```
   apply request took too long ... key:"/registry/leases/kube-system/kube-controller-manager" ... took:"283ms"
   apply request took too long ... read-only range ... took:"500ms+"
   ```
   A lease `PUT` is tiny; 283 ms means the backend commit / WAL fsync is slow.
2. **Every leader-election lease renewal is an etcd write that must `fsync`** to
   durable storage, every few seconds. When fsync is slow and the serialized
   write queue backs up, renewals miss their 25 s deadline → the component loses
   its lease → CrashLoopBackOff. That is exactly the crash above.

### Why a fast Mac still hits this

- **Virtualized disk fsync.** etcd calls `fsync` on its WAL for *every* write to
  guarantee durability. On Docker Desktop for Mac, container disk writes traverse
  the virtualization layer (the Linux VM's virtual block device backed by a file
  on APFS). fsync latency there is **tens to hundreds of ms**, vs. <1 ms on native
  disk. Idle RAM/CPU cannot make an fsync faster.
- **Write volume from ~500 CRDs.** Three full provider families install ~500 CRDs.
  Each spawns controllers (provider pods, Crossplane, apollo) that renew leases and
  write status/events continuously → a flood of serialized, fsync-bound writes.
  Slow fsync × high volume = etcd cannot keep leases alive.

**Bottleneck = etcd fsync latency (virtualized disk) × write volume (~500 CRDs).**
RAM and CPU are bystanders.

### How to confirm it yourself

```sh
# 1. Resource headroom (rule out RAM/CPU):
kubectl --context kind-resilient-mgmt describe node | grep -A3 'Allocated resources'
docker info --format '{{.MemTotal}}'

# 2. etcd slow writes (the smoking gun):
kubectl --context kind-resilient-mgmt -n kube-system logs \
  etcd-resilient-mgmt-control-plane --tail=200 | grep 'took too long'

# 3. The crash reason (leader-election lost on lease renew):
kubectl --context kind-resilient-mgmt -n crossplane-system logs \
  deploy/crossplane --previous | grep -i 'leader election'
```

## Remediation

### 1. Put etcd on tmpfs (RAM) — the direct fix

`/tmp` inside a kind node is **tmpfs** (RAM, sized ~50% of the Docker VM RAM;
`/dev/shm` is only 64 MB so is not usable). Pointing etcd's `dataDir` at `/tmp/etcd`
makes fsync memory-speed and eliminates the bottleneck. This is baked into
[`hack/resilient-mgmt-kind.yaml`](../hack/resilient-mgmt-kind.yaml):

```sh
kind create cluster --config hack/resilient-mgmt-kind.yaml
```

> ⚠️ **Durability tradeoff.** tmpfs is RAM — a kind-node restart or a Docker Desktop
> restart **wipes etcd**, destroying all cluster state (Crossplane installs, XRs,
> ProviderConfigs; the cloud resources persist but Crossplane loses track of them).
> This is fine for a **disposable** test management cluster recreated from scratch —
> its intended role here. If you need state to survive a restart, delete the
> `etcd.local.dataDir: /tmp/etcd` line (etcd falls back to on-disk `/var/lib/etcd`)
> and rely on the tuning + leader-election relaxation below, which alone stops the
> crashloop while staying durable — just slower.

### 2. Cut the write volume — trim provider families (durable, recommended)

The families install everything; the tests use a fraction. Installing only the
**service** providers actually referenced drops the CRD/controller count several-
fold, which lowers etcd write pressure regardless of disk speed. Roughly:

- **AWS:** `provider-aws-ssm` (heartbeat), `provider-aws-s3` (shared bucket),
  `provider-aws-eks`/`ec2`/`iam` (EKS control-plane provisioning).
- **Azure:** `provider-azure-containerservice`/`network`/`resources`.
- **GCP:** `provider-gcp-container`/`compute`/`storage`/`cloudplatform`.

(The `configuration-*-ctp` packages may pull families transitively; pin the
specific service providers where possible.)

### 3. Docker Desktop VirtioFS + Apple Virtualization framework

Docker Desktop → Settings → General → enable **VirtioFS** and the **Apple
Virtualization framework**. Materially lower fsync latency than the older
gRPC-FUSE / QEMU backend. Free, and helps every container.

### 4. Relaxed leader-election (band-aid, already in the manifest)

`kube-scheduler` / `kube-controller-manager` lease durations are raised
(60s/45s/10s vs. default 15s/10s/2s) so transient fsync spikes don't crashloop
them. Crossplane core's leader-election is separate — if it still flaps on a
durable-disk setup, raise its `--leader-election-*` flags (or `LEADER_ELECTION_*`
env) on the Crossplane deployment similarly.

## Summary

| Lever | Fixes | Durable? | Effort |
|---|---|---|---|
| etcd on tmpfs (`/tmp/etcd`) | fsync latency | No (RAM, lost on restart) | ✅ in manifest |
| Trim provider families | write volume | Yes | Medium (change installs) |
| VirtioFS backend | fsync latency | Yes | Low (Docker setting) |
| Relaxed leader-election | crashloop symptom | Yes | ✅ in manifest |

**Recommended for the disposable test mgmt cluster:** tmpfs etcd + trimmed
providers + VirtioFS — removes both halves of the bottleneck and leverages the
spare RAM. The Docker VM has ~26 GB; etcd for ~500 CRDs needs 1–2 GB, so tmpfs is
comfortably sized.

## Update (2026-07-24): multi-node + on-disk, reviewed via 8 lenses

`hack/resilient-mgmt-kind.yaml` + `hack/create-mgmt-cluster.sh` now bring up a
**load-ready** cluster on a single Docker Desktop VM (14 CPU / 27 GB). Key
findings from the review that changed the earlier single-node manifest:

- **⚠️ The kubeadm patches must be `v1beta3`, not `v1beta4`.** kind v0.25.0 /
  k8s 1.31.2 generates `v1beta3` (extraArgs is a **map**, not the v1beta4
  name/value **list**). A v1beta4 patch makes kind's strategic merge NULL the
  lists (`scheduler: extraArgs: null`), so the etcd tuning **and the relaxed
  leader-election silently never apply** — the exact crashloop config, believed
  fixed. Verify after create: the scheduler/etcd pods must show
  `--leader-elect-renew-deadline=45s` and `--quota-backend-bytes=…`, not just
  `--leader-elect=true`.
- **etcd: on-disk (durable), NOT tmpfs.** The MRs are the only handles to real
  billed cloud infra (EKS/AKS/GKE); a Docker restart wiping RAM-backed etcd would
  orphan it. On-disk + the tuning + relaxed LE fixes the crashloop without that
  risk, now that the narrow MRAP cut active CRDs to ~116 (¼ of the ~500 that
  saturated fsync). (`/tmp` IS tmpfs in the kind node — verified — so tmpfs
  remains an option for a truly throwaway cluster.)
- **HA multi-member etcd is rejected.** On one VM, 3 members share one disk (one
  failure domain, zero real availability) and TRIPLE the quorum fsync load.
  Single member + mitigations is correct.
- **Real per-node limits are at the Docker layer, not kind/kubelet.** kubelet
  `systemReserved`/`kubeReserved` don't cap host RAM (every node sees the full
  VM → overcommit → host OOM-killer can hit etcd). `create-mgmt-cluster.sh`
  applies `docker update --memory` + **`--cpuset-cpus` (pins the control-plane to
  dedicated cores** so provider churn can't starve apiserver/etcd — the taint
  alone is scheduling-only and gives no CPU/IO isolation on a shared kernel).
- **Companion step the kind config can't do:** relax **crossplane-core**
  leader-election at install (kind only relaxes the kubeadm scheduler/CM).

Topology: 1 tainted control-plane (6 GiB, cores 0-3, single etcd) + 2 workers
(9 GiB each, cores 4-8 / 9-13). Bring up with `hack/create-mgmt-cluster.sh`.
