"""DNS-TXT heartbeat backend (cloud- and cloud-credential-agnostic).

Alternative to the cloud-resource (SSM Parameter) backend, selected by
``spec.heartbeat.backend: dns``. Each control plane:

- WRITES its own heartbeat as a TXT record ``recon-heartbeat-<id>.<zone>``
  published by external-dns. The DNSEndpoint is applied through a
  provider-kubernetes ``Object`` (external-dns owns the DNS backend
  credentials, so no cloud-provider credentials are used here); and
- READS every peer's heartbeat by resolving that peer's TXT record live
  (plain DNS query, no credentials, cloud-agnostic).

The record value is ``ts=<epoch>;role=<role>;cp=<id>`` (see
``prelude.encode_dns_payload``). Live resolution is isolated in
``resolve_txt`` so the parsing/liveness logic (``read_peer_dns``) stays pure
and unit-testable.
"""

from .heartbeat import PeerLiveness
from .prelude import (
    SELF_HB,
    as_int,
    encode_dns_payload,
    heartbeat_external_name,
    heartbeat_fqdn,
    parse_dns_payload,
)


def build_dnsendpoint(cp_id: str, namespace: str, zone: str, ttl: int,
                      epoch: int, role: str) -> dict:
    """The external-dns DNSEndpoint carrying this CP's heartbeat TXT record."""
    return {
        "apiVersion": "externaldns.k8s.io/v1alpha1",
        "kind": "DNSEndpoint",
        "metadata": {
            "name": heartbeat_external_name(cp_id),
            "namespace": namespace,
        },
        "spec": {
            "endpoints": [
                {
                    "dnsName": heartbeat_fqdn(cp_id, zone),
                    "recordType": "TXT",
                    "recordTTL": int(ttl),
                    "targets": [encode_dns_payload(epoch, role, cp_id)],
                }
            ]
        },
    }


def build_own_object(identity: dict, namespace: str, zone: str, ttl: int,
                     k8s_provider_config: str, epoch: int,
                     role: str) -> tuple[str, dict]:
    """Build this control plane's own (writable) heartbeat as a
    provider-kubernetes ``Object`` wrapping the external-dns DNSEndpoint."""
    manifest = build_dnsendpoint(identity["id"], namespace, zone, ttl, epoch,
                                 role)
    obj = {
        "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
        "kind": "Object",
        "metadata": {
            "name": SELF_HB,
            "namespace": namespace,
            "annotations": {"crossplane.io/composition-resource-name": SELF_HB},
        },
        "spec": {
            "managementPolicies": ["*"],
            "forProvider": {"manifest": manifest},
            "providerConfigRef": {
                "name": k8s_provider_config,
                "kind": "ProviderConfig",
            },
        },
    }
    return SELF_HB, obj


def resolve_txt(fqdn: str, timeout: float = 3.0) -> list:
    """Resolve a TXT record, returning its string values (``[]`` on any
    failure: NXDOMAIN, timeout, or dnspython missing). The ONLY impure part of
    this module; kept tiny so the rest stays unit-testable. Failure is treated
    as "unreadable" by ``read_peer_dns`` (fail-safe: never fail open)."""
    try:
        import dns.resolver
    except Exception:
        return []
    try:
        resolver = dns.resolver.Resolver()
        resolver.timeout = timeout
        resolver.lifetime = timeout
        answers = resolver.resolve(fqdn, "TXT")
    except Exception:
        return []
    values = []
    for rdata in answers:
        chunks = getattr(rdata, "strings", [])
        values.append(
            "".join(c.decode() if isinstance(c, bytes) else str(c)
                    for c in chunks)
        )
    return values


def read_peer_dns(member: dict, txt_records: list, now: int,
                  ttl: int) -> PeerLiveness:
    """Derive a peer's liveness from the TXT record values resolved for it.
    Pure: given the record strings, no I/O. Mirrors ``heartbeat.read_peer``
    semantics (unreadable/stale => not fresh)."""
    cp_id = member["id"]
    epoch, role, readable = 0, "", False
    for txt in txt_records:
        payload = parse_dns_payload(txt)
        if "ts" in payload:
            epoch = as_int(payload.get("ts"), 0)
            role = payload.get("role", "")
            readable = True
            if payload.get("cp") == cp_id:
                break  # exact match for this peer wins over any stray record
    age = now - epoch if epoch else 10 ** 9
    fresh = readable and epoch > 0 and age <= ttl
    return PeerLiveness(
        cp_id=cp_id,
        priority=int(member["priority"]),
        geo_tag=member.get("geoTag", ""),
        epoch=epoch,
        age_seconds=age,
        fresh=fresh,
        readable=readable,
        role=role,
    )
