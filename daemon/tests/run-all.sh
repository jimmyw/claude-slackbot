#!/usr/bin/env bash
# Run the daemon test suites.
#
#   ./tests/run-all.sh            # everything, including the real-Claude e2e
#   ./tests/run-all.sh --offline   # skip the e2e (no API usage, no auth needed)
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

PY=.venv/bin/python
[[ -x "$PY" ]] || { echo "no venv — run: python3 -m venv .venv && .venv/bin/pip install -e ." >&2; exit 1; }

SUITES=(tests.test_approvals tests.test_render tests.test_vmctl)
if [[ "${1:-}" != "--offline" ]]; then
    SUITES+=(tests.test_gate_e2e)
else
    echo "(skipping the end-to-end suite)"
fi

failed=()
for suite in "${SUITES[@]}"; do
    echo
    echo "=============================================================="
    echo " $suite"
    echo "=============================================================="
    if ! "$PY" -m "$suite"; then
        failed+=("$suite")
    fi
done

echo
if (( ${#failed[@]} )); then
    printf 'FAILED: %s\n' "${failed[*]}" >&2
    exit 1
fi
echo "all suites passed"
