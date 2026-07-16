"""Shared helpers for the resilience composition function.

Pure, side-effect-light utilities used across the other modules: time,
naming conventions, and small dict accessors. Kept dependency-free so it can be
unit-reasoned about in isolation.
"""

from datetime import datetime, timezone

# Tag/label keys written onto the heartbeat resource. The timestamp key is the
# public convention documented in docs/SPEC.md; the others coordinate election.
TS_TAG_DEFAULT = "last-reconciliation-timestamp-utc"
ROLE_TAG = "resilient.crossplane.io/role"
CPID_TAG = "resilient.crossplane.io/cp-id"

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


def heartbeat_fqdn(cp_id: str, zone: str) -> str:
    """FQDN of a control plane's heartbeat TXT record (DNS backend). Reuses the
    same ``recon-heartbeat-<id>`` convention as the cloud-resource external-name
    so every peer can reconstruct it purely from the id and the shared zone."""
    return f"{heartbeat_external_name(cp_id)}.{zone.rstrip('.')}"


def encode_dns_payload(epoch: int, role: str, cp_id: str) -> str:
    """Encode the heartbeat payload carried in the TXT record value. Compact
    ``k=v;k=v`` so it is a single, human-readable TXT string."""
    return f"ts={int(epoch)};role={role};cp={cp_id}"


def parse_dns_payload(txt: str) -> dict:
    """Parse a TXT record value produced by ``encode_dns_payload``. Tolerant of
    extra/missing fields; returns a dict (never raises)."""
    out = {}
    for part in str(txt).split(";"):
        if "=" in part:
            key, value = part.split("=", 1)
            out[key.strip()] = value.strip()
    return out


def as_int(value, default: int = 0) -> int:
    """Best-effort int() that never raises (heartbeat tags are strings)."""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default
