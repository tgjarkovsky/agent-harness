import pytest

from harness import quota


@pytest.fixture(autouse=True)
def no_live_quota(tmp_path, monkeypatch):
    """Tests must never read the owner's OAuth token or call the real quota endpoint."""
    monkeypatch.setattr(quota, "CREDENTIALS", tmp_path / "no-credentials.json")
    monkeypatch.setattr(quota, "KEYCHAIN_SERVICE", "harness-test-no-such-item")  # macOS fallback
    monkeypatch.setattr(quota, "_cache", {"at": 0.0, "value": None})
