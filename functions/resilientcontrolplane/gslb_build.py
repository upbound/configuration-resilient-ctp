"""Create the k8gb ``Gslb`` (and, optionally, a demo backend) from the claim.

Design goal (single-claim UX): a ResilientControlPlane claim should yield a
working GSLB *signal* with no extra objects to apply. So the composition can
create the ``Gslb`` itself — matching configuration-k8gb-bluegreen, which
composes the ``Gslb`` directly (Crossplane v2 lets a composition manage
arbitrary Kubernetes resources; the composite controller applies them, so no
provider-kubernetes ``Object`` wrapper is needed). The crossplane service
account needs RBAC on ``k8gb.absa.oss/gslbs`` — a scoped platform prerequisite
shipped as examples/rbac-k8gb.yaml.

``gslb.py`` READS a Gslb's status to drive the election; this module WRITES the
Gslb. The two are deliberately separate (read vs. write).

The Gslb is only created when k8gb is (or is being) installed, so we never
apply a Gslb before its CRD exists.
"""

# Composition-resource-names for the objects this module renders.
GSLB_RESOURCE = "gslb-app"
DEMO_DEPLOYMENT = "gslb-demo-app"
DEMO_SERVICE = "gslb-demo-svc"

_DEFAULT_BACKEND_SERVICE = "app"
_DEFAULT_INGRESS_CLASS = "nginx"
_DEMO_IMAGE = "nginx:stable"


def should_manage(gslb_cfg: dict, k8gb_ready: bool, gslb_found: bool) -> bool:
    """Create the Gslb only when requested AND k8gb is actually PRESENT — the
    operator Release is Ready (so its CRDs are established) or a Gslb already
    exists. Gating on real readiness (not merely ``install: auto`` intent)
    avoids emitting a Gslb before its CRD exists, which would otherwise fail
    apply ("no matches for kind Gslb") and flap until the RESTMapper refreshes
    (operational-review guard #1). On a fresh auto-install the Gslb is created
    on the reconcile *after* the operator becomes Ready."""
    if not gslb_cfg.get("manage", True):
        return False
    return k8gb_ready or gslb_found


def build(identity: dict, members: list, gslb_cfg: dict,
          namespace: str) -> list[tuple[str, dict]]:
    """Return [(composition-resource-name, resource), ...] for the Gslb and,
    when ``gslb.demoApp`` is set, a demo Deployment+Service to back it (so k8gb
    has a real endpoint to health-check)."""
    hostname = gslb_cfg.get("hostname", "")
    strategy = gslb_cfg.get("strategy", "failover")
    ingress_class = gslb_cfg.get("ingressClassName", _DEFAULT_INGRESS_CLASS)
    demo = bool(gslb_cfg.get("demoApp", False))
    backend_service = (DEMO_SERVICE if demo
                       else gslb_cfg.get("backendServiceName", _DEFAULT_BACKEND_SERVICE))

    resources = [(GSLB_RESOURCE, _gslb(hostname, strategy, ingress_class,
                                       backend_service, identity, members,
                                       namespace))]
    if demo:
        resources.append((DEMO_DEPLOYMENT, _demo_deployment(namespace)))
        resources.append((DEMO_SERVICE, _demo_service(namespace)))
    return resources


def _gslb(hostname: str, strategy: str, ingress_class: str,
          backend_service: str, identity: dict, members: list,
          namespace: str) -> dict:
    """A k8gb Gslb wrapping an Ingress for ``hostname``. For the failover
    strategy the primary geo is the lowest-priority (preferred-leader) member,
    so DNS prefers the same control plane the election prefers."""
    strat = {"type": strategy}
    if strategy == "failover":
        primary = min(members, key=lambda m: int(m.get("priority", 999)),
                      default=identity)
        strat["primaryGeoTag"] = primary.get("geoTag", identity.get("geoTag", ""))

    return {
        "apiVersion": "k8gb.absa.oss/v1beta1",
        "kind": "Gslb",
        "metadata": {
            "name": GSLB_RESOURCE,
            "namespace": namespace,
            "annotations": {
                "crossplane.io/composition-resource-name": GSLB_RESOURCE,
            },
        },
        "spec": {
            "ingress": {
                "ingressClassName": ingress_class,
                "rules": [{
                    "host": hostname,
                    "http": {"paths": [{
                        "path": "/",
                        "pathType": "Prefix",
                        "backend": {"service": {
                            "name": backend_service,
                            "port": {"number": 80},
                        }},
                    }]},
                }],
            },
            "strategy": strat,
        },
    }


def _demo_deployment(namespace: str) -> dict:
    """A minimal always-healthy backend so k8gb has a real endpoint to probe;
    scaling it to 0 (or deleting it) is how a live GSLB health failure is
    driven in tests."""
    labels = {"app": "gslb-demo"}
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": DEMO_DEPLOYMENT,
            "namespace": namespace,
            "annotations": {
                "crossplane.io/composition-resource-name": DEMO_DEPLOYMENT,
            },
        },
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": labels},
            "template": {
                "metadata": {"labels": labels},
                "spec": {"containers": [{
                    "name": "nginx",
                    "image": _DEMO_IMAGE,
                    "ports": [{"containerPort": 80}],
                }]},
            },
        },
    }


def _demo_service(namespace: str) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {
            "name": DEMO_SERVICE,
            "namespace": namespace,
            "annotations": {
                "crossplane.io/composition-resource-name": DEMO_SERVICE,
            },
        },
        "spec": {
            "selector": {"app": "gslb-demo"},
            "ports": [{"port": 80, "targetPort": 80}],
        },
    }
