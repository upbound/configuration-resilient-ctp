"""Resilience composition function (entry point: ``compose(req, rsp)``).

Pipeline position (see apis/resilientcontrolplane/composition.yaml):
  fetch-gslb (function-extra-resources) -> resilience (this) -> auto-ready

Each reconcile this function:
  1. reads the local k8gb GSLB signal (from fetched ``gslbs``),
  2. reads peers' liveness from their observed heartbeat resources,
  3. decides leader/standby via the AND rule (election.py),
  4. writes this CP's own heartbeat + Observe MRs for every peer,
  5. optionally renders the k8gb install stack, and
  6. writes XR status (incl. the ``managementPolicy`` convention contract).

Module layout mirrors configuration-aws-ctp: a flat function directory with a
``compose`` entrypoint and sibling modules imported relatively.
"""

from crossplane.function import resource, response

from . import dns_heartbeat, election, gslb, heartbeat, k8gb_install, status
from .prelude import (
    ROLE_TAG,
    SELF_HB,
    TS_TAG_DEFAULT,
    as_int,
    heartbeat_fqdn,
    now_epoch,
)

EXTRA_RESOURCES_KEY = "apiextensions.crossplane.io/extra-resources"


def compose(req, rsp):
    """Composition function entry point (up wraps this and serves gRPC)."""
    try:
        _compose(req, rsp)
    except Exception as e:  # never crash the pipeline; surface as a fatal result
        response.fatal(rsp, f"resilience function error: {e}")


def _compose(req, rsp):
    xr = resource.struct_to_dict(req.observed.composite.resource)
    spec = xr.get("spec", {}) or {}
    prior_status = xr.get("status", {}) or {}

    identity = spec["identity"]
    members = spec.get("members", []) or []
    gslb_cfg = spec.get("gslb", {}) or {}
    k8gb_cfg = spec.get("k8gb", {}) or {}
    hb_cfg = spec.get("heartbeat", {}) or {}
    failback_cfg = spec.get("failback", {}) or {}

    namespace = xr.get("metadata", {}).get("namespace", "default")
    provider_config = spec.get("providerConfigName", "default")
    ts_tag = hb_cfg.get("tagKey", TS_TAG_DEFAULT)
    ttl = int(hb_cfg.get("freshnessTTLSeconds", 180))
    throttle = int(hb_cfg.get("writeThrottleSeconds", 60))
    backend = hb_cfg.get("backend", "cloudResource")
    dns_cfg = hb_cfg.get("dns", {}) or {}
    dns_zone = dns_cfg.get("zone", "")
    hysteresis = int(failback_cfg.get("hysteresisPeriods", 3))
    now = now_epoch()

    observed = {
        name: resource.struct_to_dict(r.resource)
        for name, r in req.observed.resources.items()
    }

    # 1. GSLB signal from fetched Gslb resources.
    ctx = resource.struct_to_dict(req.context)
    gslbs = (ctx.get(EXTRA_RESOURCES_KEY, {}) or {}).get("gslbs", []) or []
    gslb_signal = gslb.evaluate(gslbs, gslb_cfg.get("hostname", ""),
                                gslb_cfg.get("strategy", "failover"))

    # 2. Peers' liveness (exclude self by id). Source depends on the backend:
    #    cloudResource -> observed Observe MRs; dns -> live TXT resolution.
    peer_members = [m for m in members if m.get("id") != identity["id"]]
    if backend == "dns":
        peers = [
            dns_heartbeat.read_peer_dns(
                m, dns_heartbeat.resolve_txt(heartbeat_fqdn(m["id"], dns_zone)),
                now, ttl,
            )
            for m in peer_members
        ]
    else:
        peers = [
            heartbeat.read_peer(m, observed, ts_tag, now, ttl)
            for m in peer_members
        ]

    # 3. Decide.
    decision = election.decide(
        identity=identity, gslb=gslb_signal, peers=peers,
        prior_status=prior_status, hysteresis_periods=hysteresis,
        write_throttle=throttle, now=now,
    )

    # 4a. Own heartbeat, throttled: reuse the previous epoch if it is still
    # within the throttle window AND the role hasn't changed. The previous
    # epoch comes from the observed own MR (cloudResource) or from prior XR
    # status (dns: the DNSEndpoint's TXT value is not observed back).
    if backend == "dns":
        prev_epoch = as_int(prior_status.get("selfHeartbeatEpoch"), 0)
        prev_role = prior_status.get("role", "")
    else:
        prev = (
            observed.get(SELF_HB, {})
            .get("status", {}).get("atProvider", {}).get("tags", {}) or {}
        )
        prev_epoch = as_int(prev.get(ts_tag), 0)
        prev_role = prev.get(ROLE_TAG, "")
    if prev_epoch and (now - prev_epoch) < throttle and prev_role == decision.role:
        epoch_to_write = prev_epoch
    else:
        epoch_to_write = now

    if backend == "dns":
        # Own heartbeat: external-dns DNSEndpoint (via provider-kubernetes
        # Object). No peer Observe resources — peers are read via live DNS.
        self_name, self_res = dns_heartbeat.build_own_object(
            identity, namespace, dns_zone,
            int(dns_cfg.get("ttlSeconds", 30)),
            dns_cfg.get("kubernetesProviderConfigName", "default"),
            epoch_to_write, decision.role,
        )
        resource.update(rsp.desired.resources[self_name], self_res)
    else:
        self_name, self_res = heartbeat.build_own(
            identity, namespace, provider_config, ts_tag, epoch_to_write,
            decision.role,
        )
        resource.update(rsp.desired.resources[self_name], self_res)

        # 4b. Observe MRs for every peer (cloudResource backend only).
        for m in peer_members:
            name, res = heartbeat.build_peer_observe(
                m, namespace, provider_config, ts_tag)
            resource.update(rsp.desired.resources[name], res)

    # 5. Optional k8gb install.
    install_mode = k8gb_cfg.get("install", "never")
    if k8gb_install.should_install(install_mode, k8gb_present=gslb_signal.found):
        helm_pc = k8gb_cfg.get("helmProviderConfigName", "default")
        for name, res in k8gb_install.build(identity, members, k8gb_cfg,
                                            namespace, helm_pc):
            resource.update(rsp.desired.resources[name], res)

    # 6. Status writeback (status.managementPolicy is the convention contract).
    st = status.build_status(decision=decision, gslb=gslb_signal, peers=peers,
                             self_epoch=epoch_to_write)
    rsp.desired.composite.resource.update({"status": st})
