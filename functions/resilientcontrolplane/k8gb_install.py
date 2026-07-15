"""Optional k8gb install (docs/SPEC.md §4.1).

Ported from configuration-k8gb-bluegreen's ``functions/k8gb-operator`` (KCL):
installs nginx-ingress and the k8gb operator as namespaced Helm Releases
(``helm.m.crossplane.io/v1beta1``). Gated by ``spec.k8gb.install``:

- ``never``  (default): render nothing.
- ``auto``:  render only when k8gb is NOT detected present.
- ``always``: always render.

Detection for ``auto`` is coarse (presence of a k8gb Gslb resource). The
init-ingress used by the source package for CoreDNS IP discovery is deferred
(it needs provider-kubernetes for a raw Ingress); the k8gb Helm chart's own
ingress config covers the common case. See ROADMAP backlog.
"""

_NGINX = "k8gb-nginx-ingress"
_K8GB = "k8gb-operator"


def should_install(mode: str, k8gb_present: bool) -> bool:
    if mode == "always":
        return True
    if mode == "auto":
        return not k8gb_present
    return False  # "never" / anything else


def _release(name: str, namespace: str, provider_config: str, chart: dict,
             target_ns: str, values: dict) -> dict:
    return {
        "apiVersion": "helm.m.crossplane.io/v1beta1",
        "kind": "Release",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "annotations": {"crossplane.io/composition-resource-name": name},
        },
        "spec": {
            "forProvider": {
                "chart": chart,
                "namespace": target_ns,
                "values": values,
                "wait": True,
                "waitTimeout": "600s",
            },
            "providerConfigRef": {"name": provider_config, "kind": "ProviderConfig"},
        },
    }


def build(identity: dict, members: list, k8gb: dict, namespace: str,
          helm_provider_config: str) -> list[tuple[str, dict]]:
    """Return [(composition-resource-name, resource-dict), ...] for the k8gb
    install stack."""
    geo = identity.get("geoTag", "")
    ext_geos = sorted({m.get("geoTag", "") for m in members
                       if m.get("geoTag") and m.get("geoTag") != geo})
    version = k8gb.get("version", "v0.15.0")
    dns_zones = k8gb.get("dnsZones", [
        {"parentZone": "example.com", "loadBalancedZone": "cloud.example.com",
         "negTTL": 30}
    ])
    edge_dns = k8gb.get("edgeDNSServers", ["1.1.1.1"])
    log_level = k8gb.get("logLevel", "info")

    nginx = _release(
        _NGINX, namespace, helm_provider_config,
        {"name": "ingress-nginx",
         "repository": "https://kubernetes.github.io/ingress-nginx",
         "version": "4.0.15"},
        "k8gb",
        {
            "controller": {
                "admissionWebhooks": {"enabled": False, "patch": {"enabled": False}},
                "hostNetwork": True,
                "publishService": {"enabled": False},
                "daemonset": {"useHostPort": False,
                              "hostPorts": {"http": 80, "https": 443}},
                "kind": "DaemonSet",
                "service": {"enabled": False},
            },
            "udp": {"53": "k8gb/k8gb-coredns:53"},
        },
    )

    k8gb_rel = _release(
        _K8GB, namespace, helm_provider_config,
        {"name": "k8gb", "repository": "https://www.k8gb.io", "version": version},
        "k8gb",
        {
            "k8gb": {
                "deployCrds": True,
                "deployRbac": True,
                "clusterGeoTag": geo,
                "extGslbClustersGeoTags": ",".join(ext_geos),
                "dnsZones": dns_zones,
                "edgeDNSServers": edge_dns,
                "log": {"format": "simple", "level": log_level},
                "reconcileRequeueSeconds": 30,
                "nsRecordTTL": 30,
                "metricsAddress": "0.0.0.0:8080",
            },
            "extdns": {
                "enabled": True,
                "interval": "20s",
                "labelFilter": "k8gb.absa.oss/dnstype=extdns",
                "logLevel": "debug",
                "policy": "sync",
                "txtOwnerId": f"k8gb-{geo}",
                "txtPrefix": f"k8gb-{geo}-",
            },
        },
    )

    return [(_NGINX, nginx), (_K8GB, k8gb_rel)]
