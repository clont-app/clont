#!/usr/bin/env bash
# publish.sh — build and publish the `clont` package to PyPI.
#
# Usage:
#   ./publish.sh                     Build and upload to PyPI (pypi.org)
#   ./publish.sh --test              Build and upload to TestPyPI (test.pypi.org)
#   ./publish.sh --no-build          Upload the existing dist/ without rebuilding
#   ./publish.sh --token-file PATH   Read the API token from PATH
#
# The token is read from a FILE, never from argv: command lines are visible to
# every user on the box via `ps`. Default path is $PYPI_TOKEN_FILE, falling back
# to /dev/shm/pypi-token (tmpfs). Unlock it before, lock it after:
#
#   /root/.secrets/bin/secret-unlock.sh pypi 5
#   ./publish.sh
#   /root/.secrets/bin/secret-lock.sh pypi
#
# Build/upload tooling is run through `uv` (uv build / uvx twine), so nothing is
# installed into the project venv.
set -euo pipefail

DO_BUILD=1
REPO_ARGS=()
REPO_LABEL="PyPI (pypi.org)"
TOKEN_FILE="${PYPI_TOKEN_FILE:-/dev/shm/pypi-token}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --test)
      REPO_ARGS=(--repository-url https://test.pypi.org/legacy/)
      REPO_LABEL="TestPyPI (test.pypi.org)"
      shift ;;
    --no-build) DO_BUILD=0; shift ;;
    --token-file)
      TOKEN_FILE="${2:?--token-file needs a path}"; shift 2 ;;
    -h|--help)
      sed -n '2,18p' "$0" | sed 's/^# \?//'
      exit 0 ;;
    *) echo "error: unexpected argument: $1" >&2; exit 2 ;;
  esac
done

cd "$(dirname "$0")"

UV="$(command -v uv || echo /root/.local/bin/uv)"
[[ -x "$UV" ]] || { echo "error: uv not found (needed to build and upload)." >&2; exit 1; }

if [[ ! -r "$TOKEN_FILE" ]]; then
  echo "error: no readable token file at ${TOKEN_FILE}." >&2
  echo "hint: /root/.secrets/bin/secret-unlock.sh pypi 5" >&2
  exit 2
fi

if [[ "$DO_BUILD" -eq 1 ]]; then
  echo ">> building sdist + wheel"
  rm -rf dist
  "$UV" build --out-dir dist
fi

if ! compgen -G "dist/*" >/dev/null; then
  echo "error: dist/ is empty — nothing to upload (drop --no-build to build first)." >&2
  exit 1
fi

echo ">> validating artifacts"
"$UV" tool run twine check dist/*

echo ">> uploading to ${REPO_LABEL}"
TWINE_USERNAME="__token__" TWINE_PASSWORD="$(<"$TOKEN_FILE")" \
  "$UV" tool run twine upload "${REPO_ARGS[@]+"${REPO_ARGS[@]}"}" dist/*

echo ">> done."
