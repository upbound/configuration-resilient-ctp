#!/usr/bin/env python3
"""Test 1 e2e harness — 2x AWS control plane failover/failback.

Drives two already-provisioned EKS control planes (kubectl contexts ``use1`` and
``usw2``) that each have configuration-resilient-ctp + configuration-aws-s3
installed, and asserts:

  1. steady state:  use1 = leader (["*"]), usw2 = standby (["Observe"]),
                    the shared S3 bucket exists once.
  2. failover:      pause use1's ResilientControlPlane -> its heartbeat goes
                    stale -> usw2 promotes to leader (["*"]) within TTL+hysteresis.
  3. failback:      unpause use1 -> two-phase handoff -> use1 leader again,
                    usw2 back to standby.

Kubernetes ops go through ``kubectl`` (the kubeconfig already handles EKS auth);
AWS checks use boto3. Both are run with the shell's AWS_* env vars stripped so
the file credential (the account that owns the clusters/bucket) is used.

Usage:
    python e2e/harness.py \
        --use1-context use1 --usw2-context usw2 \
        --bucket resilient-ctp-demo-shared-bucket-use1usw2 \
        --bucket-region us-east-1 \
        --manifests e2e/manifests

Exit code 0 = all phases passed.
"""

import argparse
import json
import os
import subprocess
import sys
import time

# Strip inherited AWS_* so kubectl's exec plugin and boto3 use the file creds
# that own the clusters (see e2e/README.md — the shell env points elsewhere).
_CLEAN_ENV = {k: v for k, v in os.environ.items() if not k.startswith("AWS_")}


def _kubectl(context, *args, check=True):
    cmd = ["kubectl", "--context", context, *args]
    r = subprocess.run(cmd, capture_output=True, text=True, env=_CLEAN_ENV)
    if check and r.returncode != 0:
        raise RuntimeError(f"kubectl {' '.join(args)} [{context}] failed: {r.stderr.strip()}")
    return r.stdout.strip()


def _rcp_role(context):
    """(role, managementPolicy) of the ResilientControlPlane 'member'."""
    out = _kubectl(context, "get", "resilientcontrolplane", "member", "-n", "default",
                   "-o", "json", check=False)
    if not out:
        return None, None
    st = json.loads(out).get("status", {})
    return st.get("role"), st.get("managementPolicy")


def _log(msg):
    print(f"[harness] {msg}", flush=True)


def _wait(context, want_role, timeout, poll=10):
    """Wait until the CP's RCP reports want_role. Returns (ok, role, policy)."""
    deadline = time.time() + timeout
    role = policy = None
    while time.time() < deadline:
        role, policy = _rcp_role(context)
        if role == want_role:
            return True, role, policy
        _log(f"  {context}: role={role} policy={policy} (want {want_role})")
        time.sleep(poll)
    return False, role, policy


def _bucket_exists(bucket, region):
    import boto3
    from botocore.exceptions import ClientError
    s3 = boto3.session.Session(profile_name="default").client("s3", region_name=region)
    try:
        s3.head_bucket(Bucket=bucket)
        return True
    except ClientError as err:
        # S3 returns 404 for a missing bucket and 403 when the bucket exists
        # but the calling principal lacks direct access (it is managed by the
        # provider's principal, not the caller). For an existence check, 403
        # means "exists".
        status = int(err.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0))
        return status == 403


def apply_manifests(args):
    _log("applying member + bucket on both control planes")
    _kubectl(args.use1_context, "apply", "-f", f"{args.manifests}/resilient-use1.yaml")
    _kubectl(args.usw2_context, "apply", "-f", f"{args.manifests}/resilient-usw2.yaml")
    _kubectl(args.use1_context, "apply", "-f", f"{args.manifests}/bucket.yaml")
    _kubectl(args.usw2_context, "apply", "-f", f"{args.manifests}/bucket.yaml")


def phase_steady(args):
    _log("PHASE 1: steady state")
    ok1, r1, p1 = _wait(args.use1_context, "leader", args.steady_timeout)
    ok2, r2, p2 = _wait(args.usw2_context, "standby", args.steady_timeout)
    assert ok1 and p1 == ["*"], f"use1 expected leader/['*'], got {r1}/{p1}"
    assert ok2 and p2 == ["Observe"], f"usw2 expected standby/['Observe'], got {r2}/{p2}"
    assert _bucket_exists(args.bucket, args.bucket_region), f"bucket {args.bucket} missing"
    _log("PHASE 1 PASS: use1 leader, usw2 standby, shared bucket exists")


def phase_failover(args):
    _log("PHASE 2: failover (pause use1)")
    _kubectl(args.use1_context, "annotate", "resilientcontrolplane", "member", "-n",
             "default", "crossplane.io/paused=true", "--overwrite")
    ok, role, policy = _wait(args.usw2_context, "leader", args.failover_timeout)
    assert ok and policy == ["*"], f"usw2 expected to promote to leader/['*'], got {role}/{policy}"
    assert _bucket_exists(args.bucket, args.bucket_region), "bucket vanished during failover"
    _log("PHASE 2 PASS: usw2 promoted to leader, still owns the shared bucket")


def phase_failback(args):
    _log("PHASE 3: failback (unpause use1)")
    _kubectl(args.use1_context, "annotate", "resilientcontrolplane", "member", "-n",
             "default", "crossplane.io/paused-", "--overwrite", check=False)
    ok1, r1, p1 = _wait(args.use1_context, "leader", args.failover_timeout)
    ok2, r2, p2 = _wait(args.usw2_context, "standby", args.failover_timeout)
    assert ok1 and p1 == ["*"], f"use1 expected to reclaim leader/['*'], got {r1}/{p1}"
    assert ok2 and p2 == ["Observe"], f"usw2 expected to demote to standby, got {r2}/{p2}"
    _log("PHASE 3 PASS: failback complete (use1 leader, usw2 standby)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--use1-context", default="use1")
    ap.add_argument("--usw2-context", default="usw2")
    ap.add_argument("--bucket", default="resilient-ctp-demo-shared-bucket-use1usw2")
    ap.add_argument("--bucket-region", default="us-east-1")
    ap.add_argument("--manifests", default="e2e/manifests")
    ap.add_argument("--steady-timeout", type=int, default=600)
    ap.add_argument("--failover-timeout", type=int, default=600)
    ap.add_argument("--skip-apply", action="store_true")
    args = ap.parse_args()

    try:
        if not args.skip_apply:
            apply_manifests(args)
        phase_steady(args)
        phase_failover(args)
        phase_failback(args)
    except AssertionError as e:
        _log(f"FAIL: {e}")
        sys.exit(1)
    except Exception as e:
        _log(f"ERROR: {e}")
        sys.exit(2)
    _log("ALL PHASES PASSED ✅  Test 1 (2xAWS failover/failback) succeeded.")


if __name__ == "__main__":
    main()
