"""XR status assembly.

Builds the ResilientControlPlane status block (docs/SPEC.md §8) from the
decision, GSLB signal, and peer liveness. ``status.managementPolicy`` is the
convention contract that resilience-aware workload packages consume.
"""


def build_status(*, decision, gslb, peers: list, self_epoch: int) -> dict:
    peer_status = []
    for p in peers:
        peer_status.append({
            "id": p.cp_id,
            "ageSeconds": int(p.age_seconds) if p.age_seconds < 10 ** 9 else -1,
            "fresh": bool(p.fresh),
            # No per-peer GSLB attribution yet; report overall GSLB health as a
            # coarse proxy (refined at e2e, SPEC §6).
            "gslbHealthy": bool(gslb.healthy),
            # Definitively down == we could read its heartbeat and it is stale.
            "definitivelyDown": bool(p.readable and not p.fresh),
        })

    # NOTE: we do NOT write status.conditions here. function-auto-ready owns the
    # Ready condition; writing a condition from the function requires a valid
    # lastTransitionTime, and the live apiserver rejects a null one (offline
    # render does not enforce this — the live XRD does).
    status = {
        "role": decision.role,
        "managementPolicy": decision.management_policy,
        "reason": decision.reason,
        "selfHeartbeatEpoch": self_epoch,
        "promotionCandidateSince": (
            str(decision.promotion_candidate_since)
            if decision.promotion_candidate_since else ""
        ),
        "gslb": {
            "healthy": bool(gslb.healthy),
            "isActiveForGeo": bool(gslb.active),
        },
        "peers": peer_status,
    }
    if decision.last_handoff_time:
        status["lastHandoffTime"] = decision.last_handoff_time
    return status
