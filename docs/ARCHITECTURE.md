# Architecture & Test 1 topology

> An interactive hand-drawn version is available on Excalidraw:
> https://excalidraw.com/#json=MVPcCcUfrrb6dA3AoceMy,8o0baemlCNXXUZmhs1k8-A
> (open it and use *Export image* to save PNG/SVG into `docs/assets/`).

## Topology (Test 1: 2× AWS)

```mermaid
flowchart TB
  mgmt["Management plane<br/>kind + UXP + configuration-aws-ctp"]
  mgmt -->|provisions| cpa
  mgmt -->|provisions| cpb
  subgraph set["Resilience set (each runs resilient-ctp + aws-s3)"]
    direction LR
    cpa["Control Plane A — EKS us-east-1<br/>priority 1 · LEADER<br/>managementPolicies = ['*']"]
    cpb["Control Plane B — EKS us-west-2<br/>priority 2 · STANDBY<br/>managementPolicies = ['Observe']"]
    cpa <-->|"heartbeat ledger<br/>(SSM tag, epoch seconds)"| cpb
  end
  cpa -->|"manages ['*']"| bkt[("Shared S3 bucket<br/>same external-name")]
  cpb -.->|observes| bkt
```

## Leadership decision (AND rule)

A control plane holds `['*']` **iff**: GSLB-healthy **and** its own heartbeat is
fresh **and** no higher-priority peer is alive (readable + fresh). Any ambiguity
→ `['Observe']` (never fail open).

## Failover & failback sequence

```mermaid
sequenceDiagram
  participant A as CP-A (leader, pri 1)
  participant B as CP-B (standby, pri 2)
  participant S3 as Shared S3 bucket
  Note over A,B: Steady state
  A->>S3: manage ['*']
  B-->>S3: observe ['Observe']
  Note over A: Outage — heartbeat stops updating
  B->>B: peer A heartbeat age > TTL, then hysteresis window
  B->>S3: promote → manage ['*'] (takes over same bucket)
  Note over A: Recovers — resumes heartbeating
  B->>B: sees A fresh (higher priority) → two-phase handoff
  B-->>S3: demote → observe
  A->>S3: reclaim → manage ['*']
```

See [`SPEC.md`](./SPEC.md) §14 for the validated Test 1 results and operational
learnings (provider observe-poll vs freshness-TTL, `providerConfigRef.kind`,
fail-safe extra-resources fetch).
