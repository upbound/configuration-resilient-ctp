"""k8gb GSLB signal.

Reads the k8gb ``Gslb`` resources fetched by function-extra-resources (context
key ``gslbs``) and derives, from *this* control plane's local vantage point:

- ``healthy``  — does k8gb consider the load-balanced service healthy?
- ``active``   — is this cluster currently serving the hostname (its ingress
                 IPs appear in the healthy DNS records)?
- ``found``    — was any Gslb present at all?

Degradation: when no Gslb is present (k8gb not installed / not yet reporting),
``found`` is False and both signals default to True so the engine can fall back
to the heartbeat+priority signals rather than deadlock. When a Gslb IS present
but reports unhealthy, that verdict is respected. See docs/SPEC.md §6.1.
"""

from dataclasses import dataclass


@dataclass
class GslbSignal:
    found: bool
    healthy: bool
    active: bool
    geo_tag: str = ""


def _select_gslb(gslbs: list, hostname: str) -> dict | None:
    """Pick the Gslb relevant to ``hostname``. Prefer one whose healthyRecords
    or spec references the hostname; otherwise fall back to the first."""
    if not gslbs:
        return None
    for g in gslbs:
        status = g.get("status", {}) or {}
        healthy_records = status.get("healthyRecords", {}) or {}
        if hostname in healthy_records:
            return g
    return gslbs[0]


def evaluate(gslbs: list, hostname: str, strategy: str) -> GslbSignal:
    """Compute the local GSLB signal for this control plane."""
    if not gslbs:
        return GslbSignal(found=False, healthy=True, active=True)

    g = _select_gslb(gslbs, hostname)
    if g is None:
        return GslbSignal(found=False, healthy=True, active=True)

    status = g.get("status", {}) or {}
    service_health = status.get("serviceHealth", {}) or {}
    geo_tag = status.get("geoTag", "") or ""

    # Healthy iff every reported domain is Healthy. Empty serviceHealth is
    # treated as "not yet known" -> not healthy (fail safe).
    if service_health:
        healthy = all(v == "Healthy" for v in service_health.values())
    else:
        healthy = False

    # Active iff this cluster's exposed ingress IPs intersect the healthy DNS
    # records for the hostname (i.e. traffic is being routed here). In
    # roundRobin/geoip every healthy cluster is active; in failover only the
    # currently-serving geo is.
    healthy_records = set((status.get("healthyRecords") or {}).get(hostname, []) or [])
    exposed_ips = set((status.get("loadBalancer", {}) or {}).get("exposedIps", []) or [])
    active = bool(healthy_records and exposed_ips and (healthy_records & exposed_ips))

    return GslbSignal(found=True, healthy=healthy, active=active, geo_tag=geo_tag)
