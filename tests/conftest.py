import os
import shutil

import pytest


@pytest.fixture(scope="session")
def restic_binary():
    binary = os.environ.get("GUESTVAULT_TEST_RESTIC") or shutil.which("restic")
    if not binary:
        pytest.skip("Set GUESTVAULT_TEST_RESTIC to restic >=0.19.1 for real integration tests.")
    return binary
