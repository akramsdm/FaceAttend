"""Run the pure kiosk state-machine tests with Node's built-in test runner.

No JavaScript dependencies are added: Node 18+ ships ``node:test``. The test is
skipped when Node is not installed so the Python suite still works in minimal
deployments.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(NODE is None, reason="Node.js is not installed")
def test_kiosk_state_machine_js() -> None:
    result = subprocess.run(
        [NODE, "--test", str(ROOT / "tests" / "js" / "signin_state.test.js")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
