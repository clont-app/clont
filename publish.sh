#!/usr/bin/env bash
# publish.sh — build and publish the `clont` package to PyPI.
#
# Usage:
#   ./publish.sh <TOKEN>             Build and upload to PyPI (pypi.org)
#   ./publish.sh --test <TOKEN>      Build and upload to TestPyPI (test.pypi.org)
#   ./publish.sh --no-build <TOKEN>  Upload the existing dist/ without rebuilding
#
# <TOKEN> is your API token, e.g. "pypi-AgEI...". The upload username is always
# "__token__" and is set automatically.
#
# Note: a token passed as a CLI argument is visible to other local processes via
# `ps` and may land in your shell history. Prefer running this with a leading
# space (so it is not saved) or via Claude Code's `!` prefix.
set -euo pipefail

DO_BUILD=1
REPO_ARGS=()
REPO_LABEL="PyPI (pypi.org)"
TOKEN=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --test)
      REPO_ARGS=(--repository-url https://test.pypi.org/legacy/)
      REPO_LABEL="TestPyPI (test.pypi.org)"
      shift ;;
    --no-build) DO_BUILD=0; shift ;;
    -h|--help)
      sed -n '2,14p' "$0" | sed 's/^# \?//'
      exit 0 ;;
    -*) echo "error: unknown option: $1" >&2; exit 2 ;;
    *)
      if [[ -n "$TOKEN" ]]; then
        echo "error: unexpected extra argument: $1" >&2; exit 2
      fi
      TOKEN="$1"; shift ;;
  esac
done

if [[ -z "$TOKEN" ]]; then
  echo "error: no API token provided." >&2
  echo "usage: $0 [--test] [--no-build] <token>" >&2
  exit 2
fi

# Run from the project root (where this script lives) and use the local venv.
cd "$(dirname "$0")"
PY="./bin/python"
if [[ ! -x "$PY" ]]; then
  echo "error: venv python not found at $PY" >&2; exit 1
fi

# Make sure the tooling is present.
"$PY" -m pip show twine >/dev/null 2>&1 || "$PY" -m pip install --quiet twine
if [[ "$DO_BUILD" -eq 1 ]]; then
  "$PY" -m pip show build >/dev/null 2>&1 || "$PY" -m pip install --quiet build

  echo ">> building from a clean staging dir"
  rm -rf dist
  STAGE="$(mktemp -d)"
  trap 'rm -rf "$STAGE"' EXIT
  cp -r clont README.md LICENSE pyproject.toml "$STAGE"/
  "$PY" -m build --outdir "$PWD/dist" "$STAGE"
fi

if ! compgen -G "dist/*" >/dev/null; then
  echo "error: dist/ is empty — nothing to upload (drop --no-build to build first)." >&2
  exit 1
fi

echo ">> validating artifacts"
"$PY" -m twine check dist/*

echo ">> uploading to ${REPO_LABEL}"
TWINE_USERNAME="__token__" TWINE_PASSWORD="$TOKEN" \
  "$PY" -m twine upload "${REPO_ARGS[@]+"${REPO_ARGS[@]}"}" dist/*

echo ">> done."
