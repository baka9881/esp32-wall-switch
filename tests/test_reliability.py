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


# --- Self-recovery (2026-11-13 outage: ESP32 dropped off Wi-Fi and stayed off) --

def _function_body(source, name):
    start = source.index(name)
    return source[start:source.index("\n}\n", start)]


def test_firmware_restarts_when_wifi_stays_down():
    source = FIRMWARE.read_text(encoding="utf-8")
    limit = int(re.search(r"OfflineRestartMs = (\d+);", source).group(1))
    assert 60_000 <= limit <= 180_000  # Telegram hint promises recovery within ~3 minutes.
    watch = _function_body(source, "void watchConnection()")
    assert "ESP.restart()" in watch
    assert "!apActive" in watch  # never reboot while the setup hotspot is in use
    assert "watchConnection();" in _function_body(source, "void loop()")


def test_firmware_loop_watchdog_is_fed():
    source = FIRMWARE.read_text(encoding="utf-8")
    assert "esp_task_wdt_add(nullptr);" in _function_body(source, "void setup()")
    assert "esp_task_wdt_reset();" in _function_body(source, "void loop()")
    timeout = int(re.search(r"LoopWatchdogMs = (\d+);", source).group(1))
    assert timeout >= 15_000  # above the 5 s HTTP read plus the 0.75 s servo move


def test_firmware_health_markup_matches_plugin(plugin):
    """The firmware's health line must stay parseable by the plugin."""
    mod, _ = plugin
    source = FIRMWARE.read_text(encoding="utf-8")
    template = re.search(r'"(<p class=\'health\'[^"]+)"', source).group(1)
    sample = template.replace("%lu", "7380", 1).replace("%s", "brownout").replace("%lu", "2")
    text = mod["_health_text"](sample.encode())
    assert text == "已連續運作 2 小時 3 分，上次啟動原因：電壓不足，Wi‑Fi 重連 2 次。"
    for reason in re.findall(r'return "([a-z]+)";', _function_body(source, "const char *resetReasonName()")):
        assert reason == "other" or reason in mod["_RESET_REASONS"]


def test_status_reports_health(plugin, monkeypatch):
    mod, settings = plugin
    page = PAGE + b"<p class='health' data-uptime='300' data-reset='restart' data-rejoins='0'></p>"
    _fake_session(monkeypatch, [FakeResponse(200, page)], [])
    reply = mod["_switch_request"]("status", settings)
    assert "已連續運作 5 分鐘" in reply and "斷線自動重開" in reply


def _fake_ping(monkeypatch, mod, stdout):
    calls = []

    def run(command, **kw):
        calls.append(command)
        return types.SimpleNamespace(stdout=stdout, returncode=0)

    monkeypatch.setattr(mod["subprocess"], "run", run)
    return calls


def test_unreachable_esp32_but_router_ok_mentions_self_restart(plugin, monkeypatch):
    mod, settings = plugin
    _fake_session(monkeypatch, [requests.ConnectionError("offline")] * 10, [])
    pings = _fake_ping(monkeypatch, mod, b"Reply from 192.168.1.1: bytes=32 time=3ms TTL=64")
    reply = mod["_switch_request"]("status", settings)
    assert "自己重開" in reply and "VPN" not in reply
    assert pings[0][-1] == "192.168.1.1"


def test_unreachable_router_blames_this_computer(plugin, monkeypatch):
    """2026-11-13: Proton VPN blocked the LAN; the reply must say so, not blame the ESP32."""
    mod, settings = plugin
    _fake_session(monkeypatch, [requests.ConnectionError("offline")] * 10, [])
    # Windows prints "unreachable" replies without TTL; that must count as no route.
    _fake_ping(monkeypatch, mod, b"Reply from 192.168.1.102: Destination host unreachable.")
    reply = mod["_switch_request"]("status", settings)
    assert "電腦" in reply and "VPN" in reply
