import json

from harness import quota

RESPONSE = {"five_hour": {"utilization": 49.0, "resets_at": "2026-10-09T17:09:59+00:00"},
            "seven_day": {"utilization": 30.0, "resets_at": "2026-10-12T14:59:59+00:00"}, "seven_day_opus": None}


def credentials(expires_at_ms: float) -> None:
    quota.CREDENTIALS.write_text(json.dumps({"claudeAiOauth": {"accessToken": "tok", "refreshToken": "r",
                                                               "expiresAt": expires_at_ms}}), encoding="utf-8")


def test_parse_converts_percent_and_iso_reset():
    q = quota.parse(RESPONSE)
    assert q["five_hour"] == {"type": "five_hour", "status": "live", "utilization": 0.49, "resets_at": 1791565799.0}
    assert q["seven_day"]["utilization"] == 0.3
    assert quota.parse({"five_hour": None, "seven_day": {"utilization": None}}) is None


def test_fetch_uses_token_and_caches():
    credentials(2_000_000 * 1000)
    seen = []
    get = lambda token: seen.append(token) or RESPONSE
    assert quota.fetch(now=lambda: 1000, get=get)["five_hour"]["utilization"] == 0.49
    quota.fetch(now=lambda: 1000 + quota.TTL - 1, get=get)
    assert seen == ["tok"]
    quota.fetch(now=lambda: 1000 + quota.TTL, get=get)
    assert seen == ["tok", "tok"]


def test_expired_or_missing_token_never_calls_endpoint():
    get = lambda token: (_ for _ in ()).throw(AssertionError("must not call"))
    assert quota.fetch(now=lambda: 1000, get=get) is None  # no credentials file
    credentials(999 * 1000)
    assert quota.fetch(now=lambda: 2000, get=get) is None  # expired: no refresh, just fall back


def test_endpoint_failure_returns_none_and_is_cached():
    credentials(2_000_000 * 1000)
    calls = []

    def get(token):
        calls.append(token)
        raise OSError("boom")
    assert quota.fetch(now=lambda: 1000, get=get) is None
    assert quota.fetch(now=lambda: 1001, get=get) is None and len(calls) == 1


def test_macos_falls_back_to_keychain(monkeypatch):
    import subprocess
    oauth = json.dumps({"claudeAiOauth": {"accessToken": "kc", "expiresAt": 2_000_000 * 1000}})
    seen = []
    monkeypatch.setattr(quota.sys, "platform", "darwin")
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: seen.append(cmd) or
                        subprocess.CompletedProcess(cmd, 0, oauth, ""))
    assert quota._token(1000) == "kc"
    assert seen[0][:2] == ["security", "find-generic-password"]
    monkeypatch.setattr(quota.sys, "platform", "win32")
    assert quota._token(1000) is None  # elsewhere only the credentials file counts
