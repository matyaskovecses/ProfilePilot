import os

import pytest

# Never touch the real OS keyring / Credential Manager from tests.
os.environ.setdefault("PROFILEPILOT_SECRETS", "file")


@pytest.fixture
def store(tmp_path):
    from profilepilot.store import Store

    return Store(tmp_path / "pp-home")


@pytest.fixture(autouse=True)
def _no_real_browser_data(monkeypatch):
    """Never read the addresses saved in the user's own Chrome / Edge / Brave: tests that need
    browser-saved data point :func:`profilepilot.chrome_autofill._user_data_dirs` at fixtures."""
    from profilepilot import chrome_autofill

    monkeypatch.setattr(chrome_autofill, "_user_data_dirs", lambda: {})
