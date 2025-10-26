import sys

import pytest

import napcron


@pytest.mark.parametrize(
    "platform, helper_name, helper_value, expected",
    [
        ("linux", "_linux_ac_online", True, True),
        ("linux", "_linux_ac_online", False, False),
        ("linux", "_linux_ac_online", None, False),
        ("darwin", "_macos_ac_online", True, True),
        ("darwin", "_macos_ac_online", None, False),
        ("win32", "_windows_ac_online", True, True),
        ("win32", "_windows_ac_online", False, False),
        ("sunos", None, None, False),
    ],
)
def test_req_ac_power(monkeypatch, platform, helper_name, helper_value, expected):
    monkeypatch.setattr(sys, "platform", platform)

    called = {"name": helper_name, "count": 0}

    def _fake_helper():
        called["count"] += 1
        return helper_value

    if helper_name:
        monkeypatch.setattr(napcron, helper_name, _fake_helper)

    assert napcron.req_ac_power("noop") is expected
    if helper_name:
        assert called["count"] == 1
