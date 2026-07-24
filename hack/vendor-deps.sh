#!/usr/bin/env sh
# Vendor the directApi cloud SDKs (boto3/azure/google) into the function tree so
# they land in the built function image. WHY: `up project build` (the Upbound
# embedded-Python DevEx) does NOT install functions/*/requirements.txt third-party
# deps into the image (Pylon #900). main.py appends vendor/lib/pythonX.Y/site-packages
# to sys.path so cloud_read.py's lazy imports resolve; without this, directApi peer
# reads raise ModuleNotFoundError -> CloudReadError -> every peer "unreadable" ->
# the election fail-safe pins standbys forever, silently.
#
# Build for the RUNTIME arch/python (linux/amd64 + cp311 today). Re-run when the
# embedded runtime's python version changes and update the sys.path shim to match.
set -eu
FN="$(CDPATH= cd "$(dirname "$0")/../functions/resilientcontrolplane" && pwd)"
PYVER="${PYVER:-3.11}"
rm -rf "$FN/vendor"
pip install -r "$FN/requirements.txt" \
  --platform manylinux2014_x86_64 --only-binary=:all: --python-version "$PYVER" \
  --target "$FN/vendor/lib/python${PYVER}/site-packages"
echo "vendored into $FN/vendor (python ${PYVER}, linux/amd64)"
