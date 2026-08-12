#!/usr/bin/env bash
# Run exactly what CI runs, and fail the way CI fails.
#
# This exists because of a specific mistake, twice: verifying with
# `pytest -q | tail -2` or `ruff check . && echo ok` inside a longer chain.
# A pipeline reports the *last* command's status, so piping to tail hides a
# failure completely, and chaining with `;` lets a commit proceed after one.
# Both slipped a broken commit past me.
#
#   bash scripts/check.sh
#
# Exits non-zero if anything fails, with nothing between the tools and the
# exit code.
set -uo pipefail

failed=0

echo "== ruff =="
if ruff check .; then
  echo "   ok"
else
  echo "   FAILED"
  failed=1
fi

echo
echo "== pytest =="
if python -m pytest tests -q; then
  echo "   ok"
else
  echo "   FAILED"
  failed=1
fi

echo
if [ "$failed" -eq 0 ]; then
  echo "All checks passed."
else
  echo "CHECKS FAILED -- do not commit."
fi
exit "$failed"
