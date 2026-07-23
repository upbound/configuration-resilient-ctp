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
# Public alias: main.py reads this Release's observed readiness to gate Gslb
# creation (Gslb CRD only exists once the operator Release is Ready).
K8GB_OPERATOR = _K8GB


def should_install(mode: str, external_k8gb_present: bool,
                   operator_installed: bool = False) -> bool:
    """Whether to (keep) rendering the k8gb install Releases.

    CRITICAL: once WE have installed k8gb (``operator_installed`` — our operator
    Release is observed), we must KEEP rendering it every reconcile. provider-helm
    is declarative: if the Release disappears from desired, it UNINSTALLS k8gb,
    which removes the gslbs CRD and deadlocks the XR (it still references the
    composed Gslb). Earlier this self-destructed: ``auto`` used "a Gslb exists"
    as the presence signal, but WE create the Gslb — so the moment our Gslb
    appeared, auto flipped to "present -> skip", uninstalling k8gb.

    - ``always``: always render.
    - ``auto``: keep rendering if we already installed it; otherwise install
      unless an EXTERNAL k8gb is present (platform-provided — use ``never`` then).
    - ``never``: never render (k8gb is a prerequisite)."""
    if mode == "always":
        return True
    if mode == "auto":
        return operator_installed or not external_k8gb_present
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
                # Do NOT wait for chart readiness: the k8gb operator only becomes
                # healthy AFTER its LoadBalancer/CoreDNS come up, which routinely
                # exceeds helm's wait timeout -> the Release reports state=failed
                # (Ready=False) even though the operator is Running. Because Gslb
                # creation is gated on this Release being Ready, a false-failed
                # wait permanently blocks the Gslb. Crossplane observes the
                # underlying resources' health independently, so wait is
                # unnecessary here.
                "wait": False,
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
    # external-dns provider wiring (all values come from the claim / config, so
    # no DNS zone or domain is ever hardcoded here). dnsProvider selects the
    # external-dns provider ("aws" for Route53); domainFilters are derived from
    # the configured dnsZones' parent zones; credentials are read from a
    # user-supplied secret. When dnsProvider is empty the extdns block stays
    # provider-agnostic (unchanged behaviour).
    dns_provider = k8gb.get("dnsProvider", "")
    extdns_secret = k8gb.get("extdnsCredentialsSecretName", "")
    domain_filters = [z["parentZone"] for z in dns_zones if z.get("parentZone")]
    lb_annotations = _lb_annotations(identity.get("provider", ""))

    # Ingress controller as a schedulable Deployment behind a cloud LoadBalancer
    # (NOT a hostNetwork DaemonSet — that fails to schedule on shared/managed
    # clusters where node ports 80/443 are taken). Gives the Gslb-managed
    # ingress a real external address.
    nginx = _release(
        _NGINX, namespace, helm_provider_config,
        {"name": "ingress-nginx",
         "repository": "https://kubernetes.github.io/ingress-nginx",
         "version": "4.0.15"},
        "k8gb",
        {
            "controller": {
                "admissionWebhooks": {"enabled": False, "patch": {"enabled": False}},
                "kind": "Deployment",
                "replicaCount": 1,
                "service": {"type": "LoadBalancer", "annotations": lb_annotations},
            },
        },
    )

    # CoreDNS exposed via a cloud LoadBalancer. With serviceType LoadBalancer
    # k8gb discovers the cluster's external IPs from the CoreDNS service itself
    # (k8gb.io/address_discovery) — so no `k8gb.io/ip-source=true` ingress is
    # required and the operator does not crash on bootstrap. external-dns then
    # publishes the CoreDNS LB as the NS target for the load-balanced zone.
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
            "extdns": _extdns_values(geo, dns_provider, extdns_secret,
                                     domain_filters, identity.get("region", "")),
            # coredns is a TOP-LEVEL chart value (sibling of k8gb/extdns), NOT
            # nested under k8gb — the chart's values schema rejects k8gb.coredns.
            "coredns": {
                "serviceType": "LoadBalancer",
                "service": {"annotations": lb_annotations},
            },
        },
    )

    return [(_NGINX, nginx), (_K8GB, k8gb_rel)]


def _lb_annotations(provider: str) -> dict:
    """Cloud-specific Service annotations to get an L4 LoadBalancer. AWS gets an
    NLB; other clouds use their default LoadBalancer (no annotation needed)."""
    if provider == "aws":
        return {"service.beta.kubernetes.io/aws-load-balancer-type": "nlb"}
    return {}


def _extdns_values(geo: str, dns_provider: str, secret_name: str,
                   domain_filters: list, region: str) -> dict:
    """external-dns (k8gb ``extdns`` subchart) values. Base config is
    provider-agnostic; when ``dns_provider`` is set we add the provider, the
    domainFilters (derived from the claim's dnsZones), and — for aws — the
    Route53 credentials as env vars sourced from the user-supplied secret."""
    values = {
        "enabled": True,
        "interval": "20s",
        "labelFilter": "k8gb.absa.oss/dnstype=extdns",
        "logLevel": "debug",
        "policy": "sync",
        "txtOwnerId": f"k8gb-{geo}",
        "txtPrefix": f"k8gb-{geo}-",
    }
    if not dns_provider:
        return values
    values["provider"] = {"name": dns_provider}
    if domain_filters:
        values["domainFilters"] = domain_filters
    if dns_provider == "aws" and secret_name:
        env = []
        if region:
            env.append({"name": "AWS_DEFAULT_REGION", "value": region})
        env.append({"name": "AWS_ACCESS_KEY_ID", "valueFrom": {
            "secretKeyRef": {"name": secret_name, "key": "access_key_id"}}})
        env.append({"name": "AWS_SECRET_ACCESS_KEY", "valueFrom": {
            "secretKeyRef": {"name": secret_name, "key": "secret_access_key"}}})
        # Session token is only present for temporary creds; mark optional so
        # long-lived IAM keys (no token) don't fail the env mount.
        env.append({"name": "AWS_SESSION_TOKEN", "valueFrom": {
            "secretKeyRef": {"name": secret_name, "key": "session_token",
                             "optional": True}}})
        values["env"] = env
    return values
