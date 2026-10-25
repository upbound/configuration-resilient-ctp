"""Replays the reconcile right after ctp-test-1-b promoted (2026-10-25 23:26:20Z)."""
from types import SimpleNamespace
from google.protobuf import struct_pb2
from crossplane.function import resource
from function import election

now = 1792970780
# The XR status as the function receives it: through a protobuf Struct.
struct = struct_pb2.Struct()
struct.update({"status": {"role": "leader", "selfHeartbeatEpoch": now,
                          "promotionCandidateSince": "",
                          "lastHandoffTime": "2026-10-25T23:26:20Z"}})
prior_status = resource.struct_to_dict(struct)["status"]

higher_peer = SimpleNamespace(cp_id="ctp-test-1-a", priority=1, readable=True,
                              fresh=False, role="leader", age_seconds=2340)
gslb = SimpleNamespace(found=True, healthy=True, active=True)
decision = election.decide(
    identity={"id": "ctp-test-1-b", "priority": 2}, gslb=gslb,
    peers=[higher_peer], prior_status=prior_status, hysteresis_periods=3,
    write_throttle=60, now=now + 1, strategy="failover")
print(f"selfHeartbeatEpoch type={type(prior_status['selfHeartbeatEpoch']).__name__} "
      f"-> role={decision.role} reason={decision.reason!r}")
