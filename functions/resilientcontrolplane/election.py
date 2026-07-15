"""Leadership election — the AND rule with two-phase handoff and hysteresis.

Decides whether THIS control plane should hold ``managementPolicies: ["*"]``
(leader) or ``["Observe"]`` (standby), from three inputs:

1. the local GSLB signal (health + activeness),
2. this CP's own liveness (always fresh — we are reconciling), and
3. peers' liveness + advertised role, read from their heartbeat tags.

Core rule (docs/SPEC.md §6): hold ``["*"]`` iff GSLB-healthy AND GSLB-active
(strategy-aware) AND no higher-priority peer is alive. Never fail open: any
ambiguity resolves to ``["Observe"]``.

Safety mechanisms:
- **Two-phase handoff (failback):** a recovering higher-priority CP will not
  seize ``["*"]`` while a lower-priority peer still advertises ``role=leader``;
  the current leader first observes the higher peer alive, demotes to standby,
  and only then does the higher peer promote. Guarantees <=1 leader.
- **Failover hysteresis:** promoting into a gap left by a failed higher-priority
  peer requires the "higher peers all down" condition to persist for
  ``hysteresisPeriods * writeThrottleSeconds`` before the flip, damping flap.

Known limitation (SPEC §6): with per-CP heartbeats a genuine network partition
can still theoretically split-brain; priority + GSLB reduce but do not fully
eliminate it. Test 1 exercises region-death failover, not partition.
"""

from dataclasses import dataclass, field


@dataclass
class Decision:
    is_leader: bool
    role: str                       # "leader" | "standby"
    management_policy: list         # ["*"] | ["Observe"]
    reason: str
    promotion_candidate_since: int  # epoch, or 0 when not a candidate
    last_handoff_time: str          # carried through / updated by caller


def decide(*, identity: dict, gslb, peers: list, prior_status: dict,
           hysteresis_periods: int, write_throttle: int, now: int) -> Decision:
    my_priority = int(identity["priority"])
    higher = [p for p in peers if p.priority < my_priority]
    lower = [p for p in peers if p.priority > my_priority]

    prior_role = prior_status.get("role", "")
    prior_candidate_since = int(prior_status.get("promotionCandidateSince", 0) or 0)
    prior_handoff = prior_status.get("lastHandoffTime", "") or ""

    reasons = []

    # (1)+(2) Am I eligible at all? GSLB must consider me healthy and serving.
    self_eligible = gslb.healthy and gslb.active
    if not self_eligible:
        reasons.append(
            f"GSLB not eligible (healthy={gslb.healthy}, active={gslb.active}"
            + ("" if gslb.found else ", no Gslb found") + ")"
        )

    # (3) Are all higher-priority peers not-alive?
    higher_all_down = all(not p.fresh for p in higher)
    if higher and not higher_all_down:
        alive = [p.cp_id for p in higher if p.fresh]
        reasons.append(f"higher-priority peer(s) alive: {alive}")

    want_leader = self_eligible and higher_all_down
    candidate_since = 0

    if want_leader:
        # Two-phase handoff: if I'm not already the leader and a lower-priority
        # peer still advertises leader, wait for it to release first.
        lower_leader = [p.cp_id for p in lower if p.role == "leader" and p.fresh]
        if lower_leader and prior_role != "leader":
            want_leader = False
            reasons.append(
                f"waiting for lower-priority leader(s) {lower_leader} to release "
                "(two-phase handoff)"
            )
        # Failover hysteresis: only when promoting into a gap left by higher
        # peers (higher set non-empty) and not already leading.
        elif higher and prior_role != "leader":
            window = max(1, hysteresis_periods) * max(1, write_throttle)
            if prior_candidate_since <= 0:
                candidate_since = now
                want_leader = False
                reasons.append("promotion candidate; starting hysteresis")
            else:
                elapsed = now - prior_candidate_since
                if elapsed < window:
                    candidate_since = prior_candidate_since
                    want_leader = False
                    reasons.append(f"hysteresis {elapsed}s/{window}s")
                # else: window satisfied -> promote, clear candidate timer

    role = "leader" if want_leader else "standby"
    mgmt = ["*"] if want_leader else ["Observe"]

    if want_leader and not reasons:
        reasons.append("GSLB healthy+active and no higher-priority peer alive")
    if not want_leader and role == "standby" and not reasons:
        reasons.append("standby")

    # Record a handoff timestamp whenever the role actually changes.
    handoff = prior_handoff
    if prior_role and prior_role != role:
        from .prelude import iso_now
        handoff = iso_now()

    return Decision(
        is_leader=want_leader,
        role=role,
        management_policy=mgmt,
        reason="; ".join(reasons),
        promotion_candidate_since=candidate_since,
        last_handoff_time=handoff,
    )
