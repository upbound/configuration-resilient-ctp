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


def gslb_resource(service_health, *, hostname=None, healthy_ips=None,
                  exposed_ips=None):
    """A k8gb Gslb status. Pass hostname+healthy_ips+exposed_ips to make this
    cluster GSLB-*active* (its exposed ingress IPs appear in the healthy DNS
    records); omit them to model a healthy-but-not-serving (passive) geo."""
    status = {"serviceHealth": service_health}
    if hostname and healthy_ips is not None:
        status["healthyRecords"] = {hostname: healthy_ips}
    if exposed_ips is not None:
        status["loadBalancer"] = {"exposedIps": exposed_ips}
    return {
        "apiVersion": "k8gb.absa.oss/v1beta1", "kind": "Gslb",
        "metadata": {"name": "app", "namespace": "default"},
        "status": status,
    }


def observed_gslb(service_health, *, hostname="app.failover.example.test",
                  healthy_ips=None, exposed_ips=None):
    """The composition's OWN Gslb as an observed composed resource (keyed by
    composition-resource-name gslb-app), so tests exercise reading serviceHealth
    back from observed composed resources (not just the extra-resources fetch)."""
    r = gslb_resource(service_health, hostname=hostname, healthy_ips=healthy_ips,
                      exposed_ips=exposed_ips)
    r["metadata"]["name"] = "gslb-app"
    r["metadata"]["annotations"] = {
        "crossplane.io/composition-resource-name": "gslb-app"}
    return r


def k8gb_operator_ready():
    """An observed provider-helm Release for the k8gb operator reporting Ready,
    used to prove the composition creates the Gslb only once k8gb is installed."""
    return {
        "apiVersion": "helm.m.crossplane.io/v1beta1", "kind": "Release",
        "metadata": {"name": "k8gb-operator", "namespace": "default",
                     "annotations": {
                         "crossplane.io/composition-resource-name": "k8gb-operator"}},
        "status": {"conditions": [{"type": "Ready", "status": "True"}]},
    }


# Context key the composition reads fetched Gslb resources from.
EXTRA_RES_KEY = "apiextensions.crossplane.io/extra-resources"


def gslb_context(service_health, **kw):
    """Wrap a Gslb into the context shape function-extra-resources produces."""
    return {EXTRA_RES_KEY: {"gslbs": [gslb_resource(service_health, **kw)]}}


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

    # k8gb stays installed once we installed it, even after our own Gslb exists
    # (regression: auto used to uninstall k8gb the moment a Gslb appeared, which
    # removed the CRD and deadlocked the XR). Observed: our operator Release +
    # our Gslb -> the k8gb Releases are STILL rendered.
    test("k8gb-kept-installed-when-gslb-present",
         xr("cp-a", MEMBER_A, [MEMBER_A],
            gslb={"hostname": "app.failover.example.test", "strategy": "failover",
                  "manage": True},
            k8gb={"install": "auto"}),
         [
             {"apiVersion": "helm.m.crossplane.io/v1beta1", "kind": "Release",
              "metadata": {"annotations": {
                  "crossplane.io/composition-resource-name": "k8gb-operator"}}},
         ],
         observed=[k8gb_operator_ready(),
                   observed_gslb({"app.failover.example.test": "Healthy"},
                                 healthy_ips=["1.2.3.4"], exposed_ips=["1.2.3.4"])]),

    # k8gb install with Route53 external-dns: the extdns block must gain the
    # aws provider, domainFilters derived from dnsZones, and creds env from the
    # configured secret. Domain is supplied via the claim, never hardcoded.
    test("k8gb-install-route53-extdns",
         xr("cp-a", MEMBER_A,
            [MEMBER_A, {"id": "cp-b", "provider": "aws", "region": "eu-west-1",
                        "geoTag": "eu", "priority": 2}],
            gslb={"hostname": "failover.gslb.example.test", "strategy": "failover"},
            k8gb={"install": "always", "version": "v0.15.0",
                  "dnsProvider": "aws",
                  "extdnsCredentialsSecretName": "extdns-aws",
                  "dnsZones": [{"parentZone": "gslb.example.test",
                                "loadBalancedZone": "failover.gslb.example.test",
                                "negTTL": 30}]}),
         [
             {"apiVersion": "helm.m.crossplane.io/v1beta1", "kind": "Release",
              "metadata": {"annotations": {
                  "crossplane.io/composition-resource-name": "k8gb-operator"}},
              "spec": {"forProvider": {"values": {"extdns": {
                  "provider": {"name": "aws"},
                  "domainFilters": ["gslb.example.test"]}}}}},
         ]),

    # Claim creates the Gslb once the k8gb operator Release is observed Ready
    # (single-claim GSLB signal). Demo backend rendered too. Domain from claim.
    test("gslb-created-when-operator-ready",
         xr("cp-a", MEMBER_A, [MEMBER_A],
            gslb={"hostname": "app.failover.example.test", "strategy": "failover",
                  "manage": True, "demoApp": True}),
         [
             {"apiVersion": "k8gb.absa.oss/v1beta1", "kind": "Gslb",
              "metadata": {"annotations": {
                  "crossplane.io/composition-resource-name": "gslb-app"}},
              "spec": {"strategy": {"type": "failover", "primaryGeoTag": "us"}}},
             {"apiVersion": "v1", "kind": "Service",
              "metadata": {"annotations": {
                  "crossplane.io/composition-resource-name": "gslb-demo-svc"}}},
         ],
         observed=[k8gb_operator_ready()]),

    # Gslb is NOT created before k8gb is ready (no operator Release observed) ->
    # avoids applying a Gslb before its CRD exists.
    test("gslb-not-created-before-k8gb-ready",
         xr("cp-a", MEMBER_A, [MEMBER_A],
            gslb={"hostname": "app.failover.example.test", "strategy": "failover",
                  "manage": True}),
         [assert_role("leader")]),

    # The composition's OWN Gslb (observed composed resource) drives the
    # election: serviceHealth Unhealthy read back from observed -> step down.
    # Regression guard for the extra-resources-empty bug found in live testing.
    test("gslb-unhealthy-from-composed-observed",
         xr("cp-a", MEMBER_A, [MEMBER_A],
            gslb={"hostname": "app.failover.example.test", "strategy": "failover",
                  "manage": True, "demoApp": True}),
         [assert_role("standby")],
         observed=[k8gb_operator_ready(),
                   observed_gslb({"app.failover.example.test": "Unhealthy"},
                                 healthy_ips=[], exposed_ips=["1.2.3.4"])]),

    # --- GSLB is the PRIMARY signal (found=True path; Tests 1&2 never hit this).
    # A Gslb present, healthy, and active (this cluster's exposed IPs are in the
    # healthy DNS records) -> leader, even alone.
    test("gslb-healthy-active-leader",
         xr("cp-a", MEMBER_A, [MEMBER_A],
            gslb={"hostname": "app.cloud.example.com", "strategy": "failover"}),
         [assert_role("leader")],
         context=gslb_context({"app.cloud.example.com": "Healthy"},
                              hostname="app.cloud.example.com",
                              healthy_ips=["1.2.3.4"], exposed_ips=["1.2.3.4"])),

    # Gslb present but UNHEALTHY -> step down to standby even with no peer.
    # This is the failure signal the design is built around.
    test("gslb-unhealthy-standby",
         xr("cp-a", MEMBER_A, [MEMBER_A],
            gslb={"hostname": "app.cloud.example.com", "strategy": "failover"}),
         [assert_role("standby")],
         context=gslb_context({"app.cloud.example.com": "Unhealthy"},
                              hostname="app.cloud.example.com",
                              healthy_ips=[], exposed_ips=["1.2.3.4"])),

    # Gslb healthy but this cluster is NOT the serving geo (exposed IPs absent
    # from the healthy records) -> not active -> standby (failover/passive geo).
    test("gslb-healthy-not-active-standby",
         xr("cp-a", MEMBER_A, [MEMBER_A],
            gslb={"hostname": "app.cloud.example.com", "strategy": "failover"}),
         [assert_role("standby")],
         context=gslb_context({"app.cloud.example.com": "Healthy"},
                              hostname="app.cloud.example.com",
                              healthy_ips=["9.9.9.9"], exposed_ips=["1.2.3.4"])),

    # --- #29 election gate (v3): relax the higher-peer block on the LOCAL GSLB
    # view (gslb-active-failover), never on the peer's lagging role tag alone.

    # THE #29 FIX (regression catcher): cp-b is GSLB-active-failover (its exposed
    # IPs are in the healthy records) and the higher-priority cp-a is fresh but
    # advertises role=standby -- it stepped down because ITS geo went unhealthy
    # and k8gb failed over to cp-b. v3 -> cp-b PROMOTES. Pre-v3 (liveness-only, a
    # fresh higher peer blocks regardless of role) this was a PERMANENT standby,
    # the exact bug observed live in Test 1.
    test("promote-past-stepped-down-higher-peer",
         xr("cp-b", MEMBER_B, [MEMBER_A, MEMBER_B],
            gslb={"hostname": "app.cloud.example.com", "strategy": "failover"},
            heartbeat={"freshnessTTLSeconds": 999999999, "writeThrottleSeconds": 1},
            failback={"automatic": True, "hysteresisPeriods": 1},
            status={"promotionCandidateSince": "1"}),
         [assert_role("leader")],
         observed=[peer_hb("cp-a", "us-east-1", 1700000000, "standby")],
         context=gslb_context({"app.cloud.example.com": "Healthy"},
                              hostname="app.cloud.example.com",
                              healthy_ips=["1.2.3.4"], exposed_ips=["1.2.3.4"])),

    # C1 REGRESSION GUARD (double-leader on former-leader recovery): cp-a is the
    # HIGHEST priority and its persisted status.role=="leader", but it DIED and
    # recovered -> its selfHeartbeatEpoch is stale (1700000000, far older than the
    # failover damping window). A lower-priority peer cp-b took over during the
    # outage and is fresh + role=leader (still holding ["*"]). cp-a must NOT trust
    # the stale persisted role as "currently leading" and seize ["*"] -- it must
    # wait out the two-phase handoff (standby) until cp-b releases. Before the C1
    # fix, prior_role=="leader" skipped the handoff gate -> cp-a AND cp-b both
    # leader (split-brain). continuous_leader is False here (self-hb stale).
    test("recovered-former-leader-holds-for-interim-leader",
         xr("cp-a", MEMBER_A, [MEMBER_A, MEMBER_B],
            heartbeat={"freshnessTTLSeconds": 999999999},
            status={"role": "leader", "selfHeartbeatEpoch": 1700000000}),
         [assert_role("standby")],
         observed=[peer_hb("cp-b", "us-west-2", 1700000000, "leader")]),

    # PARTITION TIE-BREAKER (the safety gate): cp-b is GSLB-active-failover BUT
    # the higher-priority cp-a is fresh AND still advertises role=leader -- a
    # gray failure that partitions only the health-check plane, so both geos
    # self-compute active locally. v3 holds cp-b at standby on cp-a's live
    # heartbeat: the independent signal that breaks the tie and prevents a double
    # leader. Hysteresis is satisfied here to prove it is the role==leader check,
    # not the timer, that holds. (role==leader is evaluated BEFORE the
    # gslb-active-failover relax -- ordering is load-bearing.)
    test("hold-standby-when-higher-peer-still-leader",
         xr("cp-b", MEMBER_B, [MEMBER_A, MEMBER_B],
            gslb={"hostname": "app.cloud.example.com", "strategy": "failover"},
            heartbeat={"freshnessTTLSeconds": 999999999, "writeThrottleSeconds": 1},
            failback={"automatic": True, "hysteresisPeriods": 1},
            status={"promotionCandidateSince": "1"}),
         [assert_role("standby")],
         observed=[peer_hb("cp-a", "us-east-1", 1700000000, "leader")],
         context=gslb_context({"app.cloud.example.com": "Healthy"},
                              hostname="app.cloud.example.com",
                              healthy_ips=["1.2.3.4"], exposed_ips=["1.2.3.4"])),

    # PERMISSIVE MODE unchanged (gslb.found is load-bearing): with no Gslb ->
    # found=False -> priority+heartbeat only, a fresh higher peer holds a lower
    # one REGARDLESS of its advertised role. Without independent single-active
    # arbitration we must stay conservative even for a stepped-down peer.
    test("permissive-holds-fresh-higher-peer-regardless-of-role",
         xr("cp-b", MEMBER_B, [MEMBER_A, MEMBER_B],
            heartbeat={"freshnessTTLSeconds": 999999999}),
         [assert_role("standby")],
         observed=[peer_hb("cp-a", "us-east-1", 1700000000, "standby")]),

    # FAILOVER-ONLY restriction: in roundRobin every healthy geo is active, so
    # GSLB-active is NOT exclusive and must not relax the higher-peer gate. cp-b
    # active + higher cp-a fresh+standby but strategy=roundRobin -> stay standby.
    test("roundrobin-holds-fresh-stepped-down-higher-peer",
         xr("cp-b", MEMBER_B, [MEMBER_A, MEMBER_B],
            gslb={"hostname": "app.cloud.example.com", "strategy": "roundRobin"},
            heartbeat={"freshnessTTLSeconds": 999999999, "writeThrottleSeconds": 1},
            failback={"automatic": True, "hysteresisPeriods": 1},
            status={"promotionCandidateSince": "1"}),
         [assert_role("standby")],
         observed=[peer_hb("cp-a", "us-east-1", 1700000000, "standby")],
         context=gslb_context({"app.cloud.example.com": "Healthy"},
                              hostname="app.cloud.example.com",
                              healthy_ips=["1.2.3.4"], exposed_ips=["1.2.3.4"])),

    # ===== ACTIVE-ACTIVE (roundRobin/geoip) — EXACTLY ONE LEADER ALWAYS =====
    # In roundRobin/geoip every healthy geo is GSLB-active for TRAFFIC (all
    # members satisfy gslb.healthy+active). GSLB therefore provides NO
    # exclusivity, so single MANAGEMENT leadership is carried entirely by
    # priority + heartbeat: exactly the highest-priority live member holds
    # ["*"]; every other member is ["Observe"] even though it is GSLB-active.
    # These cases prove that invariant. Tri-geo set: us(pri1)/eu(pri2)/ap(pri3).

    # AA-1: highest priority, GSLB-active, 3-member roundRobin -> LEADER.
    test("active-active-roundrobin-highest-priority-leads",
         xr("cp-a", MEMBER_A, [MEMBER_A, MEMBER_AZ, MEMBER_GCP],
            gslb={"hostname": "app.cloud.example.com", "strategy": "roundRobin"}),
         [assert_role("leader")],
         context=gslb_context({"app.cloud.example.com": "Healthy"},
                              hostname="app.cloud.example.com",
                              healthy_ips=["1.1.1.1", "2.2.2.2", "3.3.3.3"],
                              exposed_ips=["1.1.1.1"])),

    # AA-2 (headline): a MIDDLE-priority member is GSLB-ACTIVE but NOT leader,
    # because a higher-priority peer is alive and leading. Active != leader.
    test("active-active-roundrobin-active-but-not-leader",
         xr("cp-az", MEMBER_AZ, [MEMBER_A, MEMBER_AZ, MEMBER_GCP],
            gslb={"hostname": "app.cloud.example.com", "strategy": "roundRobin"},
            heartbeat={"freshnessTTLSeconds": 999999999}),
         [assert_role_azure("standby")],
         observed=[peer_hb("cp-a", "us-east-1", 1700000000, "leader")],
         context=gslb_context({"app.cloud.example.com": "Healthy"},
                              hostname="app.cloud.example.com",
                              healthy_ips=["1.1.1.1", "2.2.2.2", "3.3.3.3"],
                              exposed_ips=["2.2.2.2"])),

    # AA-3: lowest priority, geoip, GSLB-active, both higher peers alive
    # (pri1 leading, pri2 stepped down) -> standby. No promotion while any
    # higher peer is alive, regardless of their role, in active-active.
    test("active-active-geoip-lowest-priority-holds",
         xr("cp-gcp", MEMBER_GCP, [MEMBER_A, MEMBER_AZ, MEMBER_GCP],
            gslb={"hostname": "app.cloud.example.com", "strategy": "geoip"},
            heartbeat={"freshnessTTLSeconds": 999999999}),
         [assert_role_gcp("standby")],
         observed=[peer_hb("cp-a", "us-east-1", 1700000000, "leader"),
                   peer_hb_azure("cp-az", "eastus", 1700000000, "standby")],
         context=gslb_context({"app.cloud.example.com": "Healthy"},
                              hostname="app.cloud.example.com",
                              healthy_ips=["1.1.1.1", "2.2.2.2", "3.3.3.3"],
                              exposed_ips=["3.3.3.3"])),

    # AA-4: single-leader preserved after the highest fails. roundRobin, pri3
    # is GSLB-active, pri1 is DOWN (stale) but pri2 is alive and leading ->
    # pri3 defers to the pri2 survivor and stays standby (NO double-promote;
    # exactly one leader = pri2).
    test("active-active-no-double-promote-after-failure",
         xr("cp-gcp", MEMBER_GCP, [MEMBER_A, MEMBER_AZ, MEMBER_GCP],
            gslb={"hostname": "app.cloud.example.com", "strategy": "roundRobin"},
            heartbeat={"freshnessTTLSeconds": 1, "writeThrottleSeconds": 1},
            failback={"automatic": True, "hysteresisPeriods": 1}),
         [assert_role_gcp("standby")],
         observed=[peer_hb("cp-a", "us-east-1", 1700000000, "leader"),        # stale -> down
                   peer_hb_azure("cp-az", "eastus", 9999999999, "leader")],   # fresh survivor leader
         context=gslb_context({"app.cloud.example.com": "Healthy"},
                              hostname="app.cloud.example.com",
                              healthy_ips=["1.1.1.1", "2.2.2.2", "3.3.3.3"],
                              exposed_ips=["3.3.3.3"])),
]

output = {"items": [t.model_dump(by_alias=True, exclude_none=True) for t in tests]}
print(yaml.dump(output))
