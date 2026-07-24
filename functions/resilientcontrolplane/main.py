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

import concurrent.futures
import glob
import os
import sys

# Pylon #900: the Upbound embedded-Python build does NOT install requirements.txt
# third-party modules into the function image. Vendor them (functions/
# resilientcontrolplane/vendor, built for linux/amd64) and put that dir on
# sys.path so cloud_read.py's lazy boto3/azure/google imports resolve. Without
# this, directApi peer reads raise ModuleNotFoundError -> CloudReadError -> every
# peer reads "unreadable" -> the election fail-safe holds standbys at standby
# forever (silently). Appended (not prepended) so the image's own deps win.
#
# P1: do NOT hardcode the interpreter minor version. The embedded-Python build
# floats (3.11 today, 3.12 next), so a hardcoded ``python3.11`` silently misses
# the vendor tree on a newer runtime. Derive the running version and fall back
# to a glob over any vendored ``python3.*`` tree so the shim resolves either way.


def _resolve_vendor_dir(base_dir):
    """Locate the vendored linux/amd64 site-packages for the RUNNING Python.

    Prefers the exact ``pythonMAJOR.MINOR`` tree, then any vendored
    ``python3.*`` tree. Returns the resolved dir, or None when none exists."""
    runtime = f"python{sys.version_info.major}.{sys.version_info.minor}"
    candidates = [os.path.join(base_dir, "vendor", "lib", runtime,
                               "site-packages")]
    candidates.extend(sorted(glob.glob(
        os.path.join(base_dir, "vendor", "lib", "python3.*", "site-packages"))))
    for candidate in candidates:
        if os.path.isdir(candidate):
            return candidate
    return None


_VENDOR_DIR = _resolve_vendor_dir(os.path.abspath(os.path.dirname(__file__)))
if _VENDOR_DIR and _VENDOR_DIR not in sys.path:
    sys.path.append(_VENDOR_DIR)

from crossplane.function import resource, response

from . import (cloud_read, election, gslb, gslb_build, heartbeat, k8gb_install,
               status)
from .prelude import (ROLE_TAG, SELF_HB, TS_TAG_DEFAULT, as_int, now_epoch,
                      peer_hb_resource_name)

# Bound the directApi peer-read thread fan-out (Perf1). Cap the pool so
# ``read_timeout * N`` can't blow the gRPC reconcile deadline: reads run in
# ceil(N / workers) concurrent waves instead of serially.
MAX_PARALLEL_PEER_READS = 16

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
    # M-7: single source for the default (mirrors cloud_read's per-call budget)
    # instead of a duplicated bare literal.
    read_timeout = int(hb_cfg.get("readTimeoutSeconds",
                                  cloud_read.DEFAULT_TIMEOUT_SECONDS))
    # Provider account/project/subscription scoping comes entirely from each
    # provider's ProviderConfig/credentials — never from this API. The heartbeat
    # spec is fully provider-agnostic.
    # Peer-read mode: "mr" (default) reads the provider-observed Observe MR
    # (poll-gated); "directApi" reads the cloud API in-function (seconds-fresh,
    # no --poll dependency). The WRITE path is unchanged in both modes.
    read_mode = hb_cfg.get("read", "mr")
    hysteresis = int(failback_cfg.get("hysteresisPeriods", 3))
    now = now_epoch()

    # Peers = every member except self (used both for the read and, in
    # directApi mode, to know which Observe MRs are pure conversion overhead).
    peer_members = [m for m in members if m.get("id") != identity["id"]]

    # Perf8: converting a peer's Observe MR (struct_to_dict) is wasted work in
    # directApi mode — read_peer_direct hits the cloud API, never the MR. Skip
    # those conversions; keep converting everything else (self heartbeat, the
    # composed Gslb, the k8gb Releases) which the rest of _compose consumes.
    skip_conversion = set()
    if read_mode == "directApi":
        skip_conversion = {peer_hb_resource_name(m["id"]) for m in peer_members}
    observed = {
        name: resource.struct_to_dict(r.resource)
        for name, r in req.observed.resources.items()
        if name not in skip_conversion
    }

    # P1 (LOUD): directApi silently degrades to "every peer unreadable" when the
    # vendored cloud SDKs didn't resolve. Shout via a Warning result (surfaces
    # as an XR event) instead of failing safe in silence.
    if read_mode == "directApi" and not _directapi_deps_available():
        response.warning(
            rsp,
            "heartbeat.read=directApi but no vendored cloud SDKs resolved "
            "(vendor/lib/python*/site-packages missing) and boto3 is not "
            "importable: peer reads will ALL be 'unreadable' and standbys hold "
            "at standby. Rebuild the function image with hack/vendor-deps.sh "
            "for the running Python (docs/SPEC.md §11).",
        )

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

    # 2. Peers' liveness (self already excluded above). In directApi mode read
    # each peer straight from the cloud API (seconds-fresh), fanned out
    # concurrently under a bounded budget (Perf1) so latency ~= the slowest
    # read, not the serial sum; otherwise read from the polled Observe MR.
    if read_mode == "directApi":
        peers = _read_peers_direct(peer_members, ts_tag, now, ttl, read_timeout)
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
    # H2: "installed" must be STICKY. A single momentarily-absent observation of
    # the operator Release would otherwise flip should_install False -> desired
    # drops the Releases -> provider-helm UNINSTALLS k8gb, removes the gslbs CRD,
    # and deadlocks the XR (which still references the composed Gslb). So treat
    # ANY observed k8gb-owned resource as evidence we already installed: the
    # operator Release, the nginx Release, OR the composed Gslb (we only ever
    # create the Gslb once the operator was Ready). One missing observation can
    # then no longer drop them all.
    #
    # OPEN INVESTIGATION (fresh-rebuild): live AWS reconciles have shown the
    # operator Release observed with EMPTY spec.forProvider.values (a stale /
    # empty Release), which is a distinct symptom from the absent-observation
    # flip above. Root-cause and add a positive-values guard on the fresh
    # rebuild — see FIX-SPEC H2.
    operator_installed = bool(
        observed.get(k8gb_install.K8GB_OPERATOR)
        or observed.get(k8gb_install.K8GB_NGINX)
        or composed_gslb
    )
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


def _directapi_deps_available() -> bool:
    """Whether directApi's cloud SDKs can actually be imported. Gates the LOUD
    P1 warning: only shout when NO vendor dir resolved AND boto3 is not
    importable from the image (otherwise directApi works, so silence is fine)."""
    if _VENDOR_DIR:
        return True
    try:
        import boto3  # noqa: F401
    except Exception:
        return False
    return True


def _unreadable_peer(member: dict):
    """Fail-safe PeerLiveness for a peer not read within the fan-out budget.
    ``readable=False`` mirrors read_peer_direct's CloudReadError path, so the
    election treats it as "peer unknown -> do NOT promote", never "peer down"."""
    return heartbeat.PeerLiveness(
        cp_id=member["id"],
        priority=int(member["priority"]),
        geo_tag=member.get("geoTag", ""),
        epoch=0,
        age_seconds=10 ** 9,
        fresh=False,
        readable=False,
        role="",
    )


def _read_peers_direct(peer_members: list, ts_tag: str, now: int, ttl: int,
                       read_timeout: int) -> list:
    """Fan out per-peer directApi cloud reads across a bounded thread pool so
    total latency ~= the slowest read, not the SERIAL sum (Perf1: a serial
    list-comp costs ``read_timeout * N`` and blows the gRPC deadline at N peers).

    read_peer_direct is thread-safe (WS-2) and per-call bounded by
    ``read_timeout``. On top of that we impose a single overall wall-clock budget
    across ceil(N / workers) concurrent waves; any peer not back in time fails
    safe as unreadable (blocks promotion, never reads as "down"). Results
    preserve ``peer_members`` order so downstream indexing is unchanged."""
    if not peer_members:
        return []
    max_workers = min(len(peer_members), MAX_PARALLEL_PEER_READS)
    # ceil(N / workers) concurrent waves; allow one extra wave of slack over the
    # per-call timeout so a lone straggler still can't exceed the deadline.
    waves = (len(peer_members) + max_workers - 1) // max_workers
    overall_budget = read_timeout * (waves + 1)
    liveness_by_id = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        future_to_member = {
            pool.submit(heartbeat.read_peer_direct, member, ts_tag, now, ttl,
                        timeout=read_timeout): member
            for member in peer_members
        }
        done, not_done = concurrent.futures.wait(
            future_to_member, timeout=overall_budget)
        for future in done:
            member = future_to_member[future]
            try:
                liveness_by_id[member["id"]] = future.result()
            except Exception:
                # read_peer_direct already swallows CloudReadError; this only
                # catches an unexpected error -> fail safe as unreadable.
                liveness_by_id[member["id"]] = _unreadable_peer(member)
        for future in not_done:
            future.cancel()
            member = future_to_member[future]
            liveness_by_id[member["id"]] = _unreadable_peer(member)
    return [liveness_by_id[member["id"]] for member in peer_members]
