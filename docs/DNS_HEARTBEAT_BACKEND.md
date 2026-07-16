# DNS-TXT Heartbeat Backend — Design Spec

Status: **Proposal / design** (reference implementation prototyped; not yet
live-validated). Companion to [`SPEC.md`](./SPEC.md) §5 (the heartbeat ledger).

## 1. Motivation

The default heartbeat backend (`SPEC.md` §5.1) writes each control plane's
last-reconcile timestamp to a **per-cloud resource** (AWS SSM `Parameter` tag,
Azure Resource Group tag, GCP label, …). That works but has two costs in a
multi-cloud resilience set:

1. **Per-cloud implementation.** Every cloud needs its own "cheapest taggable
   resource" builder and a verified way to read the tag back from
   `status.atProvider`. Today only AWS is implemented.
2. **Per-cloud credentials.** Each control plane needs working provider
   credentials for the cloud its heartbeat resource lives in.

A **DNS TXT record** ledger removes both: DNS is a single, cloud-neutral
namespace that every control plane can already reach, and it needs **no
cloud-provider credentials** in the resilience layer. This makes it the natural
fit for AWS + Azure (+ GCP) sets where we do not want to implement and
credential a heartbeat resource per cloud.

## 2. Goal

Add a **mutually exclusive**, opt-in heartbeat backend selected by
`spec.heartbeat.backend: dns` that is **cloud-agnostic** (works regardless of
where the zone is hosted) and **cloud-credential-agnostic** (the resilience
layer holds no AWS/Azure/GCP credentials for the heartbeat). The default
(`cloudResource`) is unchanged.

## 3. Why external-dns (not a Crossplane DNS provider)

- There is **no official `provider-dns`** on the Upbound Marketplace, and no
  `crossplane-contrib/provider-dns` repo. (Verified 2026-07: the Marketplace
  page 404s.)
- The only Crossplane RFC2136 option is the **community**, GHCR-hosted,
  cluster-scoped [`dana-team/provider-dns-v2`](https://github.com/dana-team/provider-dns-v2)
  — not official, and it would still require a TSIG credential.
- [external-dns](https://github.com/kubernetes-sigs/external-dns) is
  **cloud-agnostic by construction** (Route53, Azure DNS, Cloud DNS, Cloudflare,
  RFC2136, … — 30+ providers), owns its own DNS-backend credentials, and is
  **already deployed by the optional k8gb install** (`SPEC.md` §4.1). Writing a
  record is a plain `DNSEndpoint` CR; reading is plain DNS resolution.

Trade-off accepted: external-dns must be present and configured on each control
plane (the k8gb path already assumes this), and it — not the RCP — holds the DNS
credentials. That is exactly the "credential-agnostic in the resilience layer"
property we want.

## 4. Design

### 4.1 Config surface (XRD)

```yaml
spec:
  heartbeat:
    backend: dns            # 'cloudResource' (default) | 'dns' — mutually exclusive
    dns:
      zone: cloud.example.com               # zone the per-CP TXT records live under
      ttlSeconds: 30                        # TXT record TTL
      kubernetesProviderConfigName: default # provider-kubernetes PC used to apply the DNSEndpoint
    freshnessTTLSeconds: 180                # shared with cloudResource
    writeThrottleSeconds: 60                # shared with cloudResource
```

### 4.2 Record

- **FQDN:** `recon-heartbeat-<id>.<zone>` — same `recon-heartbeat-<id>`
  convention as the cloud-resource external-name, so every peer reconstructs it
  purely from the member id + shared zone.
- **Type:** `TXT`.
- **Value:** `ts=<epoch>;role=<role>;cp=<id>` — Unix epoch seconds (portable,
  comparable), plus the decided role and cp-id for cross-checking.

### 4.3 Write path

Each control plane publishes **its own** heartbeat by composing an external-dns
`DNSEndpoint` (`externaldns.k8s.io/v1alpha1`) carrying the TXT record. Because a
`DNSEndpoint` is a plain CRD (not a Crossplane MR), it is applied via a
namespaced **provider-kubernetes `Object`** (`kubernetes.m.crossplane.io/v1alpha1`,
`managementPolicies: ["*"]`). external-dns then reconciles the record into the
zone using whatever DNS backend it is configured for.

```
resilient-ctp fn ── composes ─▶ Object (provider-kubernetes)
                                   └─ manifest: DNSEndpoint (TXT recon-heartbeat-<id>.<zone>)
                                        └─ external-dns ──▶ Route53 / Azure DNS / RFC2136 / …
```

Write throttling reuses the previous epoch (from prior XR
`status.selfHeartbeatEpoch`) within `writeThrottleSeconds` so external-dns is
not churned every reconcile.

### 4.4 Read path

Peers are read by **live DNS resolution** of each peer's TXT record (no
Crossplane resource — the peers are on other clusters). Resolution is isolated
in a single `resolve_txt(fqdn)` helper (`dnspython`) so the parsing/liveness
logic stays pure and unit-testable. There are **no peer Observe resources** in
this backend.

### 4.5 Liveness / safety (identical to cloudResource)

- `readable` = a TXT with a `ts` field was resolved; `fresh` = `readable AND
  epoch > 0 AND now-epoch ≤ freshnessTTLSeconds`.
- **Fail-safe:** resolution failure (NXDOMAIN, timeout, `dnspython` missing) →
  `readable = False` → not fresh → treated exactly like an unreadable/stale
  cloud-resource read. The AND rule (`SPEC.md` §6) never fails open.

## 5. Prerequisites (per control plane)

1. **external-dns** running and authoritative for `<zone>`, watching
   `DNSEndpoint` CRs (`--source=crd`), `--policy=upsert-only`, distinct
   `--txt-owner-id` per CP, and a `--txt-prefix` so external-dns's ownership TXT
   does not collide with the heartbeat data TXT.
2. **provider-kubernetes** installed with a `ProviderConfig`
   (`credentials.source: InjectedIdentity` is sufficient — it applies the
   `DNSEndpoint` to the local cluster).
3. A **publicly delegated zone** (so the function pods can resolve peer TXT
   records via normal recursion).

## 6. cloudResource vs dns

| | `cloudResource` (default) | `dns` |
|---|---|---|
| Write | per-CP cloud resource tag (AWS SSM `Parameter`, …) | TXT via external-dns `DNSEndpoint` (provider-kubernetes `Object`) |
| Read | Observe MR → `status.atProvider.tags` | live DNS TXT resolution (`dnspython`) |
| Creds in RCP | cloud-provider creds | **none** (external-dns owns DNS creds) |
| Per-cloud code | yes (one builder per cloud) | **no** (one path, any cloud) |
| Extra deps | provider per cloud | provider-kubernetes + external-dns + `dnspython` |
| Best for | single-cloud sets | multi-cloud / credential-agnostic sets |

## 7. Failure modes

- **external-dns down / misconfigured:** the CP's own record goes stale →
  peers see it as down. Same failure semantics as a provider that cannot write
  the cloud resource. Not split-brain-inducing on its own (AND with GSLB +
  priority).
- **Zone not delegated / resolver blocked from function pods:** peers appear
  unreadable → fail-safe standby. Detectable in `status.peers[].readable`.
- **Two external-dns in one zone:** must use distinct `--txt-owner-id` +
  `--policy=upsert-only` so neither deletes the other's record.
- **Propagation vs TTL:** `ttlSeconds` (record) + external-dns sync interval
  must be comfortably below `freshnessTTLSeconds` to avoid false staleness.

## 8. Testing plan

- **Unit (pure):** payload round-trip (`encode`/`parse`), FQDN convention,
  `read_peer_dns` fresh/stale/unreadable/exact-match, `DNSEndpoint` shape.
- **Composition (offline, CI):** render `backend: dns` and assert the composed
  provider-kubernetes `Object` wraps a `DNSEndpoint` with the correct FQDN and
  `recordType: TXT` (write path).
- **Live e2e (deferred):** external-dns + a delegated zone on 2 CPs; verify each
  publishes and resolves the peer's TXT; then steady → failover (pause leader,
  its TXT ages past TTL) → failback. The existing harness asserts on
  `status.role`, which is backend-independent, so it applies with minimal
  change.

## 9. Alternatives considered

- **`dana-team/provider-dns-v2` (RFC2136):** self-contained Crossplane MR
  pattern, but non-official, GHCR-hosted, cluster-scoped, and still needs a
  TSIG credential. Rejected for the default DNS path; viable if a
  Crossplane-MR-only implementation is later desired.
- **Cloud-specific DNS providers (Route53/Azure-DNS/Cloud-DNS `Record` MRs):**
  reuse providers we already have, but are **not** cloud-agnostic (defeats the
  purpose).
- **RFC2136 + BIND/CoreDNS directly (no external-dns):** the most
  infrastructure-agnostic, but the heaviest to stand up for a demo. A good
  future option for environments without external-dns.

## 10. Open questions / future

- Fully self-hosted RFC2136 path for air-gapped / no-external-dns environments.
- Shared-zone record ownership hardening (owner-id + prefix conventions) as a
  documented default.
- Optional signing of the TXT payload if the zone is not trusted end-to-end.

## 11. Reference implementation

A working prototype exists on branch `feat/dns-heartbeat-backend`
(`functions/resilientcontrolplane/dns_heartbeat.py`, XRD `spec.heartbeat.backend`,
`main.py` backend switch, composition + unit tests). This doc is the design of
record and is intentionally independent of that branch's merge status.
