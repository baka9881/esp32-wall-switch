"""Regression checks for the "目前連不到 ESP32 控制頁" outage (2026-11-12).

Run:  python -m pytest tests  (needs `requests`; Hermes-only imports are stubbed)
"""
from pathlib import Path
import re
import runpy
import sys
import types

import pytest
import requests

ROOT = Path(__file__).resolve().parents[1]
FIRMWARE = ROOT / "firmware" / "WallSwitch" / "WallSwitch.ino"
PLUGIN = ROOT / "hermes" / "wall-switch-telegram" / "__init__.py"


def _load_plugin():
    for name in ("gateway", "gateway.platforms"):
        sys.modules.setdefault(name, types.ModuleType(name))
    event = types.ModuleType("gateway.platforms.event")
    event.MessageType = type("MessageType", (), {"TEXT": object()})
    sys.modules.setdefault("gateway.platforms.event", event)
    if "dotenv" not in sys.modules:
        try:
            import dotenv  # noqa: F401
        except ImportError:
            stub = types.ModuleType("dotenv")
            stub.dotenv_values = lambda *a, **k: {}
            sys.modules["dotenv"] = stub
    return runpy.run_path(str(PLUGIN))


# --- Firmware -----------------------------------------------------------------

def test_firmware_keeps_wifi_awake():
    """Wi-Fi modem sleep made the ESP32 miss Hermes requests. Must stay off."""
    source = FIRMWARE.read_text(encoding="utf-8")
    assert re.search(r"^\s*WiFi\.setSleep\(false\);", source, re.MULTILINE)
    # Must come after WiFi.mode(), otherwise the mode change resets it.
    assert source.index("WiFi.mode(WIFI_STA);\n  // Keep") < source.index("WiFi.setSleep(false);")


def test_firmware_servo_travel_matches_flashed_build():
    """2026-11-12 flash readback matched the 2026-11-10 build with these values.
    Change only after re-testing on the wall."""
    source = FIRMWARE.read_text(encoding="utf-8")
    assert "OnPulseUs = 1233;" in source
    assert "OffPulseUs = 1767;" in source


# --- Plugin -------------------------------------------------------------------

PAGE = (b"<form action='/move'><input name='nonce' value='" + b"a" * 32 +
        b"'><button name='side' value='on'></button><button name='side' value='off'></button>")


class FakeResponse:
    def __init__(self, status, content=b"", headers=None):
        self.status_code, self.content, self.headers = status, content, headers or {}


@pytest.fixture
def plugin(tmp_path, monkeypatch):
    mod = _load_plugin()
    cred = tmp_path / "cred.txt"
    cred.write_text("設定密碼　" + "0" * 24 + "\n", encoding="utf-8")
    settings = {"address": "192.168.1.114", "credential_file": str(cred),
                "telegram_env_file": str(tmp_path / ".env"), "telegram_profile": "wall-switch"}
    monkeypatch.setattr(mod["time"], "sleep", lambda s: None)
    return mod, settings


def _fake_session(monkeypatch, get_results, post_results):
    calls = {"get": 0, "post": 0}

    class Session(requests.Session):
        def get(self, url, **kw):
            calls["get"] += 1
            result = get_results.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

        def prepare_request(self, request):
            prepared = super().prepare_request(request)
            prepared.headers["Authorization"] = "Digest test"  # GET already authenticated
            return prepared

        def send(self, request, **kw):
            calls["post"] += 1
            result = post_results.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

    monkeypatch.setattr(requests, "Session", Session)
    return calls


def test_status_survives_short_wifi_drop(plugin, monkeypatch):
    mod, settings = plugin
    drop = requests.ConnectTimeout("asleep")
    calls = _fake_session(monkeypatch, [drop, drop, drop, FakeResponse(200, PAGE)], [])
    assert mod["_switch_request"]("status", settings).startswith("ESP32 控制頁可以連線")
    assert calls["get"] == 4


def test_status_gives_up_after_retries(plugin, monkeypatch):
    mod, settings = plugin
    drop = requests.ConnectionError("offline")
    calls = _fake_session(monkeypatch, [drop] * 10, [])
    assert "連不到 ESP32" in mod["_switch_request"]("status", settings)
    assert calls["get"] == mod["_GET_ATTEMPTS"]
    # Worst case must stay short enough for a Telegram reply (~15 s).
    connect_timeout = 3
    worst = mod["_GET_ATTEMPTS"] * connect_timeout + (mod["_GET_ATTEMPTS"] - 1) * mod["_GET_RETRY_DELAY"]
    assert worst <= 16


def test_motion_post_is_never_retried(plugin, monkeypatch):
    """Retrying the POST could move the servo twice and flip the light back."""
    mod, settings = plugin
    calls = _fake_session(monkeypatch, [FakeResponse(200, PAGE)],
                          [requests.ConnectTimeout("lost reply"), FakeResponse(303, headers={"Location": "/?moved=1"})])
    reply = mod["_switch_request"]("on", settings)
    assert "未收到確認" in reply
    assert calls["post"] == 1
