"""Shared helpers for the resilience composition function.

Pure, side-effect-light utilities used across the other modules: time,
naming conventions, and small dict accessors. Kept dependency-free so it can be
unit-reasoned about in isolation.
"""

from datetime import datetime, timezone

# Tag/label keys written onto the heartbeat resource. The timestamp key is the
# public convention documented in docs/SPEC.md; the others coordinate election.
# Tag/label KEYS must be portable across AWS, Azure and GCP. Azure tag names
# forbid '/ < > % & \\ ?' and GCP label keys allow only lowercase
# [a-z0-9_-] starting with a letter — so no slashes, dots, or uppercase. Keep
# every key to lowercase letters, digits and hyphens.
TS_TAG_DEFAULT = "last-reconciliation-timestamp-utc"
ROLE_TAG = "resilient-role"
CPID_TAG = "resilient-cp-id"

# Composition-resource-name for this control plane's own heartbeat.
SELF_HB = "heartbeat-self"


def now_epoch() -> int:
    """Current UTC time as integer Unix seconds (the portable heartbeat value:
    valid as an AWS/Azure/Alibaba tag AND a GCP label, unlike an ISO string
    which contains colons GCP labels forbid)."""
    return int(datetime.now(timezone.utc).timestamp())


def iso_now() -> str:
    """Current UTC time as an RFC3339 string, for human-facing status fields."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def heartbeat_external_name(cp_id: str) -> str:
    """Deterministic external-name of a control plane's heartbeat resource,
    derived purely from its id so every peer can reconstruct it."""
    return f"recon-heartbeat-{cp_id}"


def peer_hb_resource_name(cp_id: str) -> str:
    """Composition-resource-name used for a peer's observed heartbeat MR."""
    return f"heartbeat-peer-{cp_id}"


def as_int(value, default: int = 0) -> int:
    """Best-effort int() that never raises (heartbeat tags are strings)."""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default
