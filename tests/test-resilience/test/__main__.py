"""Generate the resilience CompositionTests and print them as YAML.

Offline tests for the decision engine (docs/ROADMAP.md Phase 0). Freshness is
driven deterministically via ``freshnessTTLSeconds`` extremes because the
function reads real wall-clock time:
  - 999999999 -> any observed heartbeat is "fresh"
  - 1         -> any observed heartbeat is "stale"
The decision is asserted via the own heartbeat's role tag, which mirrors
status.managementPolicy (leader -> ["*"], standby -> ["Observe"]).
"""

import yaml
from models.io.k8s.apimachinery.pkg.apis.meta import v1 as k8s
from models.io.upbound.dev.meta.compositiontest import v1alpha1 as ct

COMPOSITION = "apis/resilientcontrolplane/composition.yaml"
XRD = "apis/resilientcontrolplane/definition.yaml"

MEMBER_A = {"id": "cp-a", "provider": "aws", "region": "us-east-1",
            "geoTag": "us", "priority": 1}
MEMBER_B = {"id": "cp-b", "provider": "aws", "region": "us-west-2",
            "geoTag": "us", "priority": 2}
MEMBER_AZ = {"id": "cp-az", "provider": "azure", "region": "eastus",
             "geoTag": "eu", "priority": 2}
MEMBER_AZ_PRI1 = {"id": "cp-az", "provider": "azure", "region": "eastus",
                  "geoTag": "eu", "priority": 1}
MEMBER_GCP = {"id": "cp-gcp", "provider": "gcp", "region": "asia-southeast1",
              "geoTag": "ap", "priority": 3}


def xr(name, identity, members, *, gslb=None, k8gb=None, heartbeat=None,
       failback=None, status=None):
    spec = {"identity": identity, "members": members,
            "gslb": gslb or {"hostname": "app.cloud.example.com",
                             "strategy": "failover"}}
    if k8gb:
        spec["k8gb"] = k8gb
    if heartbeat:
        spec["heartbeat"] = heartbeat
    if failback:
        spec["failback"] = failback
    obj = {"apiVersion": "resilient.platform.upbound.io/v1alpha1",
           "kind": "ResilientControlPlane",
           "metadata": {"name": name, "namespace": "default"},
           "spec": spec}
    if status:
        obj["status"] = status
    return obj


def peer_hb(cp_id, region, epoch, role):
    """An observed peer heartbeat Parameter (Observe MR)."""
    return {
        "apiVersion": "ssm.aws.m.upbound.io/v1beta1", "kind": "Parameter",
        "metadata": {
            "name": f"recon-heartbeat-{cp_id}", "namespace": "default",
            "annotations": {
                "crossplane.io/composition-resource-name": f"heartbeat-peer-{cp_id}"},
        },
        "spec": {"managementPolicies": ["Observe"],
                 "forProvider": {"region": region}},
        "status": {"atProvider": {"tags": {
            "last-reconciliation-timestamp-utc": str(epoch),
            "resilient-role": role,
            "resilient-cp-id": cp_id}}},
    }


def assert_role(role):
    return {
        "apiVersion": "ssm.aws.m.upbound.io/v1beta1", "kind": "Parameter",
        "metadata": {"annotations": {
            "crossplane.io/composition-resource-name": "heartbeat-self"}},
        "spec": {
            "forProvider": {"tags": {"resilient-role": role}},
            # v2 namespaced MRs require providerConfigRef.kind.
            "providerConfigRef": {"kind": "ProviderConfig"},
        },
    }


def peer_hb_azure(cp_id, location, epoch, role):
    """An observed peer heartbeat Resource Group (Azure Observe MR)."""
    return {
        "apiVersion": "azure.m.upbound.io/v1beta1", "kind": "ResourceGroup",
        "metadata": {
            "name": f"recon-heartbeat-{cp_id}", "namespace": "default",
            "annotations": {
                "crossplane.io/composition-resource-name": f"heartbeat-peer-{cp_id}"},
        },
        "spec": {"managementPolicies": ["Observe"],
                 "forProvider": {"location": location}},
        "status": {"atProvider": {"tags": {
            "last-reconciliation-timestamp-utc": str(epoch),
            "resilient-role": role,
            "resilient-cp-id": cp_id}}},
    }


def assert_role_azure(role):
    """Assert the Azure control plane's own heartbeat Resource Group role."""
    return {
        "apiVersion": "azure.m.upbound.io/v1beta1", "kind": "ResourceGroup",
        "metadata": {"annotations": {
            "crossplane.io/composition-resource-name": "heartbeat-self"}},
        "spec": {
            "forProvider": {"tags": {"resilient-role": role}},
            "providerConfigRef": {"kind": "ProviderConfig"},
        },
    }


def peer_hb_gcp(cp_id, location, epoch, role):
    """An observed peer heartbeat GCS Bucket (GCP Observe MR). GCP carries the
    heartbeat in labels, not tags."""
    return {
        "apiVersion": "storage.gcp.m.upbound.io/v1beta1", "kind": "Bucket",
        "metadata": {
            "name": f"recon-heartbeat-{cp_id}", "namespace": "default",
            "annotations": {
                "crossplane.io/composition-resource-name": f"heartbeat-peer-{cp_id}"},
        },
        "spec": {"managementPolicies": ["Observe"],
                 "forProvider": {"location": location}},
        "status": {"atProvider": {"labels": {
            "last-reconciliation-timestamp-utc": str(epoch),
            "resilient-role": role,
            "resilient-cp-id": cp_id}}},
    }


def assert_role_gcp(role):
    """Assert the GCP control plane's own heartbeat GCS Bucket role (labels)."""
    return {
        "apiVersion": "storage.gcp.m.upbound.io/v1beta1", "kind": "Bucket",
        "metadata": {"annotations": {
            "crossplane.io/composition-resource-name": "heartbeat-self"}},
        "spec": {
            "forProvider": {"labels": {"resilient-role": role}},
            "providerConfigRef": {"kind": "ProviderConfig"},
        },
    }


def gslb_resource(service_health):
    return {
        "apiVersion": "k8gb.absa.oss/v1beta1", "kind": "Gslb",
        "metadata": {"name": "app", "namespace": "default"},
        "status": {"serviceHealth": service_health},
    }


def test(name, the_xr, asserts, *, observed=None, extra=None, context=None):
    spec = ct.Spec(
        compositionPath=COMPOSITION, xrdPath=XRD, timeoutSeconds=60,
        validate=True, xr=the_xr, assertResources=asserts,
    )
    if observed:
        spec.observedResources = observed
    if extra:
        spec.extraResources = extra
    if context:
        spec.context = context
    return ct.CompositionTest(metadata=k8s.ObjectMeta(name=name), spec=spec)


tests = [
    # Single-member set, no peers, no GSLB (degraded) -> leader.
    test("leader-alone",
         xr("cp-a", MEMBER_A, [MEMBER_A]),
         [assert_role("leader")]),

    # Higher-priority peer fresh -> this CP observes.
    test("standby-higher-priority-fresh",
         xr("cp-b", MEMBER_B, [MEMBER_A, MEMBER_B],
            heartbeat={"freshnessTTLSeconds": 999999999}),
         [assert_role("standby")],
         observed=[peer_hb("cp-a", "us-east-1", 1700000000, "leader")]),

    # Stale higher-priority peer but no prior hysteresis -> still standby.
    test("failover-candidate-not-yet-promoted",
         xr("cp-b", MEMBER_B, [MEMBER_A, MEMBER_B],
            heartbeat={"freshnessTTLSeconds": 1, "writeThrottleSeconds": 1},
            failback={"automatic": True, "hysteresisPeriods": 3}),
         [assert_role("standby")],
         observed=[peer_hb("cp-a", "us-east-1", 1700000000, "leader")]),

    # Stale higher-priority peer + prior hysteresis satisfied -> promote.
    test("failover-promote",
         xr("cp-b", MEMBER_B, [MEMBER_A, MEMBER_B],
            heartbeat={"freshnessTTLSeconds": 1, "writeThrottleSeconds": 1},
            failback={"automatic": True, "hysteresisPeriods": 1},
            status={"promotionCandidateSince": "1"}),
         [assert_role("leader")],
         observed=[peer_hb("cp-a", "us-east-1", 1700000000, "leader")]),

    # Recovering primary must wait for lower-priority leader to release.
    test("two-phase-handoff-wait",
         xr("cp-a", MEMBER_A, [MEMBER_A, MEMBER_B],
            heartbeat={"freshnessTTLSeconds": 999999999}),
         [assert_role("standby")],
         observed=[peer_hb("cp-b", "us-west-2", 1700000000, "leader")]),

    # Azure control plane, single-member set -> leader; own heartbeat is an
    # Azure Resource Group (azure.m.upbound.io) carrying the timestamp/role tags.
    test("azure-leader-alone",
         xr("cp-az", MEMBER_AZ, [MEMBER_AZ]),
         [assert_role_azure("leader")]),

    # Cross-cloud read: AWS CP (priority 2) observes a fresh higher-priority
    # Azure peer's Resource Group heartbeat -> AWS CP stays standby. Exercises
    # reading an Azure peer's heartbeat from an AWS control plane.
    test("cross-cloud-standby-behind-azure",
         xr("cp-b", {"id": "cp-b", "provider": "aws", "region": "us-west-2",
                     "geoTag": "us", "priority": 2},
            [MEMBER_AZ_PRI1, {"id": "cp-b", "provider": "aws",
                              "region": "us-west-2", "geoTag": "us",
                              "priority": 2}],
            heartbeat={"freshnessTTLSeconds": 999999999}),
         [assert_role("standby")],
         observed=[peer_hb_azure("cp-az", "eastus", 1700000000, "leader")]),

    # GCP control plane, single-member set -> leader; own heartbeat is a GCS
    # Bucket (storage.gcp.m.upbound.io) whose labels carry the timestamp/role.
    test("gcp-leader-alone",
         xr("cp-gcp", MEMBER_GCP, [MEMBER_GCP]),
         [assert_role_gcp("leader")]),

    # Tri-cloud read: a GCP CP (priority 3) observes a fresh higher-priority
    # AWS peer's SSM heartbeat -> GCP CP stays standby. Exercises a GCP control
    # plane reading a peer's heartbeat in another cloud.
    test("tri-cloud-standby-behind-aws",
         xr("cp-gcp", MEMBER_GCP, [MEMBER_A, MEMBER_AZ, MEMBER_GCP],
            heartbeat={"freshnessTTLSeconds": 999999999}),
         [assert_role_gcp("standby")],
         observed=[peer_hb("cp-a", "us-east-1", 1700000000, "leader")]),

    # NO DOUBLE-PROMOTION (the key single-leader guarantee for 3+ members):
    # GCP (pri3) sees the pri1 AWS leader DOWN (stale) but the pri2 Azure peer
    # still ALIVE -> GCP defers to the higher-priority survivor and stays
    # standby. Only the highest-priority survivor (Azure) promotes.
    # (freshnessTTLSeconds=1: epoch 1700000000 is stale; a far-future epoch is
    # "fresh" because now-epoch is negative <= ttl.)
    test("tri-cloud-no-double-promote",
         xr("cp-gcp", MEMBER_GCP, [MEMBER_A, MEMBER_AZ, MEMBER_GCP],
            heartbeat={"freshnessTTLSeconds": 1, "writeThrottleSeconds": 1},
            failback={"automatic": True, "hysteresisPeriods": 1}),
         [assert_role_gcp("standby")],
         observed=[
             peer_hb("cp-a", "us-east-1", 1700000000, "leader"),       # AWS down
             peer_hb_azure("cp-az", "eastus", 9999999999, "leader"),   # Azure alive
         ]),

    # Optional k8gb install (auto) renders the helm Releases when GSLB absent.
    test("k8gb-install-auto",
         xr("cp-a", MEMBER_A,
            [MEMBER_A, {"id": "cp-b", "provider": "aws", "region": "eu-west-1",
                        "geoTag": "eu", "priority": 2}],
            gslb={"hostname": "app.cloud.example.com", "strategy": "roundRobin"},
            k8gb={"install": "auto", "version": "v0.15.0"}),
         [
             {"apiVersion": "helm.m.crossplane.io/v1beta1", "kind": "Release",
              "metadata": {"annotations": {
                  "crossplane.io/composition-resource-name": "k8gb-operator"}},
              "spec": {"forProvider": {"values": {
                  "k8gb": {"clusterGeoTag": "us", "extGslbClustersGeoTags": "eu"}}}}},
             {"apiVersion": "helm.m.crossplane.io/v1beta1", "kind": "Release",
              "metadata": {"annotations": {
                  "crossplane.io/composition-resource-name": "k8gb-nginx-ingress"}}},
         ]),
]

output = {"items": [t.model_dump(by_alias=True, exclude_none=True) for t in tests]}
print(yaml.dump(output))
