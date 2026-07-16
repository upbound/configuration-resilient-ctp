"""Unit tests for the DNS heartbeat backend's pure logic (no network).

Run locally:  python -m pytest hack/test_dns_heartbeat.py -q
(These are not part of the `up test` composition suite, which covers the DNS
write path end-to-end; see tests/test-resilience.)

The function package uses relative imports, so we put the ``functions``
directory on sys.path and import ``resilientcontrolplane`` as a PEP 420
namespace package. None of prelude/heartbeat/dns_heartbeat import the Crossplane
SDK at module load, so this works without the function runtime.
"""

import pathlib
import sys

_FUNCTIONS = pathlib.Path(__file__).resolve().parents[1] / "functions"
sys.path.insert(0, str(_FUNCTIONS))

from resilientcontrolplane import dns_heartbeat, prelude  # noqa: E402

MEMBER_A = {"id": "cp-a", "provider": "aws", "region": "us-east-1",
            "geoTag": "us", "priority": 1}


def test_payload_round_trip():
    txt = prelude.encode_dns_payload(1700000000, "leader", "cp-a")
    parsed = prelude.parse_dns_payload(txt)
    assert parsed == {"ts": "1700000000", "role": "leader", "cp": "cp-a"}


def test_parse_payload_is_tolerant():
    assert prelude.parse_dns_payload("garbage") == {}
    # extra/reordered fields do not break parsing
    got = prelude.parse_dns_payload("role=standby;ts=42;cp=x;extra=y")
    assert got["ts"] == "42" and got["role"] == "standby"


def test_fqdn_convention():
    assert prelude.heartbeat_fqdn("cp-a", "cloud.example.com") == \
        "recon-heartbeat-cp-a.cloud.example.com"
    # trailing dot on the zone is normalized away
    assert prelude.heartbeat_fqdn("cp-a", "cloud.example.com.") == \
        "recon-heartbeat-cp-a.cloud.example.com"


def test_read_peer_fresh():
    now = 1700000100
    txt = [prelude.encode_dns_payload(1700000090, "leader", "cp-a")]
    p = dns_heartbeat.read_peer_dns(MEMBER_A, txt, now=now, ttl=180)
    assert p.readable and p.fresh and p.epoch == 1700000090
    assert p.role == "leader" and p.age_seconds == 10


def test_read_peer_stale():
    now = 1700000100
    txt = [prelude.encode_dns_payload(1699990000, "leader", "cp-a")]  # old
    p = dns_heartbeat.read_peer_dns(MEMBER_A, txt, now=now, ttl=180)
    assert p.readable and not p.fresh  # readable but too old


def test_read_peer_unresolvable():
    # No records (NXDOMAIN / resolution failure) -> unreadable, not fresh.
    p = dns_heartbeat.read_peer_dns(MEMBER_A, [], now=1700000100, ttl=180)
    assert not p.readable and not p.fresh and p.epoch == 0


def test_read_peer_prefers_exact_cp_match():
    now = 1700000100
    txt = [
        prelude.encode_dns_payload(1700000000, "standby", "other"),
        prelude.encode_dns_payload(1700000095, "leader", "cp-a"),
    ]
    p = dns_heartbeat.read_peer_dns(MEMBER_A, txt, now=now, ttl=180)
    assert p.epoch == 1700000095 and p.role == "leader"


def test_build_own_object_shape():
    name, obj = dns_heartbeat.build_own_object(
        MEMBER_A, namespace="default", zone="cloud.example.com", ttl=30,
        k8s_provider_config="default", epoch=1700000000, role="leader")
    assert name == "heartbeat-self"
    assert obj["apiVersion"] == "kubernetes.m.crossplane.io/v1alpha1"
    assert obj["spec"]["managementPolicies"] == ["*"]
    manifest = obj["spec"]["forProvider"]["manifest"]
    assert manifest["apiVersion"] == "externaldns.k8s.io/v1alpha1"
    ep = manifest["spec"]["endpoints"][0]
    assert ep["dnsName"] == "recon-heartbeat-cp-a.cloud.example.com"
    assert ep["recordType"] == "TXT" and ep["recordTTL"] == 30
    assert "role=leader" in ep["targets"][0]
