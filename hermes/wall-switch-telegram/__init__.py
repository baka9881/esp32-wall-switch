import ipaddress
import json
from pathlib import Path
import re
from threading import Lock

from dotenv import dotenv_values
from gateway.platforms.event import MessageType
import requests
from requests.auth import HTTPDigestAuth


_ACTIONS = {
    "on": "on",
    "off": "off",
    "status": "status",
    "開燈": "on",
    "關燈": "off",
    "狀態": "status",
}
_PLAIN_ACTIONS = {
    "開燈": "on",
    "开灯": "on",
    "打開燈": "on",
    "打开灯": "on",
    "把燈打開": "on",
    "把灯打开": "on",
    "關燈": "off",
    "关灯": "off",
    "關掉燈": "off",
    "关掉灯": "off",
    "把燈關掉": "off",
    "把灯关掉": "off",
    "狀態": "status",
    "状态": "status",
    "電燈狀態": "status",
    "电灯状态": "status",
}
_PLAIN_REQUEST = re.compile(
    r"(?:(?:請|请|麻煩|麻烦)你?[ \t]*)?"
    r"(?:(?:幫我|帮我)[ \t]*)?"
    r"(?P<phrase>[^\s。.!！]+)[ \t]*[。.!！]?"
)
_PRIVATE_NETWORKS = tuple(
    ipaddress.IPv4Network(network)
    for network in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)
_SETTINGS = Path(__file__).with_name("settings.json")
_NONCE = re.compile(rb"name='nonce' value='([0-9a-f]{32})'")
_PASSWORD = re.compile(r"^設定密碼　([0-9a-f]{24})$", re.MULTILINE)
_GET_RETRYABLE = (
    requests.ConnectionError,
    requests.Timeout,
    requests.exceptions.ChunkedEncodingError,
)
_CONTROL_LOCK = Lock()
_DENIED = "請用已授權的 Telegram 帳號，在和機器人的私人聊天操作。"


def _read_settings():
    settings = json.loads(_SETTINGS.read_text(encoding="utf-8-sig"))
    if not all(isinstance(settings[key], str) and settings[key] for key in (
        "address", "credential_file", "telegram_env_file", "telegram_profile"
    )):
        raise ValueError("Invalid wall switch settings")
    return settings


def _allowed_sender(source, settings):
    env_file = Path(settings["telegram_env_file"])
    if not env_file.is_absolute():
        return False
    authorization_home = getattr(source, "_authorization_profile_home", None)
    if authorization_home is not None and Path(authorization_home).resolve() != env_file.parent.resolve():
        return False
    profile = source.profile
    if profile is None:
        from hermes_constants import get_hermes_home

        if env_file.parent.resolve() != get_hermes_home().resolve():
            return False
    elif profile != settings["telegram_profile"]:
        return False
    allowlist = dotenv_values(env_file, encoding="utf-8-sig", interpolate=False)
    allowed_ids = (allowlist.get("TELEGRAM_ALLOWED_USERS") or "").split(",")
    return str(source.user_id) in {user_id.strip() for user_id in allowed_ids}


def _switch_request(action, settings):
    address = ipaddress.IPv4Address(settings["address"])
    if not any(address in network for network in _PRIVATE_NETWORKS):
        raise ValueError("Expected a private IPv4 address")
    credential_file = Path(settings["credential_file"])
    if not credential_file.is_absolute():
        raise ValueError("Expected an absolute credential path")
    password = _PASSWORD.search(credential_file.read_text(encoding="utf-8-sig"))
    if not password:
        raise ValueError("Missing device credential")
    url = f"http://{address}"
    timeout = (2, 5)

    with _CONTROL_LOCK, requests.Session() as session:
        session.trust_env = False
        session.auth = HTTPDigestAuth("light", password.group(1))
        for attempt in range(2):
            try:
                page = session.get(f"{url}/", timeout=timeout, allow_redirects=False)
                break
            except requests.RequestException as error:
                if attempt == 1 or not isinstance(error, _GET_RETRYABLE):
                    return "目前連不到 ESP32 控制頁。請確認 ESP32 已連上家裡的 Wi‑Fi。"
        if page.status_code == 401:
            return "ESP32 拒絕登入。請確認電燈控制頁的設定密碼。"
        nonce = _NONCE.search(page.content)
        controls = (b"action='/move'", b"name='side' value='on'", b"name='side' value='off'")
        if page.status_code != 200 or not nonce or not all(marker in page.content for marker in controls):
            return f"ESP32 未提供電燈控制頁（HTTP {page.status_code}）。"
        if action == "status":
            return "ESP32 控制頁可以連線。裝置沒有燈況感測器。"

        request = session.prepare_request(requests.Request(
            "POST", f"{url}/move", data={"side": action, "nonce": nonce.group(1).decode("ascii")}
        ))
        if not request.headers.get("Authorization", "").startswith("Digest "):
            return "ESP32 控制頁未完成 Digest 驗證。"
        # The GET already authenticated. Do not let the Digest 401 hook resend a motion POST.
        request.hooks["response"] = []
        try:
            result = session.send(request, timeout=timeout, allow_redirects=False)
        except requests.RequestException:
            return "指令送出後未收到確認。請先確認燈和舵機的狀態。"
        if result.status_code != 303 or result.headers.get("Location") != "/?moved=1":
            return f"ESP32 未確認完成指令（HTTP {result.status_code}）。"
    label = {"on": "開燈", "off": "關燈"}[action]
    return f"ESP32 已完成{label}的舵機指令。裝置沒有燈況感測器。"


def _is_telegram_private_sender(source):
    return (
        source is not None
        and getattr(source.platform, "value", source.platform) == "telegram"
        and source.chat_type == "dm"
        and not source.is_bot
        and re.fullmatch(r"[1-9][0-9]*", str(source.user_id)) is not None
        and str(source.chat_id) == str(source.user_id)
    )


def _rewrite_plain_text(event, **kwargs):
    if (
        event.message_type is not MessageType.TEXT
        or event.internal
        or not event.allow_gateway_control
        or event.media_urls
        or event.edit_revision is not None
        or getattr(event.raw_message, "forward_origin", None) is not None
        or not _is_telegram_private_sender(event.source)
    ):
        return None
    match = _PLAIN_REQUEST.fullmatch(event.text.strip())
    action = _PLAIN_ACTIONS.get(match["phrase"]) if match else None
    if action is None:
        return None
    try:
        if _allowed_sender(event.source, _read_settings()):
            return {"action": "rewrite", "text": f"/light {action}"}
    except (OSError, RuntimeError, ValueError, KeyError, TypeError):
        return None
    return None


def light_command(raw_args, *, source=None):
    if not _is_telegram_private_sender(source):
        return _DENIED
    try:
        settings = _read_settings()
        if not _allowed_sender(source, settings):
            return _DENIED
        action = _ACTIONS.get(raw_args.strip().lower())
        if action is None:
            return "用法 /light on、/light off 或 /light status。也可以用 開燈、關燈、狀態。"
        return _switch_request(action, settings)
    except (OSError, RuntimeError, ValueError, KeyError, TypeError):
        return "電燈控制的本機設定無法讀取。請在電腦檢查外掛設定。"


def register(ctx):
    ctx.register_command("light", light_command, "控制家中的電燈", args_hint="on|off|status")
    ctx.register_hook("pre_gateway_dispatch", _rewrite_plain_text)
