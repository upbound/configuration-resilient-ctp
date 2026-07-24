"""Direct, read-only cloud lookups of a heartbeat tag/label.

Why this exists (see docs/SPEC.md §Gotchas/3): a peer's heartbeat lives in an
external cloud resource, and the only way it reaches this control plane through
the normal path is a provider *observe poll* (upjet default ``--poll=10m``).
That poll gates peer freshness far above the freshness TTL and causes
split-brain. Writing the heartbeat is fine (event-driven on the MR spec change,
so the cloud value is fresh within ~``writeThrottleSeconds``); only *reading a
peer's* value is slow.

This module lets the composition function read the peer's tag DIRECTLY from the
cloud API, bypassing the polled Observe MR. It is deliberately:

- **read-only** — never mutates cloud state, so it does not violate the
  "composition functions are pure transforms" contract the way a write would;
- **dependency-injectable** — every reader accepts a ``client`` so it can be
  unit-tested offline with a fake, and the cloud SDKs are imported lazily so
  importing this module never requires boto3/azure/google installed;
- **fail-safe by contract** — a reader returns the tag value (``str``) when the
  resource is reachable and carries the tag, ``None`` when the resource is
  reachable but the tag is absent, and raises :class:`CloudReadError` when the
  value could not be read at all (auth/network/throttle/not-found). The caller
  MUST map :class:`CloudReadError` to "peer state unknown -> do NOT promote";
  never to "peer down". A transient read failure must not trigger failover.

Credentials are the caller's responsibility (wired into the function via a
DeploymentRuntimeConfig secret mount / ambient cloud identity). The readers use
the SDK's default credential resolution unless an explicit ``client`` is
injected.
"""

import os
import threading
from dataclasses import dataclass

# Default per-call budget. The reader runs inside the composition function's
# gRPC deadline, so keep cloud calls short and let the caller fall back to the
# prior role on a miss rather than block/fail the reconcile.
#
# 10s (not 2s): a CROSS-CLOUD read — e.g. an Azure-hosted function calling AWS
# SSM in another region, or the first (cold) call that also pays an AAD/OIDC
# token fetch — routinely exceeds 2s, raising CloudReadError -> the election
# treats the peer as unreadable -> fail-safe -> no leader ever elected. Clients
# are cached across reconciles so steady-state reads stay sub-second; the wider
# budget only covers the cold/cross-cloud first call.
DEFAULT_TIMEOUT_SECONDS = 10.0

# Clients (and the Azure AAD token they cache internally) are reused across
# reconciles. The composition function is a long-lived gRPC server, so a
# module-level cache keyed by scope keeps steady-state reads sub-second and
# avoids re-fetching an AAD token on every call. Injected clients bypass this.
#
# Thread-safety: ``heartbeat.read_peer_direct`` is fanned out across a thread
# pool (WS-3), so multiple threads hit this cache concurrently. ``_CLIENT_LOCK``
# guards the get-or-create in ``_cached_client`` so the cache is never torn and
# at most one client is built per scope.
_CLIENT_CACHE: dict = {}
_CLIENT_LOCK = threading.Lock()


def _cached_client(cache_key, factory):
    """Return the SDK client cached under ``cache_key``, building it once via
    ``factory()`` on a miss. Thread-safe (double-checked under ``_CLIENT_LOCK``)
    so concurrent ``read_peer_direct`` fan-out never corrupts the cache or builds
    N clients for the same scope. ``factory`` runs under the lock, which only
    serializes the rare cold build; steady-state hits take the lock-free path."""
    client = _CLIENT_CACHE.get(cache_key)
    if client is not None:
        return client
    with _CLIENT_LOCK:
        client = _CLIENT_CACHE.get(cache_key)
        if client is None:
            client = factory()
            _CLIENT_CACHE[cache_key] = client
    return client


class CloudReadError(Exception):
    """Raised when a heartbeat tag could not be read (auth, network, throttle,
    resource-not-found, or any SDK error). The caller treats this as
    "peer liveness unknown" and must NOT interpret it as "peer down"."""


@dataclass
class _AwsTarget:
    resource_name: str
    region: str


def read_resource_tag(
    provider: str,
    resource_name: str,
    tag_key: str,
    *,
    region: str = "",
    project: str = "",
    subscription_id: str = "",
    credential=None,
    client=None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
):
    """Read a single tag/label value off one cloud resource.

    Args:
        provider: ``"aws"`` | ``"azure"`` | ``"gcp"``.
        resource_name: cloud name of the heartbeat resource — for us the
            deterministic external-name ``recon-heartbeat-<cp_id>`` (AWS SSM
            Parameter name, Azure Resource Group name, GCP bucket name).
        tag_key: the tag/label key to read (e.g. the timestamp key).
        region: AWS region (required for aws) / GCP location (unused for read).
        project: GCP project id (required for gcp).
        subscription_id: Azure subscription id (required for azure).
        credential: optional Azure ``TokenCredential`` (else ``DefaultAzureCredential``).
        client: optional pre-built SDK client — injected in tests; when set,
            ``region``/``project``/``subscription_id``/``credential`` are ignored.
        timeout: per-call budget in seconds (best-effort; applied to AWS).

    Returns:
        The tag value as ``str`` if present, or ``None`` if the resource is
        reachable but does not carry ``tag_key``.

    Raises:
        CloudReadError: if the value could not be read (fail-safe -> unknown).
    """
    tags = read_resource_tags(
        provider, resource_name, region=region, project=project,
        subscription_id=subscription_id, credential=credential, client=client,
        timeout=timeout,
    )
    return tags.get(tag_key)


def read_resource_tags(
    provider: str,
    resource_name: str,
    *,
    region: str = "",
    project: str = "",
    subscription_id: str = "",
    credential=None,
    client=None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict:
    """Read ALL tags/labels off one cloud resource in a single API call.

    Same contract as :func:`read_resource_tag` but returns the whole
    ``{key: value}`` dict (empty if the resource carries none). Preferred when
    the caller needs several keys (e.g. timestamp AND role) so the peer is read
    once per reconcile. Raises :class:`CloudReadError` if unreadable.
    """
    if provider == "aws":
        return _aws_read_tags(resource_name, region, client, timeout)
    if provider == "azure":
        return _azure_read_tags(resource_name, subscription_id, credential,
                                client, timeout)
    if provider == "gcp":
        return _gcp_read_labels(resource_name, project, client, timeout)
    raise CloudReadError(f"direct tag read not supported for provider '{provider}'")


def _aws_read_tags(resource_name, region, client, timeout):
    """Read all of an SSM Parameter's tags via ``ListTagsForResource``."""
    try:
        if client is None:
            cache_key = ("aws-ssm", region, timeout)

            def _factory():
                import boto3  # lazy: only needed for a live aws read
                from botocore.config import Config
                cfg = Config(connect_timeout=timeout, read_timeout=timeout,
                             retries={"max_attempts": 1})
                return boto3.client("ssm", region_name=region, config=cfg)

            client = _cached_client(cache_key, _factory)
        resp = client.list_tags_for_resource(
            ResourceType="Parameter", ResourceId=resource_name,
        )
        tags = {t["Key"]: t["Value"] for t in resp.get("TagList", [])}
    except Exception as e:  # any SDK/network/auth error -> unknown, fail-safe
        raise CloudReadError(f"aws ssm read of '{resource_name}' failed: {e}") from e
    return tags


def _azure_read_tags(resource_name, subscription_id, credential, client,
                     timeout=DEFAULT_TIMEOUT_SECONDS):
    """Read a Resource Group's tag via the resource-manager client. The client
    caches its AAD token internally, so caching the client across reconciles
    keeps steady-state reads fast (the first call pays the token fetch).

    The subscription is NOT an API input — it comes from the credentials, like
    the AWS account and GCP project. If not explicitly provided it is read from
    the standard ``AZURE_SUBSCRIPTION_ID`` env var, which the control plane sets
    from the mounted Azure creds secret."""
    try:
        if client is None:
            subscription_id = subscription_id or os.environ.get(
                "AZURE_SUBSCRIPTION_ID", "")
            cache_key = ("azure-rm", subscription_id)

            def _factory():
                from azure.identity import DefaultAzureCredential
                try:  # azure-mgmt-resource >=26 moved the client under .resources
                    from azure.mgmt.resource.resources import ResourceManagementClient
                except ImportError:
                    from azure.mgmt.resource import ResourceManagementClient
                cred = credential if credential is not None else DefaultAzureCredential()
                # retry_total=0: one attempt, so a slow read costs ~=timeout, not
                # timeout x retries (matches AWS retries.max_attempts=1). Honored
                # by azure-core's RetryPolicy configured from this kwarg.
                return ResourceManagementClient(cred, subscription_id,
                                                retry_total=0)

            client = _cached_client(cache_key, _factory)
        # Bounding the call in azure-core 1.41.0 / azure-mgmt-resource 23.2.0:
        #   * timeout=            -> RetryPolicy pops it as the overall retry
        #                            budget and clamps the per-attempt CONNECT
        #                            timeout (policies/_retry.py:123,371);
        #   * connection_timeout/ -> popped and honored DIRECTLY by the requests
        #     read_timeout           transport (transport/_requests_basic.py:
        #                            371,379), so they also bound the READ phase
        #                            a bare timeout= alone does NOT cover.
        # All three are set so a slow/hung read is bounded by ~=timeout. See the
        # WS-2 Azure-timeout finding.
        group = client.resource_groups.get(
            resource_name, timeout=timeout,
            connection_timeout=timeout, read_timeout=timeout,
        )
        tags = group.tags or {}
    except Exception as e:
        raise CloudReadError(
            f"azure resource-group read of '{resource_name}' failed: {e}"
        ) from e
    return tags


def _gcp_read_labels(resource_name, project, client,
                     timeout=DEFAULT_TIMEOUT_SECONDS):
    """Read a Cloud Storage bucket's label via ``get_bucket``. ``project`` is
    optional — like the AWS account/GCP project generally, it defaults from the
    credentials (ADC / GOOGLE_CLOUD_PROJECT) when not given."""
    try:
        if client is None:
            cache_key = ("gcp-storage", project)

            def _factory():
                from google.cloud import storage  # lazy
                return storage.Client(project=project or None)

            client = _cached_client(cache_key, _factory)
        # timeout bounds the call; retry=None disables google-api-core's default
        # retry so a slow read costs ~=timeout, not timeout x retries (matches
        # AWS retries.max_attempts=1). storage.Client has no constructor retry
        # knob, so retries are disabled per-call here.
        bucket = client.get_bucket(resource_name, timeout=timeout, retry=None)
        labels = bucket.labels or {}
    except Exception as e:
        raise CloudReadError(
            f"gcp bucket read of '{resource_name}' failed: {e}"
        ) from e
    return labels
