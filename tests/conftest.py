import os

import pytest

# Never touch the real OS keyring / Credential Manager from tests.
os.environ.setdefault("PROFILEPILOT_SECRETS", "file")


@pytest.fixture
def store(tmp_path):
    from profilepilot.store import Store

    return Store(tmp_path / "pp-home")
