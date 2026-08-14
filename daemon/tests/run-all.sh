#!/usr/bin/env bash
# Run the daemon test suites.
#
#   ./tests/run-all.sh            # everything, including the real-Claude e2e
#   ./tests/run-all.sh --offline   # skip the e2e (no API usage, no auth needed)
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

PY=.venv/bin/python
[[ -x "$PY" ]] || { echo "no venv — run: python3 -m venv .venv && .venv/bin/pip install -e ." >&2; exit 1; }

SUITES=(tests.test_approvals tests.test_render tests.test_vmctl tests.test_bridge_args tests.test_hook_paths tests.test_cloud_init tests.test_commands tests.test_bash_policy tests.test_mrkdwn)
if [[ "${1:-}" != "--offline" ]]; then
    SUITES+=(tests.test_gate_e2e)
else
    echo "(skipping the API-spending suites)"
fi

# The live-VM suite needs a provisioned, authenticated guest, so it is opt-in:
#   ./tests/run-all.sh --vm 192.168.122.x
if [[ "${1:-}" == "--vm" ]]; then
    [[ -n "${2:-}" ]] || { echo "usage: $0 --vm <vm-ip>" >&2; exit 64; }
    VM_IP="$2"
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

if [[ -n "${VM_IP:-}" ]]; then
    echo
    echo "=============================================================="
    echo " tests.test_bridge_e2e (live VM $VM_IP)"
    echo "=============================================================="
    "$PY" -m tests.test_bridge_e2e "$VM_IP" || failed+=(tests.test_bridge_e2e)
fi

echo
if (( ${#failed[@]} )); then
    printf 'FAILED: %s\n' "${failed[*]}" >&2
    exit 1
fi
echo "all suites passed"
