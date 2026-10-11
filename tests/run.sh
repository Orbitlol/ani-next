#!/usr/bin/env bash
# Run the aninext test suite. No network access and no pip packages required:
# providers are exercised against the built-in offline demo site and against
# local HTTP servers the tests start themselves.
#
#   ./tests/run.sh              all tests
#   ./tests/run.sh -v           verbose
#   ./tests/run.sh test_db      one module
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"
cd "$ROOT"

PY="${PYTHON:-python3}"
command -v "$PY" >/dev/null 2>&1 || { echo "python3 not found" >&2; exit 1; }

# keep test runs away from a real ~/.aninext
export ANINEXT_HOME="${ANINEXT_HOME:-$(mktemp -d)}"
export NO_COLOR=1
export ANINEXT_LOG=error
export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"

if [ "$#" -gt 0 ] && [ "${1#-}" = "$1" ]; then
  # a module name was given: tests.test_db or test_db
  MODULE="$1"; shift
  case "$MODULE" in
    tests.*) ;; *) MODULE="tests.${MODULE%.py}" ;;
  esac
  exec "$PY" -m unittest -v "$MODULE" "$@"
fi

exec "$PY" -m unittest discover -s tests -t . "$@"
