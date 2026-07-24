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

import os
import sys

# Pylon #900: the Upbound embedded-Python build does NOT install requirements.txt
# third-party modules into the function image. Vendor them (functions/
# resilientcontrolplane/vendor, built for linux/amd64 + py3.11) and put that dir
# on sys.path so cloud_read.py's lazy boto3/azure/google imports resolve. Without
# this, directApi peer reads raise ModuleNotFoundError -> CloudReadError -> every
# peer reads "unreadable" -> the election fail-safe holds standbys at standby
# forever (silently). Appended (not prepended) so the image's own deps win.
_vendor = os.path.join(os.path.abspath(os.path.dirname(__file__)),
                       "vendor", "lib", "python3.11", "site-packages")
if os.path.isdir(_vendor) and _vendor not in sys.path:
    sys.path.append(_vendor)

from crossplane.function import resource, response

from . import election, gslb, gslb_build, heartbeat, k8gb_install, status
from .prelude import ROLE_TAG, SELF_HB, TS_TAG_DEFAULT, as_int, now_epoch

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
    ts_tag = hb_cfg.get("livenessKey", TS_TAG_DEFAULT)
    ttl = int(hb_cfg.get("freshnessTTLSeconds", 180))
    throttle = int(hb_cfg.get("writeThrottleSeconds", 60))
    # directApi per-read timeout (seconds), tunable at runtime via the claim so a
    # cross-cloud / cold read needing more than the default budget doesn't
    # spuriously fail as "peer unreadable" and block leader election. mr mode
    # ignores it.
    read_timeout = int(hb_cfg.get("readTimeoutSeconds", 10))
    # Provider account/project/subscription scoping comes entirely from each
    # provider's ProviderConfig/credentials — never from this API. The heartbeat
    # spec is fully provider-agnostic.
    # Peer-read mode: "mr" (default) reads the provider-observed Observe MR
    # (poll-gated); "directApi" reads the cloud API in-function (seconds-fresh,
    # no --poll dependency). The WRITE path is unchanged in both modes.
    read_mode = hb_cfg.get("read", "mr")
    hysteresis = int(failback_cfg.get("hysteresisPeriods", 3))
    now = now_epoch()

    observed = {
        name: resource.struct_to_dict(r.resource)
        for name, r in req.observed.resources.items()
    }

    # 1. GSLB signal. Read the Gslb from BOTH sources: any externally-managed
    # Gslbs fetched via function-extra-resources (context "gslbs"), AND the Gslb
    # this composition creates itself, whose observed status arrives in
    # observed.resources (like the reference package, which reads serviceHealth
    # from observed composed resources). The composed Gslb is authoritative and
    # reliable — the extra-resources fetch can be empty for a namespaced Gslb.
    ctx = resource.struct_to_dict(req.context)
    gslbs = list((ctx.get(EXTRA_RESOURCES_KEY, {}) or {}).get("gslbs", []) or [])
    composed_gslb = observed.get(gslb_build.GSLB_RESOURCE)
    if composed_gslb and composed_gslb.get("kind") == "Gslb":
        gslbs.append(composed_gslb)
    strategy = gslb_cfg.get("strategy", "failover")
    gslb_signal = gslb.evaluate(gslbs, gslb_cfg.get("hostname", ""), strategy)

    # 2. Peers' liveness (exclude self by id). In directApi mode read each peer
    # straight from the cloud API (seconds-fresh); otherwise from the polled MR.
    peer_members = [m for m in members if m.get("id") != identity["id"]]
    if read_mode == "directApi":
        peers = [
            heartbeat.read_peer_direct(m, ts_tag, now, ttl, timeout=read_timeout)
            for m in peer_members
        ]
    else:
        peers = [heartbeat.read_peer(m, observed, ts_tag, now, ttl)
                 for m in peer_members]

    # 3. Decide.
    decision = election.decide(
        identity=identity, gslb=gslb_signal, peers=peers,
        prior_status=prior_status, hysteresis_periods=hysteresis,
        write_throttle=throttle, now=now, strategy=strategy,
    )

    # 4a. Own heartbeat, throttled: reuse the previous epoch if it is still
    # within the throttle window AND the role hasn't changed.
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

    self_name, self_res = heartbeat.build_own(
        identity, namespace, provider_config, ts_tag, epoch_to_write,
        decision.role,
    )
    resource.update(rsp.desired.resources[self_name], self_res)

    # 4b. Observe MRs for every peer.
    for m in members:
        if m.get("id") == identity["id"]:
            continue
        name, res = heartbeat.build_peer_observe(m, namespace, provider_config,
                                                 ts_tag)
        resource.update(rsp.desired.resources[name], res)

    # 5. Optional k8gb install. Keep it installed once we've installed it
    # (operator Release observed) — never uninstall just because our own Gslb
    # now exists, which would remove the CRD and deadlock the XR.
    install_mode = k8gb_cfg.get("install", "auto")
    operator_installed = bool(observed.get(k8gb_install.K8GB_OPERATOR))
    if k8gb_install.should_install(install_mode, gslb_signal.found,
                                   operator_installed=operator_installed):
        helm_pc = k8gb_cfg.get("helmProviderConfigName", "default")
        for name, res in k8gb_install.build(identity, members, k8gb_cfg,
                                            namespace, helm_pc):
            resource.update(rsp.desired.resources[name], res)

    # 5b. Optionally create the Gslb (single-claim GSLB signal). Gated on k8gb
    # being READY (operator Release Ready, so its CRD exists) or a Gslb already
    # present — never emit a Gslb before its CRD (operational guard #1).
    k8gb_ready = _release_ready(observed.get(k8gb_install.K8GB_OPERATOR, {}))
    if gslb_build.should_manage(gslb_cfg, k8gb_ready, gslb_signal.found):
        for name, res in gslb_build.build(identity, members, gslb_cfg, namespace):
            resource.update(rsp.desired.resources[name], res)

    # 6. Status writeback (status.managementPolicy is the convention contract).
    st = status.build_status(decision=decision, gslb=gslb_signal, peers=peers,
                             self_epoch=epoch_to_write)
    rsp.desired.composite.resource.update({"status": st})


def _release_ready(observed_release: dict) -> bool:
    """True if an observed provider-helm Release reports Ready=True. Used to
    confirm k8gb's operator (and thus its CRDs) is installed before creating a
    Gslb."""
    conds = (observed_release.get("status", {}) or {}).get("conditions", []) or []
    return any(c.get("type") == "Ready" and c.get("status") == "True"
               for c in conds)
