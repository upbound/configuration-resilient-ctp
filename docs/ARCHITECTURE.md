# Architecture & Test 1 topology

> An interactive hand-drawn version is available on Excalidraw:
> https://excalidraw.com/#json=neYLJYL1foN4_Mw4GwxNG,L4z_l6trtnmq-a78qkEa9g
> (open it and use *Export image* to save PNG/SVG into `docs/assets/`).

## Topology (Test 1: 2× AWS)

```mermaid
flowchart TB
  mgmt["Management plane<br/>kind + UXP + configuration-aws-ctp"]
  mgmt -->|provisions| cpa
  mgmt -->|provisions| cpb
  subgraph set["Resilience set (each runs resilient-ctp + aws-s3)"]
    direction LR
    cpa["Control Plane A — EKS us-east-1<br/>priority 1 · LEADER<br/>honors each MR's intended policy<br/>(e.g. [Create,Update,Observe])"]
    cpb["Control Plane B — EKS us-west-2<br/>priority 2 · STANDBY<br/>reduced to ['Observe']"]
    cpa <-->|"heartbeat ledger<br/>(SSM tag, epoch seconds)"| cpb
  end
  cpa -->|"manages (intended policy)"| bkt[("Shared S3 bucket<br/>same external-name")]
  cpb -.->|observes| bkt
```

## Leadership decision (AND rule)

A control plane is the **leader iff**: GSLB-healthy **and** its own heartbeat is
fresh **and** no higher-priority peer is alive (readable + fresh). Any ambiguity
→ treated as **not leader** (never fail open). resilient-ctp decides *leadership*;
the effective write-scope is applied by `function-management-policies`: on the
leader each managed resource keeps its **intended** `managementPolicies`; on
standbys they are reduced to `['Observe']`.

## Failover & failback sequence

```mermaid
sequenceDiagram
  participant A as CP-A (leader, pri 1)
  participant B as CP-B (standby, pri 2)
  participant S3 as Shared S3 bucket
  Note over A,B: Steady state
  A->>S3: manage (intended policy, e.g. [Create,Update,Observe])
  B-->>S3: observe ['Observe']
  Note over A: Outage — heartbeat stops updating
  B->>B: peer A heartbeat age > TTL, then hysteresis window
  B->>S3: promote → manage (intended policy) (takes over same bucket)
  Note over A: Recovers — resumes heartbeating
  B->>B: sees A fresh (higher priority) → two-phase handoff
  B-->>S3: demote → observe
  A->>S3: reclaim → manage (intended policy)
```

See [`SPEC.md`](./SPEC.md) §14 for the validated Test 1 results and operational
learnings (provider observe-poll vs freshness-TTL, `providerConfigRef.kind`,
fail-safe extra-resources fetch).
