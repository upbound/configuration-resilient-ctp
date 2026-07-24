"""Leadership election — the AND rule with two-phase handoff and hysteresis.

Decides whether THIS control plane should hold ``managementPolicies: ["*"]``
(leader) or ``["Observe"]`` (standby), from three inputs:

1. the local GSLB signal (health + activeness),
2. this CP's own liveness (always fresh — we are reconciling), and
3. peers' liveness + advertised role, read from their heartbeat tags.

Core rule (docs/SPEC.md §6): hold ``["*"]`` iff GSLB-healthy AND GSLB-active
(strategy-aware) AND no higher-priority peer blocks. Never fail open: any
ambiguity resolves to ``["Observe"]``.

Higher-peer gate (the two-factor tie-breaker). A higher-priority peer blocks
promotion unless we can positively conclude it is not leading:
- **unreadable** -> block (fail-safe: never promote on our own blindness).
- **readable + stale** -> down; does not block.
- **readable + fresh + role==leader** -> block. The peer still advertises
  leadership and its *heartbeat* is the independent partition tie-breaker: in a
  gray failure that partitions only the health-check plane, both geos self-
  compute GSLB-active locally, so the intact heartbeat is what prevents a double
  leader (the AND of the two signals breaks the tie).
- **readable + fresh + role!=leader** -> block UNLESS we are the sole active geo
  under ``failover`` strategy (``gslb_active_failover``); then the higher peer has
  stepped down for an app-health failover and we may promote (this is the #29
  fix). Without that independent single-active arbitration (permissive / no-GSLB
  / roundRobin / geoip) a fresh higher peer always holds a lower one, role-
  agnostic — the original conservative priority+heartbeat rule, unchanged.

Safety mechanisms:
- **Two-phase handoff (failback):** a recovering higher-priority CP will not
  seize ``["*"]`` while a lower-priority peer still advertises ``role=leader``;
  the current leader first observes the higher peer alive, demotes to standby,
  and only then does the higher peer promote. Guarantees <=1 leader.
- **Failover hysteresis:** promoting into a gap left by a stepped-down/failed
  higher-priority peer requires the "higher peers all down" condition to persist
  for ``hysteresisPeriods * writeThrottleSeconds`` before the flip, damping flap.

Known limitation (SPEC §6, §14.3): with per-CP heartbeats a genuine network
partition can still theoretically split-brain if the provider observe-poll lag
exceeds the hysteresis window (``poll < freshnessTTL``/window is required for
correctness); priority + GSLB + the role/freshness tie-breaker reduce but do not
fully eliminate it. Test 1 exercises region-death/app-health failover.
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
           hysteresis_periods: int, write_throttle: int, now: int,
           strategy: str = "failover") -> Decision:
    my_priority = int(identity["priority"])
    higher = [p for p in peers if p.priority < my_priority]
    lower = [p for p in peers if p.priority > my_priority]

    prior_role = prior_status.get("role", "")
    prior_candidate_since = int(prior_status.get("promotionCandidateSince", 0) or 0)
    prior_handoff = prior_status.get("lastHandoffTime", "") or ""

    # C1: distinguish *continuous* leadership from a stale persisted role. A CP
    # that was leader, died and recovered returns with status.role=="leader" but a
    # stale selfHeartbeatEpoch; treating that as "currently leading" would let it
    # skip the handoff/hysteresis gates and double-promote against an interim
    # leader that still holds ["*"]. Persisted role=="leader" is trusted as
    # CURRENT leadership only when this CP's own heartbeat is still fresh. decide()
    # is not passed the raw freshnessTTL, so we bound freshness by the failover
    # damping window: no other CP can have completed a takeover within one window,
    # so a self-heartbeat newer than it proves uninterrupted leadership.
    from .prelude import as_int
    hysteresis_window = max(1, hysteresis_periods) * max(1, write_throttle)
    prior_self_epoch = as_int(prior_status.get("selfHeartbeatEpoch"), 0)
    self_hb_fresh = prior_self_epoch > 0 and (now - prior_self_epoch) <= hysteresis_window
    continuous_leader = prior_role == "leader" and self_hb_fresh

    reasons = []

    # (1)+(2) Am I eligible at all? GSLB must consider me healthy and serving.
    self_eligible = gslb.healthy and gslb.active
    if not self_eligible:
        reasons.append(
            f"GSLB not eligible (healthy={gslb.healthy}, active={gslb.active}"
            + ("" if gslb.found else ", no Gslb found") + ")"
        )

    # (3) Which higher-priority peers block promotion? Single predicate (see the
    # module docstring for the full rule). `gslb_active_failover` is the only
    # thing that relaxes a fresh, stepped-down higher peer — and only in
    # `failover` strategy, where GSLB makes exactly one geo active, so being
    # active means the higher geo is NOT serving. The role==leader check is
    # evaluated FIRST and holds even in that mode: it is the independent
    # heartbeat tie-breaker for a health-check-plane-only partition.
    gslb_active_failover = gslb.found and gslb.active and strategy == "failover"

    def _blocks(peer):
        if not peer.readable:
            # Fail-safe: never promote on our own blindness -- UNLESS we are the
            # sole GSLB-active geo under `failover`. A dead region reads as
            # *unreadable* over directApi but as *stale* over mr; treating the
            # unreadable higher peer as definitively-down here gives both modes
            # the SAME region-death outcome (C2). The partition tie-breaker is
            # untouched: a higher peer we CAN read that is fresh+role=leader still
            # blocks below, so a health-check-plane-only partition still holds.
            return None if gslb_active_failover else "unreadable"
        if not peer.fresh:
            return None                  # readable + stale => down
        if peer.role == "leader":
            return "leader-active"       # still leading => partition tie-breaker
        if gslb_active_failover:
            return None                  # stepped down + GSLB sole-active => #29
        return "alive"                   # permissive: any fresh higher peer holds

    blocking = [(peer, why) for peer in higher if (why := _blocks(peer))]
    higher_all_down = not blocking
    if blocking:
        leading = [p.cp_id for p, why in blocking if why == "leader-active"]
        alive = [p.cp_id for p, why in blocking if why == "alive"]
        unreadable = [p.cp_id for p, why in blocking if why == "unreadable"]
        if leading:
            reasons.append(f"higher-priority leader(s) still active: {leading}")
        if alive:
            reasons.append(f"higher-priority peer(s) alive: {alive}")
        if unreadable:
            reasons.append(
                f"higher-priority peer(s) unreadable, holding standby "
                f"(fail-safe): {unreadable}"
            )

    want_leader = self_eligible and higher_all_down
    candidate_since = 0

    if want_leader:
        # Two-phase handoff: a fresh lower-priority peer still advertising
        # role=leader ALWAYS forces the wait, regardless of prior_role -- a
        # recovered former leader (stale persisted role=="leader") must not seize
        # ["*"] while an interim lower-priority leader still holds it (C1).
        lower_leader = [p.cp_id for p in lower if p.role == "leader" and p.fresh]
        if lower_leader:
            want_leader = False
            reasons.append(
                f"waiting for lower-priority leader(s) {lower_leader} to release "
                "(two-phase handoff)"
            )
        # Failover hysteresis: only when promoting into a gap left by higher
        # peers (higher set non-empty) and NOT already *continuously* leading. A
        # recovered former leader (continuous_leader False) must re-serve the
        # hysteresis before promoting into a higher-peer gap (C1).
        elif higher and not continuous_leader:
            window = hysteresis_window
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
        reasons.append("GSLB healthy+active and no higher-priority peer blocking")
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
