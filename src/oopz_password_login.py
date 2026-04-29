"""后台 OOPZ 账号密码登录与凭据落盘。"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Optional

from logger_config import get_logger

logger = get_logger("OopzPasswordLogin")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(PROJECT_ROOT, "config.py")
CONFIG_EXAMPLE_PATH = os.path.join(PROJECT_ROOT, "config.example.py")
PRIVATE_KEY_PATH = os.path.join(PROJECT_ROOT, "private_key.py")
BROWSER_DATA_DIR = os.path.join(PROJECT_ROOT, "data", "oopz_admin_login_profile")
CHROMIUM_RUNTIME_DIR = os.path.join(PROJECT_ROOT, "data", "chromium_runtime")
OOPZ_WEB_URL = "https://web.oopz.cn/#/login"
LOGIN_RESPONSE_PATH = "/client/v1/login/v2/login"
WS_EVENT_AUTH = 253
OOPZ_CONFIG_CREDENTIAL_FIELDS = ("app_version", "device_id", "person_uid", "jwt_token")
REQUIRED_CAPTURE_FIELDS = ("person_uid", "device_id", "jwt_token", "private_key_pem")

try:
    from voice_client import _BROWSER_ARGS as _VOICE_BROWSER_ARGS
except Exception:
    _VOICE_BROWSER_ARGS = []

# 复用语音推流的 Chromium 参数，但登录页需要遵循 OOPZ/系统代理设置。
_BROWSER_ARGS = [arg for arg in _VOICE_BROWSER_ARGS if arg != "--no-proxy-server"]
for _arg in (
    "--disable-blink-features=AutomationControlled",
    "--autoplay-policy=no-user-gesture-required",
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
    "--disable-crash-reporter",
    "--disable-crashpad",
):
    if _arg not in _BROWSER_ARGS:
        _BROWSER_ARGS.append(_arg)


def _get_chromium_executable_path() -> Optional[str]:
    """读取容器或宿主机指定的 Chromium 可执行文件路径。"""
    path = os.environ.get("BOT_CHROMIUM_EXECUTABLE_PATH") or os.environ.get("CHROME_BIN")
    if not path:
        return None
    path = path.strip()
    if not path:
        return None
    if os.path.exists(path):
        return path
    logger.warning("指定的 Chromium 路径不存在，回退到 Playwright 默认浏览器: %s", path)
    return None


def _is_writable_dir(path: str) -> bool:
    try:
        return bool(path) and os.path.isdir(path) and os.access(path, os.W_OK)
    except Exception:
        return False


def _prepare_chromium_runtime() -> tuple[dict[str, str], str]:
    """给 Docker 中的 Chromium 准备可写 HOME/XDG/Crashpad 目录。"""
    home_dir = os.path.join(CHROMIUM_RUNTIME_DIR, "home")
    config_dir = os.path.join(CHROMIUM_RUNTIME_DIR, "config")
    cache_dir = os.path.join(CHROMIUM_RUNTIME_DIR, "cache")
    crash_dir = os.path.join(CHROMIUM_RUNTIME_DIR, "crashpad")
    for path in (home_dir, config_dir, cache_dir, crash_dir):
        os.makedirs(path, exist_ok=True)

    env = dict(os.environ)
    if not _is_writable_dir(env.get("HOME", "")):
        env["HOME"] = home_dir
    if not _is_writable_dir(env.get("XDG_CONFIG_HOME", "")):
        env["XDG_CONFIG_HOME"] = config_dir
    if not _is_writable_dir(env.get("XDG_CACHE_HOME", "")):
        env["XDG_CACHE_HOME"] = cache_dir
    return env, crash_dir


def _chromium_args(crash_dir: str) -> list[str]:
    args = list(_BROWSER_ARGS)
    crash_arg = f"--crash-dumps-dir={crash_dir}"
    if crash_arg not in args:
        args.append(crash_arg)
    return args


def _new_credentials() -> dict[str, Any]:
    """创建一次登录捕获所需的凭据容器。"""
    return {
        "person_uid": None,
        "device_id": None,
        "jwt_token": None,
        "private_key_pem": None,
        "app_version": None,
    }


def _missing_required_credentials(credentials: dict[str, Any]) -> list[str]:
    return [key for key in REQUIRED_CAPTURE_FIELDS if not credentials.get(key)]


class OopzPasswordLoginError(RuntimeError):
    """OOPZ 自动登录失败。"""


# 页面加载前注入：让 OOPZ Web 端生成/导入的签名私钥可导出。
JS_CRYPTO_HOOK = """
(() => {
    window.__oopz_captured_pem = null;
    window.__oopz_key_events = [];

    const _subtle = crypto.subtle;
    const _importKey   = _subtle.importKey.bind(_subtle);
    const _generateKey = _subtle.generateKey.bind(_subtle);
    const _sign        = _subtle.sign.bind(_subtle);
    const _exportKey   = _subtle.exportKey.bind(_subtle);

    async function exportAsPem(key) {
        try {
            const ab    = await _exportKey('pkcs8', key);
            const bytes = new Uint8Array(ab);
            let bin = '';
            for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
            const b64   = btoa(bin);
            const lines = b64.match(/.{1,64}/g) || [];
            return '-----BEGIN PRIVATE KEY-----\\n' + lines.join('\\n') + '\\n-----END PRIVATE KEY-----';
        } catch (e) {
            window.__oopz_key_events.push({action: 'export_failed', error: e.message});
            return null;
        }
    }

    crypto.subtle.importKey = async function(format, keyData, algorithm, extractable, keyUsages) {
        const isSignKey = keyUsages && keyUsages.includes('sign');
        if (isSignKey) extractable = true;

        const key = await _importKey(format, keyData, algorithm, extractable, keyUsages);

        if (key && key.type === 'private') {
            window.__oopz_key_events.push({action: 'importKey', format, extractable: key.extractable});
            if (!window.__oopz_captured_pem && key.extractable) {
                window.__oopz_captured_pem = await exportAsPem(key);
            }
        }
        return key;
    };

    crypto.subtle.generateKey = async function(algorithm, extractable, keyUsages) {
        const isSignKey = keyUsages && keyUsages.includes('sign');
        if (isSignKey) extractable = true;

        const result = await _generateKey(algorithm, extractable, keyUsages);
        const pk = result && result.privateKey ? result.privateKey
                 : (result && result.type === 'private') ? result : null;

        if (pk) {
            window.__oopz_key_events.push({action: 'generateKey', extractable: pk.extractable});
            if (!window.__oopz_captured_pem && pk.extractable) {
                window.__oopz_captured_pem = await exportAsPem(pk);
            }
        }
        return result;
    };

    crypto.subtle.sign = async function(algorithm, key, data) {
        if (key && key.type === 'private' && !window.__oopz_captured_pem) {
            window.__oopz_key_events.push({action: 'sign', extractable: key.extractable});
            if (key.extractable) {
                window.__oopz_captured_pem = await exportAsPem(key);
            }
        }
        return _sign(algorithm, key, data);
    };
})();
"""

JS_GET_CAPTURED = """
() => ({
    pem: window.__oopz_captured_pem || null,
    events: window.__oopz_key_events || [],
})
"""

JS_CLEAR_INDEXEDDB = """
async () => {
    const deleted = [];
    try {
        try { localStorage.clear(); } catch (e) {}
        try { sessionStorage.clear(); } catch (e) {}
        const dbs = await indexedDB.databases();
        for (const db of dbs) {
            if (!db.name) continue;
            await new Promise((resolve) => {
                const req = indexedDB.deleteDatabase(db.name);
                req.onsuccess = () => resolve();
                req.onerror = () => resolve();
                req.onblocked = () => resolve();
            });
            deleted.push(db.name);
        }
    } catch (e) {}
    return deleted;
}
"""


def _mask(value: Optional[str], keep: int = 4) -> str:
    text = str(value or "")
    if not text:
        return ""
    if len(text) <= keep * 2:
        return text[:keep] + "***"
    return f"{text[:keep]}***{text[-keep:]}"


def _jwt_payload(token: str) -> dict[str, Any]:
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        return json.loads(base64.urlsafe_b64decode(part.encode("utf-8")))
    except Exception:
        return {}


def _jwt_exp_info(token: str) -> dict[str, Any]:
    payload = _jwt_payload(token)
    exp = payload.get("exp")
    if not isinstance(exp, (int, float)):
        return {"exp": None, "expires_at": "", "expires_in_seconds": None, "expired": False}
    now = time.time()
    return {
        "exp": int(exp),
        "expires_at": datetime.fromtimestamp(exp, tz=timezone.utc).isoformat(),
        "expires_in_seconds": max(0, int(exp - now)),
        "expired": exp <= now,
    }


def _safe_response_error(payload: Any) -> str:
    if not isinstance(payload, dict):
        return "登录接口返回异常"
    data = payload.get("data")
    for key in ("message", "msg", "error", "code"):
        value = payload.get(key)
        if value:
            return str(value)
    if isinstance(data, dict):
        for key in ("message", "msg", "error", "code"):
            value = data.get(key)
            if value:
                return str(value)
    return "登录失败，请检查账号密码或风控验证"


def _sanitize_credentials(credentials: dict[str, Any]) -> dict[str, Any]:
    jwt_info = _jwt_exp_info(str(credentials.get("jwt_token") or ""))
    return {
        "person_uid": _mask(credentials.get("person_uid")),
        "device_id": _mask(credentials.get("device_id")),
        "jwt_token": _mask(credentials.get("jwt_token"), keep=10),
        "private_key": bool(credentials.get("private_key_pem")),
        "app_version": credentials.get("app_version") or "",
        **jwt_info,
    }


def _update_from_headers(credentials: dict[str, Any], headers: dict[str, str]) -> None:
    if headers.get("oopz-person") and not credentials.get("person_uid"):
        credentials["person_uid"] = headers["oopz-person"]
    if headers.get("oopz-device-id") and not credentials.get("device_id"):
        credentials["device_id"] = headers["oopz-device-id"]
    if headers.get("oopz-signature") and not credentials.get("jwt_token"):
        credentials["jwt_token"] = headers["oopz-signature"]
    if headers.get("oopz-app-version-number"):
        credentials["app_version"] = headers["oopz-app-version-number"]


def _update_from_login_body(credentials: dict[str, Any], post_data: str | None) -> None:
    if not post_data:
        return
    try:
        body = json.loads(post_data)
    except Exception:
        return
    if isinstance(body, dict) and body.get("deviceId") and not credentials.get("device_id"):
        credentials["device_id"] = body["deviceId"]


def _apply_proxy_to_launch_kwargs(launch_kwargs: dict[str, Any]) -> None:
    try:
        from proxy_utils import get_playwright_proxy
        import config as runtime_config

        proxy = get_playwright_proxy(getattr(runtime_config, "OOPZ_CONFIG", {}).get("proxy"))
        if proxy:
            launch_kwargs["proxy"] = proxy
    except Exception:
        logger.debug("解析 OOPZ 登录浏览器代理失败，使用默认网络设置", exc_info=True)


def _build_launch_kwargs(headless: bool) -> dict[str, Any]:
    browser_env, crash_dir = _prepare_chromium_runtime()
    launch_kwargs: dict[str, Any] = {
        "user_data_dir": BROWSER_DATA_DIR,
        "headless": headless,
        "viewport": {"width": 1280, "height": 900},
        "locale": "zh-CN",
        "args": _chromium_args(crash_dir),
        "env": browser_env,
    }
    chromium_executable_path = _get_chromium_executable_path()
    if chromium_executable_path:
        launch_kwargs["executable_path"] = chromium_executable_path
    _apply_proxy_to_launch_kwargs(launch_kwargs)
    return launch_kwargs


async def _poll_private_key(page, credentials: dict[str, Any], seconds: float) -> None:
    deadline = time.monotonic() + max(0.1, seconds)
    while time.monotonic() < deadline:
        try:
            captured = await page.evaluate(JS_GET_CAPTURED)
            pem = (captured or {}).get("pem")
            if pem:
                credentials["private_key_pem"] = pem
                return
        except Exception:
            pass
        await asyncio.sleep(0.5)


async def _clear_cached_keys_and_retry(page, credentials: dict[str, Any]) -> None:
    try:
        deleted = await page.evaluate(JS_CLEAR_INDEXEDDB)
        logger.info("OOPZ 登录私钥未捕获，已清理 IndexedDB 后重试: %s", deleted)
        await page.reload(wait_until="domcontentloaded")
        await page.wait_for_timeout(5000)
        await _poll_private_key(page, credentials, 8)
    except Exception as exc:
        logger.debug("清理 IndexedDB 重试失败: %s", exc)


async def _open_clean_login_page(context, page) -> None:
    """清理旧网页登录态，确保本次使用表单里输入的账号。"""
    try:
        await context.clear_cookies()
    except Exception:
        logger.debug("清理 OOPZ Cookie 失败", exc_info=True)

    await page.goto(OOPZ_WEB_URL, wait_until="domcontentloaded")
    try:
        await page.evaluate(JS_CLEAR_INDEXEDDB)
    except Exception:
        logger.debug("清理 OOPZ 本地登录状态失败", exc_info=True)
    await page.goto(OOPZ_WEB_URL, wait_until="domcontentloaded")


async def _fill_password_login(page, phone: str, password: str) -> None:
    # OOPZ Web 是 Flutter Canvas，坐标点击比 DOM selector 更稳定。
    await page.mouse.click(880, 610)
    await page.wait_for_timeout(1000)
    await page.mouse.click(760, 354)
    await page.keyboard.press("Control+A")
    await page.keyboard.type(phone, delay=15)
    await page.mouse.click(760, 440)
    await page.keyboard.press("Control+A")
    await page.keyboard.type(password, delay=15)
    await page.mouse.click(882, 532)


async def login_with_password(
    phone: str,
    password: str,
    *,
    timeout: float = 90,
    headless: bool = True,
    save: bool = True,
) -> dict[str, Any]:
    """通过无头 Chromium 登录 OOPZ，并返回已脱敏的凭据摘要。"""
    phone = str(phone or "").strip()
    password = str(password or "")
    if not phone or not password:
        raise OopzPasswordLoginError("账号和密码不能为空")

    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise OopzPasswordLoginError("当前环境缺少 Playwright，请先安装依赖") from exc

    credentials = _new_credentials()
    login_done = asyncio.Event()
    login_error: dict[str, str] = {}

    async def on_response(response) -> None:
        if LOGIN_RESPONSE_PATH not in response.url:
            return
        try:
            payload = await response.json()
        except Exception:
            payload = None
        if not isinstance(payload, dict) or not payload.get("status"):
            login_error["message"] = _safe_response_error(payload)
            login_done.set()
            return
        data = payload.get("data") or {}
        if isinstance(data, dict):
            if data.get("uid"):
                credentials["person_uid"] = data["uid"]
            if data.get("signature"):
                credentials["jwt_token"] = data["signature"]
        login_done.set()

    def on_request(request) -> None:
        try:
            headers = request.headers
            _update_from_headers(credentials, headers)
            if LOGIN_RESPONSE_PATH in request.url:
                _update_from_login_body(credentials, request.post_data)
        except Exception:
            logger.debug("解析 OOPZ 登录请求失败", exc_info=True)

    def on_websocket(ws) -> None:
        def on_frame(payload) -> None:
            try:
                data = json.loads(payload)
                if data.get("event") != WS_EVENT_AUTH:
                    return
                body = json.loads(data.get("body", "{}"))
                if body.get("person") and not credentials.get("person_uid"):
                    credentials["person_uid"] = body["person"]
                if body.get("deviceId") and not credentials.get("device_id"):
                    credentials["device_id"] = body["deviceId"]
                if body.get("signature") and not credentials.get("jwt_token"):
                    credentials["jwt_token"] = body["signature"]
            except Exception:
                pass

        ws.on("framesent", on_frame)

    os.makedirs(BROWSER_DATA_DIR, exist_ok=True)
    async with async_playwright() as p:
        launch_kwargs = _build_launch_kwargs(headless=headless)
        context = await p.chromium.launch_persistent_context(**launch_kwargs)
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            page.set_default_timeout(30000)
            await page.add_init_script(JS_CRYPTO_HOOK)
            page.on("request", on_request)
            page.on("websocket", on_websocket)
            page.on("response", lambda response: asyncio.create_task(on_response(response)))

            await _open_clean_login_page(context, page)
            await page.wait_for_timeout(6500)
            await _fill_password_login(page, phone, password)

            try:
                await asyncio.wait_for(login_done.wait(), timeout=timeout)
            except asyncio.TimeoutError as exc:
                raise OopzPasswordLoginError("等待 OOPZ 登录响应超时") from exc

            if login_error.get("message"):
                raise OopzPasswordLoginError(login_error["message"])

            await _poll_private_key(page, credentials, 10)
            if not credentials.get("private_key_pem"):
                await _clear_cached_keys_and_retry(page, credentials)

            missing = _missing_required_credentials(credentials)
            if missing:
                raise OopzPasswordLoginError("登录成功但未捕获完整凭据: " + ", ".join(missing))
        finally:
            await context.close()

    saved: list[str] = []
    if save:
        saved = save_credentials(credentials)

    return {
        "ok": True,
        "saved": saved,
        "credentials": _sanitize_credentials(credentials),
        "raw": credentials,
        "restart_required": True,
    }


def _read_config_template() -> str:
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return f.read()
    if os.path.exists(CONFIG_EXAMPLE_PATH):
        with open(CONFIG_EXAMPLE_PATH, "r", encoding="utf-8") as f:
            return f.read()
    raise OopzPasswordLoginError("config.py 不存在，且未找到 config.example.py")


def _replace_config_value(content: str, key: str, value: Any) -> tuple[str, bool]:
    if value is None or str(value) == "":
        return content, False
    pattern = re.compile(rf'("{re.escape(key)}"\s*:\s*)"[^"]*"')
    replacement_value = json.dumps(str(value), ensure_ascii=False)
    content, count = pattern.subn(lambda m: f"{m.group(1)}{replacement_value}", content, count=1)
    return content, count > 0


def _private_key_module_content(pem: str) -> str:
    pem = pem.strip().replace("\r\n", "\n")
    return (
        '"""RSA 私钥（由后台 OOPZ 登录自动生成）"""\n'
        "\n"
        "from cryptography.hazmat.primitives import serialization\n"
        "from cryptography.hazmat.backends import default_backend\n"
        "\n"
        f'PRIVATE_KEY_PEM = b"""{pem}"""\n'
        "\n"
        "\n"
        "def get_private_key():\n"
        '    """加载并返回 RSA 私钥对象。"""\n'
        "    return serialization.load_pem_private_key(\n"
        "        PRIVATE_KEY_PEM,\n"
        "        password=None,\n"
        "        backend=default_backend(),\n"
        "    )\n"
    )


def _save_config(credentials: dict[str, Any]) -> str:
    content = _read_config_template()
    replaced_any = False
    for key in OOPZ_CONFIG_CREDENTIAL_FIELDS:
        content, replaced = _replace_config_value(content, key, credentials.get(key))
        replaced_any = replaced_any or replaced
    if not replaced_any:
        raise OopzPasswordLoginError("未能在 config.py 中定位 OOPZ_CONFIG 凭据字段")
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        f.write(content)
    return "config.py"


def _save_private_key(pem: str) -> str:
    with open(PRIVATE_KEY_PATH, "w", encoding="utf-8") as f:
        f.write(_private_key_module_content(pem))
    return "private_key.py"


def save_credentials(credentials: dict[str, Any]) -> list[str]:
    """写入 config.py 与 private_key.py，不额外生成明文凭据备份。"""
    pem = str(credentials.get("private_key_pem") or "").strip()
    if not pem:
        raise OopzPasswordLoginError("缺少 RSA 私钥，无法写入 private_key.py")

    saved = [_save_config(credentials), _save_private_key(pem)]
    _apply_config_to_runtime(credentials)
    return saved


def _apply_config_to_runtime(credentials: dict[str, Any]) -> None:
    updates = {key: credentials.get(key) for key in OOPZ_CONFIG_CREDENTIAL_FIELDS if credentials.get(key)}
    for module_name in ("config", "web_player_config"):
        try:
            module = __import__(module_name)
            target = getattr(module, "OOPZ_CONFIG", None)
            if isinstance(target, dict):
                target.update(updates)
        except Exception:
            logger.debug("同步 %s.OOPZ_CONFIG 失败", module_name, exc_info=True)


def load_private_key_from_pem(pem: str):
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives import serialization

    return serialization.load_pem_private_key(
        pem.encode("utf-8"),
        password=None,
        backend=default_backend(),
    )
