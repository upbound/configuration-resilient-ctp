"""Heartbeat ledger.

Each control plane owns exactly ONE lightweight cloud resource (sole-writer, no
contention) carrying a single timestamp tag/label. This control plane:

- WRITES its own heartbeat (managementPolicies ``["*"]``), stamping the current
  epoch plus its decided role, throttled so it only changes when older than
  ``writeThrottleSeconds``; and
- OBSERVES every peer's heartbeat (managementPolicies ``["Observe"]``), reading
  the timestamp tag to judge liveness.

Peer coordinates (provider/region/name) are derived from ``spec.members``.

AWS (SSM Parameter, ``ssm.aws.m.upbound.io/v1beta1``) and Azure (Resource Group,
``azure.m.upbound.io/v1beta1``) are implemented; GCP/Alibaba builders raise
until their phases (SPEC §5.1). The timestamp is read back from
``status.atProvider.tags`` (provider-agnostic in ``read_peer``); confirm the tag
surfaces there for each provider before relying on it live (SPEC §5.1 caveat).
"""

from dataclasses import dataclass

from .prelude import (
    CPID_TAG,
    ROLE_TAG,
    as_int,
    heartbeat_external_name,
    peer_hb_resource_name,
)


@dataclass
class PeerLiveness:
    cp_id: str
    priority: int
    geo_tag: str
    epoch: int          # last-reconcile epoch read from the tag (0 if unknown)
    age_seconds: int    # now - epoch (large if unknown)
    fresh: bool         # age <= ttl AND readable
    readable: bool      # we could read a timestamp tag at all
    role: str           # role tag value ("leader"/"standby"/"") if present


def _aws_parameter(name: str, namespace: str, external_name: str, region: str,
                   provider_config: str, mgmt_policies: list, tags: dict) -> dict:
    """Build an AWS SSM Parameter MR (namespaced v2 provider). The heartbeat
    timestamp lives in ``tags``; ``insecureValue`` is a constant because SSM
    requires a value for a String parameter."""
    return {
        "apiVersion": "ssm.aws.m.upbound.io/v1beta1",
        "kind": "Parameter",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "annotations": {
                "crossplane.io/composition-resource-name": name,
                "crossplane.io/external-name": external_name,
            },
        },
        "spec": {
            "managementPolicies": mgmt_policies,
            "forProvider": {
                "region": region,
                "type": "String",
                "insecureValue": "resilient-ctp-heartbeat",
                "tags": tags,
            },
            "providerConfigRef": {"name": provider_config, "kind": "ProviderConfig"},
        },
    }


def _azure_resource_group(name: str, namespace: str, external_name: str,
                          location: str, provider_config: str,
                          mgmt_policies: list, tags: dict) -> dict:
    """Build an Azure Resource Group MR (namespaced v2 provider). A Resource
    Group is free at rest and taggable; the heartbeat timestamp lives in
    ``tags`` and is read back from ``status.atProvider.tags`` (same shape as the
    AWS SSM Parameter). ``external-name`` is the Resource Group name so every
    peer can reconstruct it from the cp id."""
    return {
        "apiVersion": "azure.m.upbound.io/v1beta1",
        "kind": "ResourceGroup",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "annotations": {
                "crossplane.io/composition-resource-name": name,
                "crossplane.io/external-name": external_name,
            },
        },
        "spec": {
            "managementPolicies": mgmt_policies,
            "forProvider": {
                "location": location,
                "tags": tags,
            },
            "providerConfigRef": {"name": provider_config, "kind": "ProviderConfig"},
        },
    }


def _gcp_bucket(name: str, namespace: str, external_name: str, location: str,
                project: str, provider_config: str, mgmt_policies: list,
                labels: dict) -> dict:
    """Build a GCP Cloud Storage Bucket MR (namespaced v2 provider). Free at
    rest (empty bucket). GCP uses ``labels`` (not ``tags``), so the heartbeat
    timestamp/role/cp-id live in ``forProvider.labels`` and are read back from
    ``status.atProvider.labels``. The portable tag keys (lowercase, hyphens) are
    already valid GCP label keys."""
    return {
        "apiVersion": "storage.gcp.m.upbound.io/v1beta1",
        "kind": "Bucket",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "annotations": {
                "crossplane.io/composition-resource-name": name,
                "crossplane.io/external-name": external_name,
            },
        },
        "spec": {
            "managementPolicies": mgmt_policies,
            "forProvider": {
                "project": project,
                "location": location,
                "labels": labels,
                "uniformBucketLevelAccess": True,
                "forceDestroy": False,
            },
            "providerConfigRef": {"name": provider_config, "kind": "ProviderConfig"},
        },
    }


def build_parameter(provider: str, name: str, namespace: str, cp_id: str,
                    region: str, provider_config: str, mgmt_policies: list,
                    tags: dict, gcp_project: str = "") -> dict:
    """Provider-dispatched heartbeat resource builder. ``tags`` carries the
    heartbeat key/values; each provider places them where it reads them back
    (AWS/Azure -> tags, GCP -> labels)."""
    external_name = heartbeat_external_name(cp_id)
    if provider == "aws":
        return _aws_parameter(name, namespace, external_name, region,
                              provider_config, mgmt_policies, tags)
    if provider == "azure":
        # region carries the Azure location for azure members.
        return _azure_resource_group(name, namespace, external_name, region,
                                     provider_config, mgmt_policies, tags)
    if provider == "gcp":
        # region carries the GCP location; tags are written as GCP labels.
        return _gcp_bucket(name, namespace, external_name, region, gcp_project,
                           provider_config, mgmt_policies, tags)
    raise NotImplementedError(
        f"heartbeat resource for provider '{provider}' not implemented yet "
        f"(see docs/ROADMAP.md)"
    )


def build_own(identity: dict, namespace: str, provider_config: str,
              ts_tag: str, epoch: int, role: str,
              gcp_project: str = "") -> tuple[str, dict]:
    """Build this control plane's own (writable) heartbeat resource."""
    tags = {
        ts_tag: str(epoch),
        ROLE_TAG: role,
        CPID_TAG: identity["id"],
    }
    from .prelude import SELF_HB
    res = build_parameter(
        provider=identity["provider"],
        name=SELF_HB,
        namespace=namespace,
        cp_id=identity["id"],
        region=identity["region"],
        provider_config=provider_config,
        mgmt_policies=["*"],
        tags=tags,
        gcp_project=gcp_project,
    )
    return SELF_HB, res


def build_peer_observe(member: dict, namespace: str, default_provider_config: str,
                       ts_tag: str, gcp_project: str = "") -> tuple[str, dict]:
    """Build an Observe-only heartbeat resource for a peer, derived from its
    member entry. Tags are not set on an Observe resource (we only read)."""
    name = peer_hb_resource_name(member["id"])
    provider_config = member.get("providerConfigName", default_provider_config)
    res = build_parameter(
        provider=member["provider"],
        name=name,
        namespace=namespace,
        cp_id=member["id"],
        region=member["region"],
        provider_config=provider_config,
        mgmt_policies=["Observe"],
        tags={},
        gcp_project=gcp_project,
    )
    return name, res


def read_peer(member: dict, observed: dict, ts_tag: str, now: int,
              ttl: int) -> PeerLiveness:
    """Read a peer's liveness from its observed heartbeat resource's
    ``status.atProvider.tags``."""
    name = peer_hb_resource_name(member["id"])
    obs = observed.get(name, {})
    at = obs.get("status", {}).get("atProvider", {}) or {}
    # GCP stores the heartbeat in labels; AWS/Azure in tags.
    field = "labels" if member.get("provider") == "gcp" else "tags"
    tags = at.get(field, {}) or {}
    raw_ts = tags.get(ts_tag)
    readable = raw_ts is not None
    epoch = as_int(raw_ts, 0)
    age = now - epoch if epoch else 10 ** 9
    fresh = readable and epoch > 0 and age <= ttl
    return PeerLiveness(
        cp_id=member["id"],
        priority=int(member["priority"]),
        geo_tag=member.get("geoTag", ""),
        epoch=epoch,
        age_seconds=age,
        fresh=fresh,
        readable=readable,
        role=tags.get(ROLE_TAG, "") or "",
    )
