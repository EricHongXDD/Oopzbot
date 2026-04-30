"""Web 播放器管理后台路由 — 所有 /admin 和 /admin/api 端点。"""

from __future__ import annotations

import asyncio
import base64
import copy
import io
import json
import os
import secrets
import string
import sys
import time
from collections import deque
from http.cookies import SimpleCookie
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

try:
    import requests
    RequestsException = requests.RequestException
except Exception:
    requests = None
    RequestsException = RuntimeError

try:
    import qrcode
except Exception:
    qrcode = None

from database import DB_PATH, MessageStatsDB, ReminderDB, ScheduledMessageDB, SongCache, Statistics, db_connection
from logger_config import get_logger
from name_resolver import get_resolver
from queue_manager import get_redis_client, _area_key, KEY_QUEUE, KEY_CURRENT, KEY_PLAY_STATE
from scheduler_templates import get_scheduled_template, list_scheduled_templates
from web_link_token import clear_token, ensure_token, get_active_area, get_token, set_token

import web_player_config as cfg
from app.services.interaction.setup_diagnostics import SetupDiagnostics

logger = get_logger("WebPlayerAdmin")

admin_router = APIRouter()
_oopz_login_lock = asyncio.Lock()
_OOPZ_RUNTIME_FIELDS = ("app_version", "device_id", "person_uid", "jwt_token")


# ---------------------------------------------------------------------------
# 内部辅助
# ---------------------------------------------------------------------------

def _get_redis():
    """延迟导入，避免循环引用。"""
    from web_player import get_redis
    return get_redis()


def _get_sender():
    from web_player import get_sender
    return get_sender()


def _get_oopz_client():
    from web_player import get_oopz_client
    return get_oopz_client()


def _oopz_runtime_updates(credentials: dict[str, Any]) -> dict[str, Any]:
    """提取可直接同步到 OOPZ_CONFIG 的字段。"""
    return {key: credentials.get(key) for key in _OOPZ_RUNTIME_FIELDS if credentials.get(key)}


def _apply_oopz_config_updates(credentials: dict[str, Any]) -> bool:
    updates = _oopz_runtime_updates(credentials)
    if not updates:
        return False
    try:
        import config as runtime_config
        runtime_config.OOPZ_CONFIG.update(updates)
        cfg.OOPZ_CONFIG.update(updates)
        return True
    except Exception:
        logger.debug("同步 OOPZ_CONFIG 到运行时失败", exc_info=True)
        return False


def _refresh_oopz_sender_private_key(pem: str) -> bool:
    if not pem:
        return False
    try:
        from oopz_password_login import load_private_key_from_pem

        sender = _get_sender()
        if not sender or not getattr(sender, "signer", None):
            return False
        sender.signer.private_key = load_private_key_from_pem(pem)
        cache = getattr(sender, "_area_members_cache", None)
        if isinstance(cache, dict):
            cache.clear()
        return True
    except Exception:
        logger.debug("刷新 OOPZ 发送端签名器失败", exc_info=True)
        return False


def _refresh_oopz_name_resolver(pem: str) -> bool:
    if not pem:
        return False
    try:
        from oopz_password_login import load_private_key_from_pem
        import name_resolver

        resolver = getattr(name_resolver, "_resolver", None)
        if resolver is None:
            return False
        resolver._private_key = load_private_key_from_pem(pem)
        resolver._config = cfg.OOPZ_CONFIG
        resolver._api_ready = True
        return True
    except Exception:
        logger.debug("刷新名称解析器 OOPZ 凭据失败", exc_info=True)
        return False


def _reload_private_key_module() -> None:
    try:
        import importlib
        import private_key
        importlib.reload(private_key)
    except Exception:
        logger.debug("重新加载 private_key.py 失败，继续使用内存中的新私钥", exc_info=True)


def _refresh_oopz_websocket(credentials: dict[str, Any]) -> bool:
    if not all(credentials.get(k) for k in ("person_uid", "device_id", "jwt_token")):
        return False
    client = _get_oopz_client()
    if not client:
        return False
    try:
        client.update_credentials(
            credentials["person_uid"],
            credentials["device_id"],
            credentials["jwt_token"],
            reconnect=True,
        )
        return True
    except Exception:
        logger.debug("刷新 OOPZ WebSocket 凭据失败", exc_info=True)
        return False


def _refresh_oopz_runtime(credentials: dict[str, Any]) -> dict[str, bool]:
    """将新 OOPZ 凭据同步到已创建的发送端和 WebSocket 客户端。"""
    pem = str(credentials.get("private_key_pem") or "").strip()
    if pem:
        _reload_private_key_module()

    refreshed = {
        "config": _apply_oopz_config_updates(credentials),
        "sender_signer": _refresh_oopz_sender_private_key(pem),
        "websocket_client": _refresh_oopz_websocket(credentials),
        "name_resolver": _refresh_oopz_name_resolver(pem),
    }
    return refreshed


_resolved_area_cache: dict = {"value": "", "ts": 0.0}


def _resolve_area() -> str:
    """获取当前域 ID,优先使用配置,否则从已加入的域列表取第一个(缓存 5 分钟)。"""
    area = (cfg.OOPZ_CONFIG.get("default_area") or "").strip()
    if area:
        return area
    now = time.time()
    if _resolved_area_cache["value"] and now - _resolved_area_cache["ts"] < 300:
        return _resolved_area_cache["value"]
    sender = _get_sender()
    if not sender:
        return ""
    try:
        areas = sender.get_joined_areas(quiet=True)
        if areas:
            resolved = (areas[0].get("id") or "").strip()
            if resolved:
                _resolved_area_cache.update(value=resolved, ts=now)
                return resolved
    except Exception:
        logger.debug("自动解析默认域失败", exc_info=True)
    return ""


def _music_area_context(redis_client=None) -> dict:
    """返回后台音乐当前使用的域上下文。"""
    r = redis_client or _get_redis()
    default_area = (cfg.OOPZ_CONFIG.get("default_area") or "").strip()
    active_area = ""
    try:
        active_area = (get_active_area(redis_client=r) or "").strip()
    except Exception:
        logger.debug("读取活跃域失败，继续尝试默认域/自动探测", exc_info=True)

    area = ""
    source = "none"
    if active_area:
        area = active_area
        source = "active"
    elif default_area:
        area = default_area
        source = "default"
    else:
        area = _resolve_area()
        if area:
            source = "auto"

    source_text = {
        "active": "活跃域",
        "default": "默认域",
        "auto": "自动探测",
        "none": "未解析",
    }.get(source, "未解析")

    return {
        "area": area,
        "source": source,
        "source_text": source_text,
        "default_area": default_area,
        "active_area": active_area,
    }


def _get_music_area(redis_client=None) -> str:
    """获取音乐相关接口应使用的域。"""
    return _music_area_context(redis_client).get("area", "")


_members_resp_cache: dict = {"data": None, "ts": 0.0, "key": ""}
_MEMBERS_RESP_TTL = 10.0  # 管理后台成员列表响应缓存 10 秒


def _invalidate_members_cache() -> None:
    """管理操作后清除成员列表缓存,让下次请求拿到最新数据。"""
    _members_resp_cache.update(data=None, ts=0.0, key="")
    sender = _get_sender()
    if sender:
        store = getattr(sender, "_area_members_cache", None)
        if isinstance(store, dict):
            store.clear()


def _get_netease():
    from web_player import get_netease
    return get_netease()


def _get_started_at() -> float:
    from web_player import started_at
    return started_at


def _admin_enabled() -> bool:
    from web_player import _admin_enabled as web_admin_enabled
    return web_admin_enabled()


def _get_liked_ids_cache() -> list:
    from web_player import liked_ids_cache
    return liked_ids_cache


def _get_plugin_runtime():
    from web_player import get_plugin_runtime
    return get_plugin_runtime()


def _set_liked_ids_cache(value: list) -> None:
    import web_player
    web_player.liked_ids_cache = value


def _normalize_netease_base_url(raw: object = "") -> str:
    """校验并规范化后台传入的网易云 API 地址。"""
    value = str(raw or cfg.NETEASE_CLOUD.get("base_url") or "").strip().rstrip("/")
    if not value:
        raise ValueError("网易云 API 地址为空")
    if len(value) > 300:
        raise ValueError("网易云 API 地址过长")
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("网易云 API 地址必须是 http/https URL")
    return value


def _netease_timestamp_params(extra: dict | None = None) -> dict:
    """附加时间戳参数，避免网易云登录接口被缓存。"""
    stamp = str(int(time.time() * 1000))
    params = {
        "timestamp": stamp,
        "timerstamp": stamp,
    }
    if extra:
        params.update(extra)
    return params


def _netease_api_get(
    base_url: str,
    path: str,
    params: dict | None = None,
    headers: dict | None = None,
) -> tuple[dict, requests.Response]:
    """请求本地/远端 NeteaseCloudMusicApi 并返回 JSON。"""
    if requests is None:
        raise RuntimeError("缺少 requests 依赖，请先安装 requirements.txt")
    request_headers = {"Cache-Control": "no-cache"}
    if headers:
        request_headers.update(headers)
    response = requests.get(
        f"{base_url}{path}",
        params=params or {},
        headers=request_headers,
        timeout=10,
    )
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise ValueError("网易云 API 返回格式异常")
    return data, response


def _netease_api_post(
    base_url: str,
    path: str,
    data: dict | None = None,
    headers: dict | None = None,
) -> tuple[dict, requests.Response]:
    """POST 请求网易云 API；用于避免把长 Cookie 暴露在查询串里。"""
    if requests is None:
        raise RuntimeError("缺少 requests 依赖，请先安装 requirements.txt")
    request_headers = {"Cache-Control": "no-cache"}
    if headers:
        request_headers.update(headers)
    response = requests.post(
        f"{base_url}{path}",
        data=data or {},
        headers=request_headers,
        timeout=10,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("网易云 API 返回格式异常")
    return payload, response


def _netease_response_data(payload: dict) -> dict:
    """兼容不同网易云 API 分支的 data 包装结构。"""
    data = payload.get("data")
    return data if isinstance(data, dict) else {}


def _netease_qr_code(payload: dict) -> int:
    """从扫码检查响应中提取状态码。"""
    status_codes = {800, 801, 802, 803}
    values = (_netease_response_data(payload).get("code"), payload.get("code"))
    parsed_values = []
    for value in values:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            continue
        if parsed in status_codes:
            return parsed
        parsed_values.append(parsed)
    if parsed_values:
        return parsed_values[0]
    return 0


def _cookie_from_response(payload: dict, response: requests.Response) -> str:
    """优先取接口 JSON 中的 Cookie，回退到响应 Set-Cookie。"""
    nested = _netease_response_data(payload)
    cookie = str(payload.get("cookie") or nested.get("cookie") or "").strip()
    if cookie:
        logger.debug("网易云扫码登录 Cookie 提取成功: source=json %s", _cookie_debug_summary(cookie))
        return cookie

    jar = getattr(response, "cookies", None)
    if jar:
        try:
            pairs = [f"{item.name}={item.value}" for item in jar]
            cookie = "; ".join(pair for pair in pairs if pair)
            if cookie:
                logger.debug("网易云扫码登录 Cookie 提取成功: source=response %s", _cookie_debug_summary(cookie))
                return cookie
        except Exception:
            logger.debug("解析网易云登录响应 CookieJar 失败", exc_info=True)

    header = response.headers.get("set-cookie", "") if hasattr(response, "headers") else ""
    cookie = header.strip()
    if cookie:
        logger.debug("网易云扫码登录 Cookie 提取成功: source=set-cookie %s", _cookie_debug_summary(cookie))
    else:
        logger.debug("网易云扫码登录响应中未找到可用 Cookie")
    return cookie


def _netease_login_message(payload: dict, default: str = "") -> str:
    """兼容 message/msg/nested message 三种提示字段。"""
    nested = _netease_response_data(payload)
    return str(
        payload.get("message")
        or payload.get("msg")
        or nested.get("message")
        or nested.get("msg")
        or default
    )


def _extract_netease_profile(payload: dict) -> Optional[dict]:
    """从账号接口返回中提取昵称和用户 ID。"""
    nested = _netease_response_data(payload)
    profile = payload.get("profile") or nested.get("profile") or {}
    account = payload.get("account") or nested.get("account") or {}
    if not isinstance(profile, dict):
        profile = {}
    if not isinstance(account, dict):
        account = {}

    user_id = (
        profile.get("userId")
        or profile.get("userid")
        or profile.get("id")
        or account.get("id")
        or account.get("userId")
    )
    if not user_id:
        return None

    nickname = (
        profile.get("nickname")
        or profile.get("name")
        or account.get("userName")
        or account.get("nickname")
        or ""
    )
    return {
        "user_id": str(user_id),
        "nickname": str(nickname or ""),
        "avatar_url": str(profile.get("avatarUrl") or profile.get("avatarUrlHttps") or ""),
    }


def _netease_account_status(base_url: str, cookie: str) -> dict:
    """使用 Cookie 查询当前网易云登录账号。"""
    cookie = (cookie or "").strip()
    if not cookie:
        logger.debug("网易云账号状态查询跳过: 未配置 Cookie")
        return {"ok": True, "logged_in": False, "message": "未配置网易云 Cookie"}

    logger.debug("网易云账号状态查询开始: base_url=%s %s", base_url, _cookie_debug_summary(cookie))
    requests_to_try = (
        (
            "POST",
            "/login/status",
            _netease_timestamp_params({"cookie": cookie}),
        ),
        (
            "GET",
            "/user/account",
            _netease_timestamp_params(),
        ),
    )
    last_message = ""
    for method, path, params in requests_to_try:
        try:
            logger.debug("网易云账号状态请求: method=%s path=%s", method, path)
            if method == "POST":
                payload, _ = _netease_api_post(base_url, path, data=params, headers={"Cookie": cookie})
            else:
                payload, _ = _netease_api_get(base_url, path, params=params, headers={"Cookie": cookie})
        except Exception as exc:
            last_message = str(exc)
            logger.debug("网易云账号状态请求失败 (%s %s): %s", method, path, exc)
            continue

        nested = _netease_response_data(payload)
        logger.debug(
            "网易云账号状态接口返回: path=%s code=%s data_code=%s message=%s",
            path,
            payload.get("code"),
            nested.get("code") if isinstance(nested, dict) else None,
            _netease_login_message(payload, ""),
        )
        profile = _extract_netease_profile(payload)
        if profile:
            logger.debug("网易云账号状态查询成功: path=%s %s", path, _debug_profile_text(profile))
            return {
                "ok": True,
                "logged_in": True,
                "profile": profile,
                "message": "网易云账号已登录",
            }
        last_message = _netease_login_message(payload, last_message)
        logger.debug("网易云账号状态未解析到 profile: path=%s message=%s", path, last_message)

    logger.debug("网易云账号状态查询未登录: message=%s", last_message)
    return {
        "ok": True,
        "logged_in": False,
        "message": last_message or "Cookie 未登录或已过期",
    }


_BILIBILI_LOGIN_BASE = "https://passport.bilibili.com"
_BILIBILI_API_BASE = "https://api.bilibili.com"
_BILIBILI_QR_GENERATE_PATH = "/x/passport-login/web/qrcode/generate"
_BILIBILI_QR_POLL_PATH = "/x/passport-login/web/qrcode/poll"
_BILIBILI_NAV_PATH = "/x/web-interface/nav"
_BILIBILI_COOKIE_NAMES = (
    "SESSDATA",
    "bili_jct",
    "DedeUserID",
    "DedeUserID__ckMd5",
    "sid",
)


def _make_qr_data_uri(content: str) -> str:
    """把登录 URL 渲染为前端可直接展示的二维码 PNG。"""
    if qrcode is None:
        raise RuntimeError("缺少 qrcode 依赖，请先安装 requirements.txt")
    image = qrcode.make(content)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def _bilibili_api_get(path: str, params: dict | None = None) -> tuple[dict, requests.Response]:
    """请求 B 站 Web 扫码登录接口并返回 JSON。"""
    if requests is None:
        raise RuntimeError("缺少 requests 依赖，请先安装 requirements.txt")
    response = requests.get(
        f"{_BILIBILI_LOGIN_BASE}{path}",
        params=params or {},
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
            ),
            "Referer": "https://www.bilibili.com/",
            "Cache-Control": "no-cache",
        },
        timeout=10,
    )
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise ValueError("B 站 API 返回格式异常")
    return data, response


def _bilibili_account_api_get(path: str, headers: dict | None = None) -> tuple[dict, requests.Response]:
    """请求 B 站 Web API，用于读取已登录账号信息。"""
    if requests is None:
        raise RuntimeError("缺少 requests 依赖，请先安装 requirements.txt")
    request_headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
        ),
        "Referer": "https://www.bilibili.com/",
        "Cache-Control": "no-cache",
    }
    if headers:
        request_headers.update(headers)
    response = requests.get(
        f"{_BILIBILI_API_BASE}{path}",
        headers=request_headers,
        timeout=10,
    )
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise ValueError("B 站 API 返回格式异常")
    return data, response


def _bilibili_response_data(payload: dict) -> dict:
    """兼容 B 站响应中的 data 包装结构。"""
    data = payload.get("data")
    return data if isinstance(data, dict) else {}


def _bilibili_login_message(payload: dict, default: str = "") -> str:
    """提取 B 站扫码接口返回的提示。"""
    nested = _bilibili_response_data(payload)
    return str(
        nested.get("message")
        or payload.get("message")
        or payload.get("msg")
        or default
    )


def _bilibili_qr_code(payload: dict) -> int:
    """从 B 站扫码轮询响应中提取状态码。"""
    nested = _bilibili_response_data(payload)
    for value in (nested.get("code"), payload.get("code")):
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return -1


def _cookie_pairs_from_response(response: requests.Response, allowed_names: tuple[str, ...] = ()) -> str:
    """从 CookieJar 或 Set-Cookie 头中整理出可配置的 Cookie 字符串。"""
    allowed = set(allowed_names)
    pairs: list[str] = []
    jar = getattr(response, "cookies", None)
    if jar:
        try:
            for item in jar:
                name = getattr(item, "name", "")
                value = getattr(item, "value", "")
                if name and value and (not allowed or name in allowed):
                    pairs.append(f"{name}={value}")
        except Exception:
            logger.debug("解析登录响应 CookieJar 失败", exc_info=True)
    if pairs:
        return "; ".join(pairs)

    header = response.headers.get("set-cookie", "") if hasattr(response, "headers") else ""
    if header:
        try:
            cookie = SimpleCookie()
            cookie.load(header)
            for name, morsel in cookie.items():
                if morsel.value and (not allowed or name in allowed):
                    pairs.append(f"{name}={morsel.value}")
        except Exception:
            logger.debug("解析 Set-Cookie 头失败", exc_info=True)
    return "; ".join(pairs)


def _mask_debug_token(value: str, prefix: int = 6, suffix: int = 4) -> str:
    """日志中遮罩 token/key，避免泄露可复用凭据。"""
    text = str(value or "")
    if not text:
        return "-"
    if len(text) <= prefix + suffix:
        return "*" * len(text)
    return f"{text[:prefix]}...{text[-suffix:]}"


def _cookie_debug_summary(cookie: str) -> str:
    """生成不含 Cookie 值的调试摘要。"""
    text = (cookie or "").strip()
    names: list[str] = []
    if text:
        try:
            parsed = SimpleCookie()
            parsed.load(text)
            names = [name for name, morsel in parsed.items() if morsel.value]
        except Exception:
            names = []
        if not names:
            for item in text.split(";"):
                name = item.split("=", 1)[0].strip()
                if name:
                    names.append(name)
    names_text = ",".join(names) if names else "-"
    return f"len={len(text)} names={names_text}"


def _debug_profile_text(profile: Optional[dict]) -> str:
    """生成账号资料调试文本。"""
    if not profile:
        return "profile=-"
    return "nickname=%s uid=%s" % (
        profile.get("nickname") or "",
        profile.get("user_id") or "",
    )


def _bilibili_cookie_from_poll(payload: dict, response: requests.Response) -> str:
    """优先取 Set-Cookie，回退到跨域登录 URL 中的 Cookie 参数。"""
    cookie = _cookie_pairs_from_response(response, _BILIBILI_COOKIE_NAMES)
    if cookie:
        logger.debug("B 站扫码登录 Cookie 提取成功: source=response %s", _cookie_debug_summary(cookie))
        return cookie

    login_url = str(_bilibili_response_data(payload).get("url") or "").strip()
    if not login_url:
        logger.debug("B 站扫码登录未拿到跨域登录 URL，无法回退提取 Cookie")
        return ""
    try:
        query = parse_qs(urlparse(login_url).query)
    except Exception:
        logger.debug("B 站扫码登录 URL 解析失败，无法回退提取 Cookie", exc_info=True)
        return ""
    pairs = []
    for name in _BILIBILI_COOKIE_NAMES:
        values = query.get(name)
        if values and values[0]:
            pairs.append(f"{name}={values[0]}")
    cookie = "; ".join(pairs)
    if cookie:
        logger.debug("B 站扫码登录 Cookie 提取成功: source=url %s", _cookie_debug_summary(cookie))
    else:
        logger.debug("B 站扫码登录 URL 中未找到可用 Cookie 字段")
    return cookie


def _extract_bilibili_profile(payload: dict) -> Optional[dict]:
    """从 B 站导航栏接口返回中提取昵称和 UID。"""
    data = _bilibili_response_data(payload)
    if not isinstance(data, dict) or not data.get("isLogin"):
        return None
    user_id = data.get("mid") or data.get("uid")
    if not user_id:
        return None
    return {
        "user_id": str(user_id),
        "nickname": str(data.get("uname") or data.get("name") or ""),
        "avatar_url": str(data.get("face") or ""),
    }


def _bilibili_account_status(cookie: str) -> dict:
    """使用 Cookie 查询当前 B 站登录账号。"""
    cookie = (cookie or "").strip()
    if not cookie:
        logger.debug("B 站账号状态查询跳过: 未配置 Cookie")
        return {"ok": True, "logged_in": False, "message": "未配置 B 站 Cookie"}

    logger.debug("B 站账号状态查询开始: %s", _cookie_debug_summary(cookie))
    payload, _ = _bilibili_account_api_get(
        _BILIBILI_NAV_PATH,
        headers={"Cookie": cookie},
    )
    data = _bilibili_response_data(payload)
    logger.debug(
        "B 站账号状态接口返回: code=%s isLogin=%s message=%s",
        payload.get("code"),
        data.get("isLogin") if isinstance(data, dict) else None,
        _bilibili_login_message(payload, ""),
    )
    profile = _extract_bilibili_profile(payload)
    if profile:
        logger.debug("B 站账号状态查询成功: %s", _debug_profile_text(profile))
        return {
            "ok": True,
            "logged_in": True,
            "profile": profile,
            "message": "B 站账号已登录",
        }

    message = _bilibili_login_message(payload, "Cookie 未登录或已过期")
    logger.debug("B 站账号状态查询未登录: message=%s", message)
    return {"ok": True, "logged_in": False, "message": message}


_ADMIN_SHELL_TEMPLATE: string.Template | None = None


def _load_admin_template() -> string.Template:
    global _ADMIN_SHELL_TEMPLATE
    if _ADMIN_SHELL_TEMPLATE is None:
        tpl_path = os.path.join(os.path.dirname(__file__), "admin_assets", "admin-shell-template.html")
        with open(tpl_path, "r", encoding="utf-8") as f:
            _ADMIN_SHELL_TEMPLATE = string.Template(f.read())
    return _ADMIN_SHELL_TEMPLATE


_ADMIN_PAGES: dict[str, dict[str, str]] = {
    "dashboard": {
        "page_title": "后台总览",
        "page_id": "dashboard",
        "brand_title": "后台管理",
        "brand_copy": "顶部主导航、数据优先、专业 SaaS 工作台。",
        "topbar_actions": '<button class="btn btn-ghost" type="button" onclick="loadOverview().catch(() => {})">刷新概览</button>',
        "login_title": "登录后台总览",
        "login_copy": "登录后查看实时状态与关键指标。",
        "login_button": "进入总览",
    },
    "music": {
        "page_title": "音乐管理",
        "page_id": "music",
        "brand_title": "音乐管理",
        "brand_copy": "把播放控制、搜索加歌和队列调度整理成标准运营面板。",
        "topbar_actions": '<button class="btn btn-ghost" type="button" onclick="loadQueue().catch(() => {})">刷新队列</button>',
        "login_title": "登录音乐后台",
        "login_copy": "登录后控制播放、搜索歌曲和调整队列。",
        "login_button": "进入音乐控制台",
    },
    "config": {
        "page_title": "配置中心",
        "page_id": "config",
        "brand_title": "配置中心",
        "brand_copy": "把长表单整理成章节化配置工作台，保留原字段和保存接口。",
        "topbar_actions": (
            '<button class="btn btn-ghost" type="button" onclick="loadConfig().catch(() => {})">刷新配置</button>\n'
            '          <button class="btn btn-primary" type="button" onclick="saveConfig(true)">保存并持久化</button>'
        ),
        "login_title": "登录配置中心",
        "login_copy": "登录后调整后台配置。",
        "login_button": "进入配置中心",
    },
    "stats": {
        "page_title": "统计页",
        "page_id": "stats",
        "brand_title": "统计页",
        "brand_copy": "让摘要、榜单和危险操作形成稳定阅读顺序，而不是只摆一张表。",
        "topbar_actions": (
            '<button class="btn btn-ghost" type="button" onclick="loadTop().catch(() => {})">刷新统计</button>\n'
            '          <button class="btn btn-danger" type="button" onclick="clearHistory()">清空历史</button>'
        ),
        "login_title": "登录统计页",
        "login_copy": "登录后查看最近 7 天的播放排行。",
        "login_button": "进入统计页",
    },
    "system": {
        "page_title": "系统页",
        "page_id": "system",
        "brand_title": "系统页",
        "brand_copy": "把播放器入口、系统快照和实时日志拆成明确的运维层级。",
        "topbar_actions": (
            '<button class="btn btn-ghost" type="button" onclick="loadSys().catch(() => {})">刷新系统信息</button>\n'
            '          <button class="btn btn-ghost" type="button" onclick="loadLogs(logTailSize).catch(() => {})">刷新日志</button>'
        ),
        "login_title": "登录系统页",
        "login_copy": "登录后查看链接、系统信息和日志。",
        "login_button": "进入系统页",
    },
    "activity": {
        "page_title": "活跃统计",
        "page_id": "activity",
        "brand_title": "活跃统计",
        "brand_copy": "频道消息趋势与用户活跃排行一览。",
        "topbar_actions": '<button class="btn btn-ghost" type="button" onclick="loadActivity().catch(() => {})">刷新统计</button>',
        "login_title": "登录活跃统计",
        "login_copy": "登录后查看消息趋势与活跃排行。",
        "login_button": "进入活跃统计",
    },
    "scheduler": {
        "page_title": "定时任务",
        "page_id": "scheduler",
        "brand_title": "定时任务",
        "brand_copy": "管理定时消息与用户提醒。",
        "topbar_actions": '<button class="btn btn-ghost" type="button" onclick="loadScheduler().catch(() => {})">刷新列表</button>',
        "login_title": "登录定时任务",
        "login_copy": "登录后管理定时消息与查看提醒。",
        "login_button": "进入定时任务",
    },
    "members": {
        "page_title": "成员管理",
        "page_id": "members",
        "brand_title": "成员管理",
        "brand_copy": "域成员浏览、管理操作与封禁列表。",
        "topbar_actions": '<button class="btn btn-ghost" type="button" onclick="loadMembers().catch(() => {})">刷新成员</button>',
        "login_title": "登录成员管理",
        "login_copy": "登录后管理域成员。",
        "login_button": "进入成员管理",
    },
    "areas": {
        "page_title": "域管理",
        "page_id": "areas",
        "brand_title": "域管理",
        "brand_copy": "域配置、频道管理与语音频道监控。",
        "topbar_actions": '<button class="btn btn-ghost" type="button" onclick="loadAreaManager().catch(() => {})">刷新</button>',
        "login_title": "登录域管理",
        "login_copy": "登录后管理域配置与频道。",
        "login_button": "进入域管理",
    },
    "plugins": {
        "page_title": "插件管理",
        "page_id": "plugins",
        "brand_title": "插件管理",
        "brand_copy": "查看、加载、卸载插件，在线编辑插件配置。",
        "topbar_actions": '<button class="btn btn-ghost" type="button" onclick="loadPlugins().catch(() => {})">刷新列表</button>',
        "login_title": "登录插件管理",
        "login_copy": "登录后管理插件与配置。",
        "login_button": "进入插件管理",
    },
    "setup": {
        "page_title": "系统体检",
        "page_id": "setup",
        "brand_title": "系统体检",
        "brand_copy": "把首启检查、运行时诊断和下一步配置建议放在一个页面里。",
        "topbar_actions": '<button class="btn btn-ghost" type="button" onclick="loadDiagnostics().catch(() => {})">重新体检</button>',
        "login_title": "登录系统体检",
        "login_copy": "登录后查看系统体检与首启向导。",
        "login_button": "进入体检页",
    },
}


def _render_admin_page(page_key: str) -> HTMLResponse:
    if not _admin_enabled():
        return HTMLResponse("管理后台未启用，请在 WEB_PLAYER_CONFIG 中开启。", status_code=404)
    pages_dir = os.path.join(os.path.dirname(__file__), "admin_assets", "pages")
    content_path = os.path.join(pages_dir, f"{page_key}_content.html")
    script_path = os.path.join(pages_dir, f"{page_key}_script.js")
    with open(content_path, "r", encoding="utf-8") as f:
        page_content = f.read()
    with open(script_path, "r", encoding="utf-8") as f:
        page_script = f.read()
    meta = _ADMIN_PAGES[page_key]
    tpl = _load_admin_template()
    html = tpl.safe_substitute(
        page_title=meta["page_title"],
        page_id=meta["page_id"],
        brand_title=meta["brand_title"],
        brand_copy=meta["brand_copy"],
        topbar_actions=meta["topbar_actions"],
        login_title=meta["login_title"],
        login_copy=meta["login_copy"],
        login_button=meta["login_button"],
        page_content=page_content,
        page_script=page_script,
    )
    return HTMLResponse(html)


def _set_admin_session_token(token: str) -> None:
    ttl = cfg.admin_session_ttl_seconds()
    r = _get_redis()
    if ttl > 0:
        r.set(cfg.admin_session_key(token), "1", ex=ttl)
    else:
        r.set(cfg.admin_session_key(token), "1")


def _clear_admin_session_token(token: str) -> None:
    if not token:
        return
    try:
        _get_redis().delete(cfg.admin_session_key(token))
    except Exception:
        logger.debug("清除管理后台会话令牌失败", exc_info=True)


def _overview_payload() -> dict:
    redis_status = "connected"
    queue_len = 0
    playing: dict = {}
    area_context = _music_area_context()
    try:
        r = _get_redis()
        r.ping()
        area_context = _music_area_context(r)
        area = area_context.get("area", "")
        queue_len = int(r.llen(_area_key(KEY_QUEUE, area)) or 0)
        current_raw = r.get(_area_key(KEY_CURRENT, area))
        play_state_raw = r.get(_area_key(KEY_PLAY_STATE, area))
        playing = {
            "current": json.loads(current_raw) if current_raw else None,
            "play_state": json.loads(play_state_raw) if play_state_raw else None,
            "area": area,
        }
    except Exception as e:
        redis_status = f"error: {e}"

    today = Statistics.get_today() or {}
    summary = Statistics.get_summary()
    return {
        "ok": True,
        "uptime_seconds": int(time.time() - _get_started_at()),
        "redis": redis_status,
        "queue_length": queue_len,
        "playing": playing,
        "music_area": area_context,
        "statistics_today": today,
        "statistics_summary": summary,
        "today_messages": MessageStatsDB.get_today_total(),
        "active_users_today": MessageStatsDB.get_active_users_today(),
    }


def _tail_file(path: str, lines: int = 200) -> list[str]:
    if not os.path.exists(path):
        return []
    max_lines = max(1, min(int(lines), 2000))
    dq: deque[str] = deque(maxlen=max_lines)
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            dq.append(line.rstrip("\n"))
    return list(dq)


def _top_songs_from_play_history(page: int = 1, page_size: int = 10) -> tuple[list[dict], int]:
    page = max(1, int(page or 1))
    page_size = max(1, min(int(page_size or 10), 100))
    offset = (page - 1) * page_size
    with db_connection() as conn:
        total_row = conn.execute(
            """
            SELECT COUNT(1) AS c
            FROM (
                SELECT sc.song_id
                FROM play_history ph
                LEFT JOIN song_cache sc ON sc.id = ph.song_cache_id
                GROUP BY sc.song_id, sc.song_name, sc.artist, sc.album
            ) t
            """
        ).fetchone()
        total = int(total_row["c"] if total_row else 0)
        rows = conn.execute(
            """
            SELECT
                sc.song_id AS song_id,
                COALESCE(sc.song_name, '') AS song_name,
                COALESCE(sc.artist, '') AS artist,
                COALESCE(sc.album, '') AS album,
                COUNT(ph.id) AS play_count,
                MAX(ph.played_at) AS last_played_at
            FROM play_history ph
            LEFT JOIN song_cache sc ON sc.id = ph.song_cache_id
            GROUP BY sc.song_id, sc.song_name, sc.artist, sc.album
            ORDER BY play_count DESC, last_played_at DESC
            LIMIT ? OFFSET ?
            """,
            (page_size, offset),
        ).fetchall()
    return [dict(r) for r in rows], total


def _queue_snapshot(redis_client, area: str = "") -> list[dict]:
    items = redis_client.lrange(_area_key(KEY_QUEUE, area), 0, -1)
    queue: list[dict] = []
    for i, item in enumerate(items):
        try:
            song = json.loads(item)
        except Exception as e:
            logger.debug("解析队列项 %d 失败: %s", i, e)
            song = {}
        queue.append({
            "index": i,
            "id": song.get("song_id") or song.get("id"),
            "name": song.get("name", ""),
            "artists": song.get("artists", ""),
            "album": song.get("album", ""),
            "durationText": song.get("durationText") or song.get("duration", ""),
        })
    return queue


def _current_song_snapshot(redis_client, area: str = "") -> Optional[dict]:
    try:
        raw = redis_client.get(_area_key(KEY_CURRENT, area))
        if not raw:
            return None
        song = json.loads(raw)
        return {
            "id": song.get("song_id") or song.get("id"),
            "name": song.get("name", ""),
            "artists": song.get("artists", ""),
            "album": song.get("album", ""),
            "durationText": song.get("durationText") or song.get("duration", ""),
        }
    except Exception:
        logger.debug("读取当前播放信息失败", exc_info=True)
        return None


def _execute_control_action(action: str, body: dict, redis_client, area: str = "") -> dict:
    from web_player import execute_control_action
    return execute_control_action(action, body, redis_client, area=area)


def _execute_queue_action(action: str, index, redis_client, area: str = "") -> dict:
    from web_player import execute_queue_action
    return execute_queue_action(action, index, redis_client, area=area)


def _add_song_to_queue(body: dict, area: str = "") -> dict:
    from web_player import add_song_to_queue
    return add_song_to_queue(body, area=area)


# ---------------------------------------------------------------------------
# 管理后台页面路由
# ---------------------------------------------------------------------------

@admin_router.get("/admin", response_class=HTMLResponse)
def admin_index():
    return _render_admin_page("dashboard")


@admin_router.get("/admin/music", response_class=HTMLResponse)
def admin_music_page():
    return _render_admin_page("music")


@admin_router.get("/admin/config", response_class=HTMLResponse)
def admin_config_page():
    return _render_admin_page("config")


@admin_router.get("/admin/stats", response_class=HTMLResponse)
def admin_stats_page():
    return _render_admin_page("stats")


@admin_router.get("/admin/system", response_class=HTMLResponse)
def admin_system_page():
    return _render_admin_page("system")


@admin_router.get("/admin/activity", response_class=HTMLResponse)
def admin_activity_page():
    return _render_admin_page("activity")


@admin_router.get("/admin/scheduler", response_class=HTMLResponse)
def admin_scheduler_page():
    return _render_admin_page("scheduler")


@admin_router.get("/admin/areas", response_class=HTMLResponse)
def admin_areas_page():
    return _render_admin_page("areas")


@admin_router.get("/admin/plugins", response_class=HTMLResponse)
def admin_plugins_page():
    return _render_admin_page("plugins")


@admin_router.get("/admin/setup", response_class=HTMLResponse)
def admin_setup_page():
    return _render_admin_page("setup")



# ---------------------------------------------------------------------------
# 管理后台 API 路由
# ---------------------------------------------------------------------------

@admin_router.post("/admin/api/login")
async def admin_login(request: Request):
    if not _admin_enabled():
        return JSONResponse({"ok": False, "error": "管理后台未启用"}, status_code=404)
    password = cfg.admin_password()
    if not password:
        return JSONResponse({"ok": False, "error": "未配置 admin_password"}, status_code=503)
    body = await request.json()
    submitted = str(body.get("password", ""))
    if not secrets.compare_digest(submitted, password):
        return JSONResponse({"ok": False, "error": "密码错误"}, status_code=401)
    token = secrets.token_urlsafe(24)
    _set_admin_session_token(token)
    ttl = cfg.admin_session_ttl_seconds()
    response = JSONResponse({"ok": True, "ttl": ttl})
    response.set_cookie(
        key=cfg.admin_cookie_name(),
        value=token,
        httponly=True,
        samesite="lax",
        secure=cfg.admin_cookie_secure(),
        max_age=ttl if ttl > 0 else None,
    )
    return response


@admin_router.post("/admin/api/logout")
def admin_logout(request: Request):
    _clear_admin_session_token(request.cookies.get(cfg.admin_cookie_name(), ""))
    response = JSONResponse({"ok": True})
    response.delete_cookie(cfg.admin_cookie_name())
    return response


@admin_router.get("/admin/api/me")
def admin_me():
    return JSONResponse({"ok": True, "role": "admin"})


@admin_router.get("/admin/api/config")
def admin_get_config():
    return JSONResponse({
        "ok": True,
        "config": cfg.config_snapshot(),
        "runtime": {
            "music_area": _music_area_context(),
        },
        "overrides_path": cfg.ADMIN_OVERRIDES_PATH,
    })


@admin_router.post("/admin/api/config")
async def admin_update_config(request: Request):
    body = await request.json()
    updates = body.get("updates", {})
    persist = bool(body.get("persist", True))
    applied, errors, persist_payload = cfg.apply_config_updates(updates)
    music_runtime = {}

    import web_player
    if "redis" in applied:
        web_player.reset_redis(force=True)
    if "netease" in applied:
        web_player.reset_netease()
        _set_liked_ids_cache([])
    if {"netease", "qq_music", "bilibili_music"} & set(applied):
        try:
            music_runtime = web_player.refresh_music_platforms()
        except Exception as exc:
            logger.exception("刷新音乐平台运行时失败")
            errors.append(f"音乐平台刷新失败: {exc}")
    if "music" in applied and "default_volume" in applied["music"]:
        try:
            volume = cfg.default_music_volume()
            r = web_player.get_redis()
            r.set(web_player.KEY_VOLUME, str(volume))
            r.rpush(web_player.KEY_WEB_COMMANDS, f"volume:{volume}")
        except Exception as e:
            logger.debug("Apply default music volume failed: %s", e)
    cfg.refresh_runtime_dependents(set(applied))

    if persist and persist_payload:
        merged = cfg.merge_overrides(cfg.read_admin_overrides(), persist_payload)
        cfg.write_admin_overrides(merged)
    return JSONResponse({
        "ok": len(errors) == 0,
        "applied": applied,
        "errors": errors,
        "persisted": bool(persist and persist_payload),
        "config": cfg.config_snapshot(),
        "runtime": {
            "music_area": _music_area_context(),
            "music_platforms": music_runtime,
        },
    })


@admin_router.post("/admin/api/netease/login/qr")
async def admin_netease_login_qr(request: Request):
    """创建网易云扫码登录二维码。"""
    try:
        body = await request.json()
    except Exception:
        body = {}

    try:
        base_url = _normalize_netease_base_url(body.get("base_url"))
        logger.debug("网易云扫码登录二维码刷新请求开始: base_url=%s", base_url)
        key_payload, _ = _netease_api_get(
            base_url,
            "/login/qr/key",
            params=_netease_timestamp_params(),
        )
        key_data = _netease_response_data(key_payload)
        unikey = str(key_data.get("unikey") or key_payload.get("unikey") or "").strip()
        if not unikey:
            message = _netease_login_message(key_payload, "二维码 key 获取失败")
            logger.debug(
                "网易云扫码登录二维码 key 获取失败: code=%s message=%s",
                key_payload.get("code"),
                message,
            )
            return JSONResponse({"ok": False, "error": message}, status_code=502)

        qr_payload, _ = _netease_api_get(
            base_url,
            "/login/qr/create",
            params=_netease_timestamp_params({
                "key": unikey,
                "qrimg": "true",
            }),
        )
        qr_data = _netease_response_data(qr_payload)
        qrimg = str(qr_data.get("qrimg") or qr_payload.get("qrimg") or "").strip()
        qrurl = str(qr_data.get("qrurl") or qr_payload.get("qrurl") or "").strip()
        if qrimg and not qrimg.startswith("data:"):
            qrimg = f"data:image/png;base64,{qrimg}"
        if not qrimg and not qrurl:
            message = _netease_login_message(qr_payload, "二维码生成失败")
            logger.debug(
                "网易云扫码登录二维码响应缺少字段: key=%s qrimg_present=%s qrurl_present=%s message=%s",
                _mask_debug_token(unikey),
                bool(qrimg),
                bool(qrurl),
                message,
            )
            return JSONResponse({"ok": False, "error": message}, status_code=502)

        logger.debug(
            "网易云扫码登录二维码刷新成功: key=%s qrimg_len=%s qrurl_len=%s",
            _mask_debug_token(unikey),
            len(qrimg),
            len(qrurl),
        )
        return JSONResponse({
            "ok": True,
            "base_url": base_url,
            "key": unikey,
            "qrimg": qrimg,
            "qrurl": qrurl,
            "message": "二维码已刷新",
        })
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except RequestsException as exc:
        logger.warning("网易云扫码登录二维码请求失败: %s", exc)
        return JSONResponse({"ok": False, "error": f"网易云 API 请求失败: {exc}"}, status_code=502)
    except Exception as exc:
        logger.exception("创建网易云扫码登录二维码失败")
        return JSONResponse({"ok": False, "error": f"创建二维码失败: {exc}"}, status_code=500)


@admin_router.post("/admin/api/netease/login/qr/check")
async def admin_netease_login_qr_check(request: Request):
    """检查网易云扫码登录状态，成功时返回 Cookie。"""
    try:
        body = await request.json()
    except Exception:
        body = {}

    key = str(body.get("key") or "").strip()
    if not key:
        return JSONResponse({"ok": False, "error": "二维码 key 不能为空"}, status_code=400)

    try:
        base_url = _normalize_netease_base_url(body.get("base_url"))
        logger.debug("网易云扫码登录轮询开始: key=%s base_url=%s", _mask_debug_token(key), base_url)
        payload, response = _netease_api_get(
            base_url,
            "/login/qr/check",
            params=_netease_timestamp_params({"key": key}),
        )
        code = _netease_qr_code(payload)
        message = _netease_login_message(payload)
        status_map = {
            800: "expired",
            801: "waiting",
            802: "scanned",
            803: "success",
        }
        status = status_map.get(code, "unknown")
        logger.debug(
            "网易云扫码登录轮询结果: key=%s code=%s status=%s message=%s",
            _mask_debug_token(key),
            code,
            status,
            message,
        )
        result = {
            "ok": True,
            "base_url": base_url,
            "code": code,
            "status": status,
            "message": message,
        }
        if code == 803:
            cookie = _cookie_from_response(payload, response)
            if not cookie:
                return JSONResponse(
                    {"ok": False, "error": "扫码成功但网易云 API 未返回 Cookie"},
                    status_code=502,
                )
            result["cookie"] = cookie
            try:
                account_status = _netease_account_status(base_url, cookie)
                if account_status.get("logged_in"):
                    result["profile"] = account_status.get("profile")
                    logger.debug(
                        "网易云扫码登录成功并识别账号: key=%s %s",
                        _mask_debug_token(key),
                        _debug_profile_text(account_status.get("profile")),
                    )
                else:
                    result["profile_message"] = account_status.get("message", "")
                    logger.debug(
                        "网易云扫码登录成功但账号未识别: key=%s message=%s",
                        _mask_debug_token(key),
                        result["profile_message"],
                    )
            except Exception as exc:
                logger.debug("网易云扫码成功后查询账号信息失败: %s", exc)
                result["profile_message"] = f"账号信息查询失败: {exc}"
            result["message"] = message or "登录成功"
        return JSONResponse(result)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except RequestsException as exc:
        logger.warning("网易云扫码登录状态检查失败: %s", exc)
        return JSONResponse({"ok": False, "error": f"网易云 API 请求失败: {exc}"}, status_code=502)
    except Exception as exc:
        logger.exception("检查网易云扫码登录状态失败")
        return JSONResponse({"ok": False, "error": f"检查登录状态失败: {exc}"}, status_code=500)


@admin_router.get("/admin/api/netease/account")
def admin_netease_account():
    """返回当前配置 Cookie 对应的网易云账号信息。"""
    try:
        base_url = _normalize_netease_base_url(cfg.NETEASE_CLOUD.get("base_url"))
        cookie = str(cfg.NETEASE_CLOUD.get("cookie") or "")
        logger.debug("网易云账号状态接口请求: base_url=%s %s", base_url, _cookie_debug_summary(cookie))
        status = _netease_account_status(base_url, cookie)
        if status.get("logged_in"):
            logger.debug("网易云账号状态接口响应: logged_in=True %s", _debug_profile_text(status.get("profile")))
        else:
            logger.debug("网易云账号状态接口响应: logged_in=False message=%s", status.get("message", ""))
        return JSONResponse(status)
    except ValueError as exc:
        return JSONResponse({"ok": True, "logged_in": False, "message": str(exc)})
    except RequestsException as exc:
        logger.warning("网易云账号状态查询失败: %s", exc)
        return JSONResponse({"ok": True, "logged_in": False, "message": f"网易云 API 请求失败: {exc}"})
    except Exception as exc:
        logger.warning("网易云账号状态查询异常: %s", exc, exc_info=True)
        return JSONResponse({"ok": True, "logged_in": False, "message": f"账号状态查询失败: {exc}"})


@admin_router.post("/admin/api/bilibili/login/qr")
async def admin_bilibili_login_qr():
    """创建 B 站扫码登录二维码。"""
    try:
        logger.debug("B 站扫码登录二维码刷新请求开始")
        payload, _ = _bilibili_api_get(_BILIBILI_QR_GENERATE_PATH)
        if int(payload.get("code", -1)) != 0:
            message = _bilibili_login_message(payload, "二维码生成失败")
            logger.debug("B 站扫码登录二维码刷新失败: code=%s message=%s", payload.get("code"), message)
            return JSONResponse({"ok": False, "error": message}, status_code=502)

        data = _bilibili_response_data(payload)
        key = str(data.get("qrcode_key") or "").strip()
        qrurl = str(data.get("url") or "").strip()
        if not key or not qrurl:
            message = _bilibili_login_message(payload, "二维码生成失败")
            logger.debug(
                "B 站扫码登录二维码响应缺少字段: key_present=%s qrurl_present=%s message=%s",
                bool(key),
                bool(qrurl),
                message,
            )
            return JSONResponse({"ok": False, "error": message}, status_code=502)

        logger.debug(
            "B 站扫码登录二维码刷新成功: key=%s qrurl_len=%s",
            _mask_debug_token(key),
            len(qrurl),
        )
        return JSONResponse({
            "ok": True,
            "key": key,
            "qrimg": _make_qr_data_uri(qrurl),
            "qrurl": qrurl,
            "message": "二维码已刷新",
        })
    except RequestsException as exc:
        logger.warning("B 站扫码登录二维码请求失败: %s", exc)
        return JSONResponse({"ok": False, "error": f"B 站 API 请求失败: {exc}"}, status_code=502)
    except Exception as exc:
        logger.exception("创建 B 站扫码登录二维码失败")
        return JSONResponse({"ok": False, "error": f"创建二维码失败: {exc}"}, status_code=500)


@admin_router.post("/admin/api/bilibili/login/qr/check")
async def admin_bilibili_login_qr_check(request: Request):
    """检查 B 站扫码登录状态，成功时返回 Cookie。"""
    try:
        body = await request.json()
    except Exception:
        body = {}

    key = str(body.get("key") or "").strip()
    if not key:
        return JSONResponse({"ok": False, "error": "二维码 key 不能为空"}, status_code=400)

    try:
        logger.debug("B 站扫码登录轮询开始: key=%s", _mask_debug_token(key))
        payload, response = _bilibili_api_get(
            _BILIBILI_QR_POLL_PATH,
            params={"qrcode_key": key},
        )
        if int(payload.get("code", -1)) != 0:
            message = _bilibili_login_message(payload, "登录状态检查失败")
            logger.debug(
                "B 站扫码登录轮询接口失败: key=%s code=%s message=%s",
                _mask_debug_token(key),
                payload.get("code"),
                message,
            )
            return JSONResponse({"ok": False, "error": message}, status_code=502)

        code = _bilibili_qr_code(payload)
        message = _bilibili_login_message(payload)
        status_map = {
            0: "success",
            86038: "expired",
            86090: "scanned",
            86101: "waiting",
        }
        status = status_map.get(code, "unknown")
        logger.debug(
            "B 站扫码登录轮询结果: key=%s code=%s status=%s message=%s",
            _mask_debug_token(key),
            code,
            status,
            message,
        )
        result = {
            "ok": True,
            "code": code,
            "status": status,
            "message": message,
        }
        if code == 0:
            cookie = _bilibili_cookie_from_poll(payload, response)
            if not cookie:
                return JSONResponse(
                    {"ok": False, "error": "扫码成功但 B 站未返回 Cookie"},
                    status_code=502,
                )
            result["cookie"] = cookie
            try:
                account_status = _bilibili_account_status(cookie)
                if account_status.get("logged_in"):
                    result["profile"] = account_status.get("profile")
                    logger.debug(
                        "B 站扫码登录成功并识别账号: key=%s %s",
                        _mask_debug_token(key),
                        _debug_profile_text(account_status.get("profile")),
                    )
                else:
                    result["profile_message"] = account_status.get("message", "")
                    logger.debug(
                        "B 站扫码登录成功但账号未识别: key=%s message=%s",
                        _mask_debug_token(key),
                        result["profile_message"],
                    )
            except Exception as exc:
                logger.debug("B 站扫码成功后查询账号信息失败: %s", exc)
                result["profile_message"] = f"账号信息查询失败: {exc}"
            result["message"] = message or "登录成功"
        return JSONResponse(result)
    except RequestsException as exc:
        logger.warning("B 站扫码登录状态检查失败: %s", exc)
        return JSONResponse({"ok": False, "error": f"B 站 API 请求失败: {exc}"}, status_code=502)
    except Exception as exc:
        logger.exception("检查 B 站扫码登录状态失败")
        return JSONResponse({"ok": False, "error": f"检查登录状态失败: {exc}"}, status_code=500)


@admin_router.get("/admin/api/bilibili/account")
def admin_bilibili_account():
    """返回当前配置 Cookie 对应的 B 站账号信息。"""
    try:
        cookie = str(cfg.BILIBILI_MUSIC_CONFIG.get("cookie") or "")
        logger.debug("B 站账号状态接口请求: %s", _cookie_debug_summary(cookie))
        result = _bilibili_account_status(cookie)
        if result.get("logged_in"):
            logger.debug("B 站账号状态接口响应: logged_in=True %s", _debug_profile_text(result.get("profile")))
        else:
            logger.debug("B 站账号状态接口响应: logged_in=False message=%s", result.get("message", ""))
        return JSONResponse(result)
    except RequestsException as exc:
        logger.warning("B 站账号状态查询失败: %s", exc)
        return JSONResponse({"ok": True, "logged_in": False, "message": f"B 站 API 请求失败: {exc}"})
    except Exception as exc:
        logger.warning("B 站账号状态查询异常: %s", exc, exc_info=True)
        return JSONResponse({"ok": True, "logged_in": False, "message": f"账号状态查询失败: {exc}"})


@admin_router.post("/admin/api/config/reset")
def admin_reset_config_overrides():
    if os.path.exists(cfg.ADMIN_OVERRIDES_PATH):
        os.remove(cfg.ADMIN_OVERRIDES_PATH)
    for group_name, group in cfg.CONFIG_GROUPS.items():
        target = group.get("target")
        baseline = cfg.CONFIG_BASELINES.get(group_name)
        if isinstance(target, dict) and isinstance(baseline, dict):
            target.clear()
            target.update(copy.deepcopy(baseline))

    import web_player
    web_player.reset_redis(force=True)
    web_player.reset_netease()
    _set_liked_ids_cache([])
    try:
        music_runtime = web_player.refresh_music_platforms()
    except Exception as exc:
        logger.debug("重置配置后刷新音乐平台失败: %s", exc)
        music_runtime = {"available": False, "error": str(exc)}
    cfg.refresh_runtime_dependents({"redis", "web_player"})
    return JSONResponse({"ok": True, "removed": True, "path": cfg.ADMIN_OVERRIDES_PATH, "music_platforms": music_runtime})


def _parse_oopz_login_payload(body: dict[str, Any]) -> tuple[str, str, float]:
    if not isinstance(body, dict):
        body = {}
    phone = str(body.get("phone", "") or "").strip()
    password = str(body.get("password", "") or "")
    try:
        timeout = float(body.get("timeout", 90) or 90)
    except (TypeError, ValueError):
        timeout = 90.0
    return phone, password, max(30.0, min(timeout, 180.0))


def _client_safe_oopz_login_result(result: dict[str, Any]) -> dict[str, Any]:
    """移除仅供服务端热更新用的原始凭据。"""
    return {key: value for key, value in result.items() if key != "raw"}


@admin_router.post("/admin/api/oopz/login")
async def admin_oopz_login(request: Request):
    if _oopz_login_lock.locked():
        return JSONResponse({"ok": False, "error": "OOPZ 登录任务正在执行"}, status_code=409)

    phone, password, timeout = _parse_oopz_login_payload(await request.json())

    async with _oopz_login_lock:
        try:
            from oopz_password_login import OopzPasswordLoginError, login_with_password

            result = await login_with_password(phone, password, timeout=timeout, headless=True, save=True)
            raw_credentials = result.get("raw", {})
            runtime = _refresh_oopz_runtime(raw_credentials)
            saved = result.get("saved") or []
            return JSONResponse({
                **_client_safe_oopz_login_result(result),
                "runtime": runtime,
                "message": "OOPZ 登录成功，已保存到: " + ("、".join(saved) if saved else "运行时"),
            })
        except OopzPasswordLoginError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        except Exception as exc:
            logger.exception("后台 OOPZ 账号密码登录失败")
            return JSONResponse({"ok": False, "error": f"OOPZ 登录失败: {exc}"}, status_code=500)


@admin_router.get("/admin/api/overview")
def admin_overview():
    return JSONResponse(_overview_payload(), headers={"Cache-Control": "no-store"})


@admin_router.get("/admin/api/overview/stream")
async def admin_overview_stream(request: Request):
    cookie_token = request.cookies.get(cfg.admin_cookie_name(), "")

    async def _event_stream():
        last_payload = ""
        check_counter = 0
        while True:
            if await request.is_disconnected():
                break
            check_counter += 1
            if check_counter % 30 == 0 and cookie_token:
                try:
                    alive = _get_redis().get(cfg.admin_session_key(cookie_token))
                except Exception:
                    alive = None
                if not alive:
                    break
            payload = _overview_payload()
            payload_text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            if payload_text != last_payload:
                yield f"event: overview\ndata: {payload_text}\n\n"
                last_payload = payload_text
            else:
                yield ": keepalive\n\n"
            await asyncio.sleep(1.0)

    return StreamingResponse(
        _event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@admin_router.get("/admin/api/statistics")
def admin_statistics(
    days: int = Query(7, ge=1, le=30),
    top_page: int = Query(1, ge=1),
    top_page_size: int = Query(10, ge=1, le=100),
):
    top_items, top_total = _top_songs_from_play_history(page=top_page, page_size=top_page_size)
    top_pages = max(1, (top_total + top_page_size - 1) // top_page_size) if top_total else 1
    return JSONResponse({
        "ok": True,
        "today": Statistics.get_today() or {},
        "summary": Statistics.get_summary(),
        "recent_days": Statistics.get_recent(days=days),
        "top_songs": top_items,
        "top_total": top_total,
        "top_page": top_page,
        "top_pages": top_pages,
        "top_page_size": top_page_size,
        "recent_songs": SongCache.get_recent_songs(limit=10),
    })


@admin_router.post("/admin/api/statistics/clear_history")
def admin_clear_play_history():
    count = SongCache.clear_play_history()
    return JSONResponse({"ok": True, "deleted": count})


@admin_router.get("/admin/api/logs")
def admin_logs(
    tail: int | None = Query(default=None, ge=1, le=2000),
    lines: int | None = Query(default=None, ge=1, le=2000),
):
    tail_count = tail if tail is not None else lines
    if tail_count is None:
        tail_count = 200
    log_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs", "oopz_bot.log")
    line_list = _tail_file(log_path, lines=tail_count)
    return JSONResponse(
        {"ok": True, "path": log_path, "lines": line_list, "logs": line_list, "count": len(line_list)},
        headers={"Cache-Control": "no-store"},
    )


@admin_router.post("/admin/api/control")
async def admin_control(request: Request):
    try:
        body = await request.json()
        action = str(body.get("action", ""))
        area = _get_music_area()
        result = _execute_control_action(action=action, body=body, redis_client=_get_redis(), area=area)
        return JSONResponse(result)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@admin_router.post("/admin/api/liked/refresh")
def admin_liked_refresh():
    _set_liked_ids_cache([])
    return JSONResponse({"ok": True})


@admin_router.post("/admin/api/queue/clear")
def admin_queue_clear():
    area = _get_music_area()
    _get_redis().delete(_area_key(KEY_QUEUE, area))
    return JSONResponse({"ok": True})


@admin_router.get("/admin/api/queue")
def admin_queue(
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
):
    r = _get_redis()
    area_context = _music_area_context(r)
    area = area_context.get("area", "")
    full_queue = _queue_snapshot(r, area=area)
    total = len(full_queue)
    pages = max(1, (total + page_size - 1) // page_size) if total else 1
    page = min(page, pages)
    start = (page - 1) * page_size
    queue = full_queue[start:start + page_size]
    current = _current_song_snapshot(r, area=area)
    return JSONResponse({
        "ok": True,
        "area": area,
        "music_area": area_context,
        "current": current,
        "queue": queue,
        "count": len(queue),
        "total": total,
        "page": page,
        "pages": pages,
        "page_size": page_size,
    })


@admin_router.post("/admin/api/queue/action")
async def admin_queue_action(request: Request):
    body = await request.json()
    area = _get_music_area()
    result = _execute_queue_action(
        action=body.get("action", ""),
        index=body.get("index", -1),
        redis_client=_get_redis(),
        area=area,
    )
    if result.get("ok"):
        result["queue"] = _queue_snapshot(_get_redis(), area=area)
    return JSONResponse(result)


@admin_router.get("/admin/api/player/link")
def admin_player_link():
    token = get_token(redis_client=_get_redis())
    path = f"/w/{token}" if token else ""
    base_url = cfg.display_web_base_url()
    full_url = f"{base_url}{path}" if token else ""
    return JSONResponse({
        "ok": True,
        "has_token": bool(token),
        "path": path,
        "url": full_url,
        "base_url": base_url,
    })


@admin_router.post("/admin/api/player/link/rotate")
def admin_player_link_rotate():
    r = _get_redis()
    clear_token(redis_client=r)
    token = ensure_token(redis_client=r, ttl_seconds=cfg.token_ttl_seconds())
    base_url = cfg.display_web_base_url()
    return JSONResponse({"ok": True, "url": f"{base_url}/w/{token}"})


@admin_router.get("/admin/api/search")
def admin_search(
    keyword: str = Query(..., min_length=1),
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=30),
    platform: str = Query("netease"),
):
    try:
        page = max(1, int(page))
        page_size = max(1, min(int(page_size), 30))
        offset = (page - 1) * page_size

        if platform == "netease":
            nc = _get_netease()
            data = nc._get("/cloudsearch", params={
                "keywords": keyword,
                "limit": page_size,
                "offset": offset,
                "type": 1,
            })
            if not data or data.get("code") != 200:
                return JSONResponse({"ok": False, "error": "搜索失败", "results": []})
            songs = data.get("result", {}).get("songs", [])
            total = int(data.get("result", {}).get("songCount", 0) or 0)
            pages = max(1, (total + page_size - 1) // page_size) if total else 1
            results = []
            for song in songs:
                parsed = nc._parse_song(song)
                if parsed:
                    results.append(parsed)
        else:
            from web_player import _resolve_platform
            p = _resolve_platform(platform)
            results = p.search_many(keyword, limit=page_size, offset=offset)
            total = len(results)
            pages = 1

        return JSONResponse({
            "ok": True,
            "results": results,
            "total": total,
            "page": page,
            "pages": pages,
            "page_size": page_size,
            "platform": platform,
        })
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e), "results": []})


@admin_router.post("/admin/api/add")
async def admin_add(request: Request):
    try:
        body = await request.json()
        return JSONResponse(_add_song_to_queue(body=body, area=_get_music_area()))
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@admin_router.get("/admin/api/system")
def admin_system():
    data: dict = {
        "ok": True,
        "python_version": sys.version.split()[0],
        "platform": sys.platform,
        "project_root": cfg.PROJECT_ROOT,
        "db_path": DB_PATH,
        "db_exists": os.path.exists(DB_PATH),
        "db_size_bytes": os.path.getsize(DB_PATH) if os.path.exists(DB_PATH) else 0,
        "log_path": os.path.join(cfg.PROJECT_ROOT, "logs", "oopz_bot.log"),
        "uptime_seconds": int(time.time() - _get_started_at()),
    }
    log_path = data["log_path"]
    data["log_size_bytes"] = os.path.getsize(log_path) if os.path.exists(log_path) else 0
    try:
        r = _get_redis()
        r.ping()
        info = r.info(section="server")
        data["redis"] = {
            "status": "connected",
            "dbsize": int(r.dbsize() or 0),
            "redis_version": info.get("redis_version", ""),
        }
    except Exception as e:
        data["redis"] = {"status": f"error: {e}"}
    try:
        with db_connection() as conn:
            table_rows: dict = {}
            for table in ("image_cache", "song_cache", "play_history", "statistics"):
                row = conn.execute(f"SELECT COUNT(1) AS c FROM {table}").fetchone()
                table_rows[table] = int(row["c"] if row else 0)
        data["db_tables"] = table_rows
    except Exception as e:
        data["db_tables"] = {"error": str(e)}
    return JSONResponse(data)


@admin_router.get("/admin/api/setup/diagnostics")
def admin_setup_diagnostics():
    diagnostics = SetupDiagnostics(sender=_get_sender(), plugins=_get_plugin_runtime())
    report = diagnostics.build_report()
    return JSONResponse({"ok": True, **report}, headers={"Cache-Control": "no-store"})


# ---------------------------------------------------------------------------
# 定时消息 CRUD API
# ---------------------------------------------------------------------------

@admin_router.get("/admin/api/scheduled-messages")
def admin_scheduled_messages_list():
    return JSONResponse({"ok": True, "items": ScheduledMessageDB.get_all()})


@admin_router.get("/admin/api/scheduled-message-templates")
def admin_scheduled_message_templates():
    return JSONResponse({"ok": True, "items": list_scheduled_templates()})


@admin_router.post("/admin/api/scheduled-message-templates/{template_key}/apply")
async def admin_scheduled_message_template_apply(template_key: str, request: Request):
    template = get_scheduled_template(template_key)
    if not template:
        return JSONResponse({"ok": False, "error": "未找到定时模板"}, status_code=404)
    body = await request.json()
    channel_id = str(body.get("channel_id") or "").strip()
    area_id = str(body.get("area_id") or "").strip()
    if not channel_id or not area_id:
        return JSONResponse({"ok": False, "error": "channel_id/area_id 不能为空"}, status_code=400)
    name = str(body.get("name") or template["name"]).strip()
    message_text = str(body.get("message_text") or template["message_text"]).strip()
    weekdays = str(body.get("weekdays") or template["weekdays"]).strip()
    try:
        cron_hour = int(body.get("cron_hour", template["cron_hour"]))
        cron_minute = int(body.get("cron_minute", template["cron_minute"]))
    except (TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "cron_hour/cron_minute 必须为整数"}, status_code=400)
    if not name or not message_text:
        return JSONResponse({"ok": False, "error": "name/message_text 不能为空"}, status_code=400)
    task_id = ScheduledMessageDB.create(
        name=name,
        cron_hour=cron_hour,
        cron_minute=cron_minute,
        channel_id=channel_id,
        area_id=area_id,
        message_text=message_text,
        weekdays=weekdays,
    )
    return JSONResponse({"ok": True, "id": task_id, "template": template["key"]})


@admin_router.post("/admin/api/scheduled-messages")
async def admin_scheduled_messages_create(request: Request):
    body = await request.json()
    name = str(body.get("name") or "").strip()
    try:
        hour = int(body.get("cron_hour", 0))
        minute = int(body.get("cron_minute", 0))
    except (TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "cron_hour/cron_minute 必须为整数"}, status_code=400)
    weekdays = str(body.get("weekdays", "0,1,2,3,4,5,6"))
    channel_id = str(body.get("channel_id") or "").strip()
    area_id = str(body.get("area_id") or "").strip()
    message_text = str(body.get("message_text") or "").strip()
    if not name or not channel_id or not area_id or not message_text:
        return JSONResponse({"ok": False, "error": "name/channel_id/area_id/message_text 不能为空"}, status_code=400)
    task_id = ScheduledMessageDB.create(
        name=name, cron_hour=hour, cron_minute=minute,
        channel_id=channel_id, area_id=area_id, message_text=message_text,
        weekdays=weekdays,
    )
    return JSONResponse({"ok": True, "id": task_id})


@admin_router.put("/admin/api/scheduled-messages/{task_id}")
async def admin_scheduled_messages_update(task_id: int, request: Request):
    body = await request.json()
    updated = ScheduledMessageDB.update(task_id, **body)
    if not updated:
        return JSONResponse({"ok": False, "error": "未找到或无变更"}, status_code=404)
    return JSONResponse({"ok": True})


@admin_router.delete("/admin/api/scheduled-messages/{task_id}")
def admin_scheduled_messages_delete(task_id: int):
    deleted = ScheduledMessageDB.delete(task_id)
    if not deleted:
        return JSONResponse({"ok": False, "error": "未找到"}, status_code=404)
    return JSONResponse({"ok": True})


@admin_router.post("/admin/api/scheduled-messages/{task_id}/toggle")
def admin_scheduled_messages_toggle(task_id: int):
    result = ScheduledMessageDB.toggle(task_id)
    if result is None:
        return JSONResponse({"ok": False, "error": "未找到"}, status_code=404)
    return JSONResponse({"ok": True, "enabled": result})


# ---------------------------------------------------------------------------
# 消息统计 API
# ---------------------------------------------------------------------------

@admin_router.get("/admin/api/message-stats/daily")
def admin_message_stats_daily(days: int = Query(14, ge=1, le=90)):
    daily = MessageStatsDB.get_all_daily(days=days)
    return JSONResponse({"ok": True, "daily": daily})


@admin_router.get("/admin/api/message-stats/ranking")
def admin_message_stats_ranking(
    days: int = Query(7, ge=1, le=90),
    limit: int = Query(10, ge=1, le=50),
    area_id: str = Query(""),
):
    if not area_id:
        from database import db_connection as _dbc
        with _dbc() as conn:
            row = conn.execute(
                "SELECT DISTINCT area_id FROM message_stats LIMIT 1"
            ).fetchone()
            area_id = row["area_id"] if row else ""
    if not area_id:
        return JSONResponse({"ok": True, "ranking": []})
    ranking = MessageStatsDB.get_user_ranking(area_id, days=days, limit=limit)
    resolver = get_resolver()
    for item in ranking:
        item["display_name"] = resolver.user(item["user_id"])
    return JSONResponse({"ok": True, "ranking": ranking})


@admin_router.get("/admin/api/message-stats/overview")
def admin_message_stats_overview():
    return JSONResponse({
        "ok": True,
        "today_messages": MessageStatsDB.get_today_total(),
        "week_messages": MessageStatsDB.get_week_total(),
        "active_users_today": MessageStatsDB.get_active_users_today(),
    })


# ---------------------------------------------------------------------------
# 提醒查看 API
# ---------------------------------------------------------------------------

@admin_router.get("/admin/api/reminders")
def admin_reminders_list():
    return JSONResponse({"ok": True, "items": ReminderDB.get_all_pending()})


# ---------------------------------------------------------------------------
# 成员管理页面 & API
# ---------------------------------------------------------------------------

@admin_router.get("/admin/members", response_class=HTMLResponse)
def admin_members_page():
    return _render_admin_page("members")


_areas_cache: dict = {"data": None, "ts": 0.0}
_AREAS_CACHE_TTL = 120.0
_area_meta_cache: dict = {"data": None, "ts": 0.0, "area": ""}
_AREA_META_CACHE_TTL = 120.0


@admin_router.get("/admin/api/areas")
def admin_areas_list():
    """返回 Bot 已加入的域列表,供前端域选择器使用。"""
    now = time.time()
    if _areas_cache["data"] and now - _areas_cache["ts"] < _AREAS_CACHE_TTL:
        return JSONResponse(_areas_cache["data"])
    sender = _get_sender()
    if not sender:
        return JSONResponse({"ok": False, "error": "sender 未初始化"}, status_code=503)
    areas = sender.get_joined_areas(quiet=True)
    items = []
    for a in areas:
        items.append({
            "id": a.get("id", ""),
            "name": a.get("name", ""),
            "code": a.get("code", ""),
            "avatar": a.get("avatar", ""),
        })
    resp = {"ok": True, "areas": items}
    _areas_cache.update(data=resp, ts=now)
    return JSONResponse(resp)


@admin_router.get("/admin/api/areas/{area_id}/meta")
def admin_area_meta(area_id: str):
    """返回域的表单辅助数据，如身份组列表。"""
    resolved_area = area_id.strip() or _resolve_area()
    if not resolved_area:
        return JSONResponse({"ok": False, "error": "未找到可用域 ID"})

    now = time.time()
    if (_area_meta_cache["data"] and _area_meta_cache["area"] == resolved_area
            and now - _area_meta_cache["ts"] < _AREA_META_CACHE_TTL):
        return JSONResponse(_area_meta_cache["data"])

    sender = _get_sender()
    if not sender:
        return JSONResponse({"ok": False, "error": "sender 未初始化"}, status_code=503)

    area_info = sender.get_area_info(area=resolved_area)
    if not isinstance(area_info, dict) or "error" in area_info:
        err = area_info.get("error") if isinstance(area_info, dict) else "获取域信息失败"
        return JSONResponse({"ok": False, "error": err or "获取域信息失败"})

    roles = []
    for role in area_info.get("roleList") or []:
        role_id = role.get("roleID")
        if role_id is None:
            continue
        roles.append({
            "id": str(role_id),
            "name": str(role.get("name", "") or ""),
            "sort": int(role.get("sort", 0) or 0),
            "type": int(role.get("type", 0) or 0),
        })
    roles.sort(key=lambda item: (-item["sort"], item["name"], item["id"]))

    resp = {
        "ok": True,
        "area": resolved_area,
        "home_page_channel_id": str(area_info.get("homePageChannelId", "") or ""),
        "roles": roles,
    }
    _area_meta_cache.update(data=resp, ts=now, area=resolved_area)
    return JSONResponse(resp)


@admin_router.get("/admin/api/members")
def admin_members_list(
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    keyword: str = Query(""),
    area: str = Query(""),
):
    resolved_area = area.strip() if area.strip() else _resolve_area()
    cache_key = f"{resolved_area}:{offset}:{limit}"
    now = time.time()
    if not keyword and _members_resp_cache["data"] and _members_resp_cache["key"] == cache_key \
            and now - _members_resp_cache["ts"] < _MEMBERS_RESP_TTL:
        return JSONResponse(_members_resp_cache["data"])

    sender = _get_sender()
    if not sender:
        return JSONResponse({"ok": False, "error": "sender 未初始化"}, status_code=503)

    if not resolved_area:
        return JSONResponse({"ok": False, "error": "未找到可用域 ID，请检查配置"})

    result = sender.get_area_members(area=resolved_area, offset_start=offset, offset_end=offset + limit - 1, quiet=True)
    if "error" in result:
        time.sleep(1)
        result = sender.get_area_members(area=resolved_area, offset_start=offset, offset_end=offset + limit - 1)
    if "error" in result:
        return JSONResponse({"ok": False, "error": result["error"]})

    members = result.get("members") or []
    total = result.get("totalCount") or result.get("userCount", len(members))
    online = result.get("onlineCount", 0)
    is_stale = result.get("stale", False)

    uids = [m.get("uid", "") for m in members if m.get("uid")]
    person_map: dict = {}
    if uids:
        try:
            person_map = sender.get_person_infos_batch(uids)
        except Exception:
            logger.debug("批量获取用户信息失败", exc_info=True)

    area_info = None
    try:
        area_info = sender.get_area_info(area=resolved_area)
    except Exception:
        logger.debug("获取域信息失败 (area=%s)", resolved_area[:8] if resolved_area else "")
    role_name_map: dict[int, str] = {}
    if area_info and isinstance(area_info, dict) and "error" not in area_info:
        for r in area_info.get("roleList") or []:
            rid = r.get("roleID")
            if rid is not None:
                role_name_map[int(rid)] = r.get("name", "")

    if keyword:
        kw = keyword.lower()
        filtered = []
        for m in members:
            uid = m.get("uid", "")
            pi = person_map.get(uid, {})
            name = pi.get("name", "") or uid[:8]
            if kw in name.lower() or kw in uid.lower() or kw in (pi.get("pid") or "").lower():
                filtered.append(m)
        members = filtered
        total = len(filtered)

    from config import ADMIN_UIDS
    admin_set = set(ADMIN_UIDS)

    items = []
    for m in members:
        uid = m.get("uid", "")
        pi = person_map.get(uid, {})
        role_id = m.get("role", 0)
        items.append({
            "uid": uid,
            "name": pi.get("name") or uid[:8],
            "avatar": pi.get("avatar", ""),
            "pid": pi.get("pid", ""),
            "online": m.get("online", 0) == 1,
            "role": role_id,
            "roleName": role_name_map.get(int(role_id), "") if role_id else "",
            "roleSort": m.get("roleSort", 0),
            "playingState": m.get("playingState", ""),
            "displayType": m.get("displayType", ""),
            "is_bot_admin": uid in admin_set,
        })
    resp_data: dict = {
        "ok": True,
        "members": items,
        "total": total,
        "online": online,
        "offset": offset,
        "limit": limit,
    }
    if is_stale:
        resp_data["stale"] = True
    if not keyword:
        _members_resp_cache.update(data=resp_data, ts=time.time(), key=cache_key)
    return JSONResponse(resp_data)


@admin_router.get("/admin/api/members/blocks")
def admin_members_blocks(area: str = Query("")):
    sender = _get_sender()
    if not sender:
        return JSONResponse({"ok": False, "error": "sender 未初始化"}, status_code=503)
    area = area.strip() or _resolve_area()
    data = sender.get_area_blocks(area=area) if area else {"error": "未找到可用域 ID"}
    if "error" in data:
        return JSONResponse({"ok": True, "blocks": [], "error_hint": data["error"]})
    resolver = get_resolver()
    blocks = []
    for item in data.get("blocks") or []:
        uid = item.get("uid") or item.get("person") or item.get("target") or ""
        if isinstance(uid, dict):
            uid = uid.get("uid") or uid.get("person") or ""
        name = resolver.user(uid) if isinstance(uid, str) and uid else ""
        blocks.append({"uid": uid, "name": name or uid[:12]})
    return JSONResponse({"ok": True, "blocks": blocks})


@admin_router.get("/admin/api/members/{uid}")
def admin_member_detail(uid: str, area: str = Query("")):
    sender = _get_sender()
    if not sender:
        return JSONResponse({"ok": False, "error": "sender 未初始化"}, status_code=503)
    area = area.strip() or _resolve_area()
    detail = sender.get_user_area_detail(uid, area=area) if area else {"error": "未找到域 ID"}
    if "error" in detail:
        return JSONResponse({"ok": False, "error": detail["error"]})
    person = sender.get_person_detail(uid)
    assignable = sender.get_assignable_roles(uid, area=area) if area else []
    default_area = area
    stats_data = MessageStatsDB.get_user_ranking(
        area_id=default_area,
        days=7,
        limit=100,
    )
    user_msg_count = 0
    for s in stats_data:
        if s.get("user_id") == uid:
            user_msg_count = s.get("total", 0)
            break

    person_data: dict = {}
    if "error" not in person:
        person_data = {
            "name": person.get("name") or person.get("nickname") or uid[:8],
            "avatar": person.get("avatar") or "",
            "pid": person.get("pid") or person.get("userCommonId") or "",
            "online": bool(person.get("online")),
            "introduction": person.get("introduction") or "",
        }

    role_list = detail.get("list") or []
    roles_out = []
    for r in role_list:
        roles_out.append({
            "roleID": r.get("roleID"),
            "name": r.get("name", ""),
        })

    disable_text_to = detail.get("disableTextTo", 0)
    disable_voice_to = detail.get("disableVoiceTo", 0)
    now_ms = int(time.time() * 1000)
    is_muted = isinstance(disable_text_to, (int, float)) and int(disable_text_to) > now_ms
    is_mic_muted = isinstance(disable_voice_to, (int, float)) and int(disable_voice_to) > now_ms

    from config import ADMIN_UIDS
    is_bot_admin = uid in ADMIN_UIDS

    return JSONResponse({
        "ok": True,
        "uid": uid,
        "person": person_data,
        "roles": roles_out,
        "muted": is_muted,
        "muted_until": int(disable_text_to) if is_muted else 0,
        "mic_muted": is_mic_muted,
        "mic_muted_until": int(disable_voice_to) if is_mic_muted else 0,
        "assignable_roles": assignable if isinstance(assignable, list) else [],
        "messages_7d": user_msg_count,
        "is_bot_admin": is_bot_admin,
    })


def _extract_area(body: dict) -> str:
    return (body.get("area") or "").strip() or _resolve_area()


@admin_router.post("/admin/api/members/{uid}/mute")
async def admin_member_mute(uid: str, request: Request):
    sender = _get_sender()
    if not sender:
        return JSONResponse({"ok": False, "error": "sender 未初始化"}, status_code=503)
    body = await request.json()
    area = _extract_area(body)
    try:
        duration = int(body.get("duration", 5))
    except (TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "duration 必须为整数"}, status_code=400)
    result = sender.mute_user(uid, area=area, duration=duration)
    if "error" in result:
        return JSONResponse({"ok": False, "error": result["error"]})
    _invalidate_members_cache()
    return JSONResponse({"ok": True, "message": result.get("message", "已禁言")})


@admin_router.post("/admin/api/members/{uid}/unmute")
async def admin_member_unmute(uid: str, request: Request):
    sender = _get_sender()
    if not sender:
        return JSONResponse({"ok": False, "error": "sender 未初始化"}, status_code=503)
    body = await request.json()
    area = _extract_area(body)
    result = sender.unmute_user(uid, area=area)
    if "error" in result:
        return JSONResponse({"ok": False, "error": result["error"]})
    _invalidate_members_cache()
    return JSONResponse({"ok": True, "message": result.get("message", "已解除禁言")})


@admin_router.post("/admin/api/members/{uid}/mute-mic")
async def admin_member_mute_mic(uid: str, request: Request):
    sender = _get_sender()
    if not sender:
        return JSONResponse({"ok": False, "error": "sender 未初始化"}, status_code=503)
    body = await request.json()
    area = _extract_area(body)
    try:
        duration = int(body.get("duration", 10))
    except (TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "duration 必须为整数"}, status_code=400)
    result = sender.mute_mic(uid, area=area, duration=duration)
    if "error" in result:
        return JSONResponse({"ok": False, "error": result["error"]})
    _invalidate_members_cache()
    return JSONResponse({"ok": True, "message": result.get("message", "已禁麦")})


@admin_router.post("/admin/api/members/{uid}/unmute-mic")
async def admin_member_unmute_mic(uid: str, request: Request):
    sender = _get_sender()
    if not sender:
        return JSONResponse({"ok": False, "error": "sender 未初始化"}, status_code=503)
    body = await request.json()
    area = _extract_area(body)
    result = sender.unmute_mic(uid, area=area)
    if "error" in result:
        return JSONResponse({"ok": False, "error": result["error"]})
    _invalidate_members_cache()
    return JSONResponse({"ok": True, "message": result.get("message", "已解除禁麦")})


@admin_router.post("/admin/api/members/{uid}/kick")
async def admin_member_kick(uid: str, request: Request):
    sender = _get_sender()
    if not sender:
        return JSONResponse({"ok": False, "error": "sender 未初始化"}, status_code=503)
    body = await request.json()
    area = _extract_area(body)
    result = sender.remove_from_area(uid, area=area)
    if "error" in result:
        return JSONResponse({"ok": False, "error": result["error"]})
    _invalidate_members_cache()
    return JSONResponse({"ok": True, "message": result.get("message", "已踢出")})


@admin_router.post("/admin/api/members/{uid}/block")
async def admin_member_block(uid: str, request: Request):
    sender = _get_sender()
    if not sender:
        return JSONResponse({"ok": False, "error": "sender 未初始化"}, status_code=503)
    body = await request.json()
    area = _extract_area(body)
    result = sender.block_user_in_area(uid, area=area)
    if "error" in result:
        return JSONResponse({"ok": False, "error": result["error"]})
    _invalidate_members_cache()
    return JSONResponse({"ok": True, "message": result.get("message", "已封禁")})


@admin_router.post("/admin/api/members/{uid}/unblock")
async def admin_member_unblock(uid: str, request: Request):
    sender = _get_sender()
    if not sender:
        return JSONResponse({"ok": False, "error": "sender 未初始化"}, status_code=503)
    body = await request.json()
    area = _extract_area(body)
    result = sender.unblock_user_in_area(uid, area=area)
    if "error" in result:
        return JSONResponse({"ok": False, "error": result["error"]})
    _invalidate_members_cache()
    return JSONResponse({"ok": True, "message": result.get("message", "已解封")})


@admin_router.get("/admin/api/bot-admins")
def admin_bot_admins_list():
    """返回当前 Bot 管理员 UID 列表。"""
    from config import ADMIN_UIDS
    resolver = get_resolver()
    items = []
    for uid in ADMIN_UIDS:
        items.append({"uid": uid, "name": resolver.user(uid) or uid[:12]})
    return JSONResponse({"ok": True, "admins": items, "uids": list(ADMIN_UIDS)})


@admin_router.post("/admin/api/bot-admins")
async def admin_bot_admins_add(request: Request):
    """将指定 UID 添加为 Bot 管理员。"""
    import config as _cfg
    body = await request.json()
    uid = str(body.get("uid", "")).strip()
    if not uid:
        return JSONResponse({"ok": False, "error": "uid 不能为空"}, status_code=400)
    if uid in _cfg.ADMIN_UIDS:
        return JSONResponse({"ok": True, "message": "该用户已是管理员"})
    _cfg.ADMIN_UIDS.append(uid)
    _persist_admin_uids(_cfg.ADMIN_UIDS)
    resolver = get_resolver()
    name = resolver.user(uid) or uid[:12]
    logger.info("Bot 管理员已添加: %s (%s)", name, uid[:12])
    return JSONResponse({"ok": True, "message": f"已将 {name} 设为管理员"})


@admin_router.delete("/admin/api/bot-admins/{uid}")
def admin_bot_admins_remove(uid: str):
    """移除指定 UID 的 Bot 管理员权限。"""
    import config as _cfg
    uid = uid.strip()
    if uid not in _cfg.ADMIN_UIDS:
        return JSONResponse({"ok": False, "error": "该用户不是管理员"}, status_code=404)
    _cfg.ADMIN_UIDS.remove(uid)
    _persist_admin_uids(_cfg.ADMIN_UIDS)
    resolver = get_resolver()
    name = resolver.user(uid) or uid[:12]
    logger.info("Bot 管理员已移除: %s (%s)", name, uid[:12])
    return JSONResponse({"ok": True, "message": f"已移除 {name} 的管理员权限"})


def _persist_admin_uids(uids: list) -> None:
    """将管理员列表持久化到 admin_runtime_config.json。"""
    overrides = cfg.read_admin_overrides()
    overrides["admin_uids"] = list(uids)
    cfg.write_admin_overrides(overrides)


@admin_router.post("/admin/api/members/{uid}/role")
async def admin_member_role(uid: str, request: Request):
    sender = _get_sender()
    if not sender:
        return JSONResponse({"ok": False, "error": "sender 未初始化"}, status_code=503)
    body = await request.json()
    area = _extract_area(body)
    try:
        role_id = int(body.get("role_id", 0))
    except (TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "role_id 必须为整数"}, status_code=400)
    action = str(body.get("action", "add"))
    if not role_id:
        return JSONResponse({"ok": False, "error": "role_id 不能为空"}, status_code=400)
    result = sender.edit_user_role(uid, role_id, add=(action == "add"), area=area)
    if "error" in result:
        return JSONResponse({"ok": False, "error": result["error"]})
    _invalidate_members_cache()
    return JSONResponse({"ok": True, "message": result.get("message", "角色已更新")})


# ---------------------------------------------------------------------------
# 频道列表 & 发送消息/公告 API
# ---------------------------------------------------------------------------

_channels_cache: dict = {"data": None, "ts": 0.0, "area": ""}
_CHANNELS_CACHE_TTL = 120.0


@admin_router.get("/admin/api/channels")
def admin_channels_list(area: str = Query("")):
    """返回指定域的频道列表(含分组)。"""
    resolved_area = area.strip() or _resolve_area()
    if not resolved_area:
        return JSONResponse({"ok": False, "error": "未找到可用域 ID"})

    now = time.time()
    if (_channels_cache["data"] and _channels_cache["area"] == resolved_area
            and now - _channels_cache["ts"] < _CHANNELS_CACHE_TTL):
        return JSONResponse(_channels_cache["data"])

    sender = _get_sender()
    if not sender:
        return JSONResponse({"ok": False, "error": "sender 未初始化"}, status_code=503)

    groups = sender.get_area_channels(area=resolved_area, quiet=True)
    channels = []
    for g in groups:
        group_name = g.get("name", "")
        for ch in g.get("channels") or []:
            ch_type = ch.get("type", "")
            channels.append({
                "id": ch.get("id", ""),
                "name": ch.get("name", ""),
                "group": group_name,
                "type": ch_type,
                "secret": bool(ch.get("secret")),
            })

    resp = {"ok": True, "channels": channels, "area": resolved_area}
    _channels_cache.update(data=resp, ts=now, area=resolved_area)
    return JSONResponse(resp)


@admin_router.post("/admin/api/send-message")
async def admin_send_message(request: Request):
    """发送普通消息到指定频道。"""
    sender = _get_sender()
    if not sender:
        return JSONResponse({"ok": False, "error": "sender 未初始化"}, status_code=503)

    body = await request.json()
    area = (body.get("area") or "").strip() or _resolve_area()
    channel = (body.get("channel") or "").strip()
    text = (body.get("text") or "").strip()

    if not area:
        return JSONResponse({"ok": False, "error": "未指定域"})
    if not channel:
        return JSONResponse({"ok": False, "error": "未指定频道"})
    if not text:
        return JSONResponse({"ok": False, "error": "消息内容不能为空"})

    try:
        resp = sender.send_message(text, area=area, channel=channel, auto_recall=False, styleTags=[])
        result = resp.json()
        if not result.get("status") and result.get("code") not in (0, "0", 200, "200", "success"):
            return JSONResponse({"ok": False, "error": result.get("message") or "发送失败"})
        return JSONResponse({"ok": True, "message": "消息已发送"})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@admin_router.post("/admin/api/send-announcement")
async def admin_send_announcement(request: Request):
    """发送公告样式消息到指定频道。"""
    sender = _get_sender()
    if not sender:
        return JSONResponse({"ok": False, "error": "sender 未初始化"}, status_code=503)

    body = await request.json()
    area = (body.get("area") or "").strip() or _resolve_area()
    channel = (body.get("channel") or "").strip()
    text = (body.get("text") or "").strip()

    if not area:
        return JSONResponse({"ok": False, "error": "未指定域"})
    if not channel:
        return JSONResponse({"ok": False, "error": "未指定频道"})
    if not text:
        return JSONResponse({"ok": False, "error": "公告内容不能为空"})

    try:
        resp = sender.send_message(text, area=area, channel=channel, auto_recall=False, styleTags=["IMPORTANT"])
        result = resp.json()
        if not result.get("status") and result.get("code") not in (0, "0", 200, "200", "success"):
            return JSONResponse({"ok": False, "error": result.get("message") or "发送失败"})
        return JSONResponse({"ok": True, "message": "公告已发送"})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


# ---------------------------------------------------------------------------
# 域配置管理 API (area_configs CRUD)
# ---------------------------------------------------------------------------

@admin_router.get("/admin/api/area-configs")
def admin_area_configs_list():
    """返回所有域的独立配置。"""
    from area_config import get_area_registry
    reg = get_area_registry()
    configs = reg.export_all()
    return JSONResponse({"ok": True, "configs": configs})


@admin_router.get("/admin/api/area-configs/{area_id}")
def admin_area_config_get(area_id: str):
    from area_config import get_area_registry, AreaConfigRegistry
    reg = get_area_registry()
    if not reg.is_configured(area_id):
        return JSONResponse({"ok": True, "configured": False, "config": {}})
    c = reg.get(area_id)
    return JSONResponse({"ok": True, "configured": True, "config": AreaConfigRegistry.config_to_dict(c)})


@admin_router.post("/admin/api/area-configs/{area_id}")
async def admin_area_config_save(area_id: str, request: Request):
    """创建或更新域配置并持久化。"""
    body = await request.json()
    area_id = area_id.strip()
    if not area_id:
        return JSONResponse({"ok": False, "error": "area_id 不能为空"}, status_code=400)

    from area_config import get_area_registry, AreaConfigRegistry
    reg = get_area_registry()
    reg.update_config(area_id, body)

    saved = cfg.read_area_overrides()
    saved[area_id] = body
    cfg.write_area_overrides(saved)

    return JSONResponse({"ok": True, "config": AreaConfigRegistry.config_to_dict(reg.get(area_id))})


@admin_router.delete("/admin/api/area-configs/{area_id}")
def admin_area_config_delete(area_id: str):
    """删除域的独立配置。"""
    area_id = area_id.strip()
    from area_config import get_area_registry
    reg = get_area_registry()
    removed = reg.remove_config(area_id)

    saved = cfg.read_area_overrides()
    saved.pop(area_id, None)
    cfg.write_area_overrides(saved)

    return JSONResponse({"ok": True, "removed": removed})


# ---------------------------------------------------------------------------
# 频道管理 API (创建 / 删除 / 修改)
# ---------------------------------------------------------------------------

@admin_router.post("/admin/api/channels/create")
async def admin_channel_create(request: Request):
    sender = _get_sender()
    if not sender:
        return JSONResponse({"ok": False, "error": "sender 未初始化"}, status_code=503)
    body = await request.json()
    area = (body.get("area") or "").strip() or _resolve_area()
    name = (body.get("name") or "").strip()
    ch_type = body.get("type", "text")
    group_id = (body.get("group_id") or "").strip()
    if not area or not name:
        return JSONResponse({"ok": False, "error": "area 和 name 不能为空"}, status_code=400)
    try:
        result = sender.create_channel(area=area, name=name, channel_type=ch_type, group_id=group_id)
        if isinstance(result, dict) and "error" in result:
            return JSONResponse({"ok": False, "error": result["error"]})
        _channels_cache.update(data=None, ts=0.0, area="")
        return JSONResponse({"ok": True, "message": "频道已创建", "result": result if isinstance(result, dict) else {}})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@admin_router.delete("/admin/api/channels/{channel_id}")
async def admin_channel_delete(channel_id: str, request: Request):
    sender = _get_sender()
    if not sender:
        return JSONResponse({"ok": False, "error": "sender 未初始化"}, status_code=503)
    body = await request.json()
    area = (body.get("area") or "").strip() or _resolve_area()
    if not area:
        return JSONResponse({"ok": False, "error": "area 不能为空"}, status_code=400)
    try:
        result = sender.delete_channel(channel=channel_id, area=area)
        if isinstance(result, dict) and "error" in result:
            return JSONResponse({"ok": False, "error": result["error"]})
        _channels_cache.update(data=None, ts=0.0, area="")
        return JSONResponse({"ok": True, "message": "频道已删除"})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@admin_router.put("/admin/api/channels/{channel_id}")
async def admin_channel_update(channel_id: str, request: Request):
    sender = _get_sender()
    if not sender:
        return JSONResponse({"ok": False, "error": "sender 未初始化"}, status_code=503)
    body = await request.json()
    area = (body.get("area") or "").strip() or _resolve_area()
    name = (body.get("name") or "").strip()
    if not area:
        return JSONResponse({"ok": False, "error": "area 不能为空"}, status_code=400)
    try:
        result = sender.update_channel(area=area, channel_id=channel_id, name=name)
        if isinstance(result, dict) and "error" in result:
            return JSONResponse({"ok": False, "error": result["error"]})
        _channels_cache.update(data=None, ts=0.0, area="")
        return JSONResponse({"ok": True, "message": "频道已更新"})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


# ---------------------------------------------------------------------------
# 频道设置 API (读取 / 编辑)
# ---------------------------------------------------------------------------

@admin_router.get("/admin/api/channels/{channel_id}/settings")
def admin_channel_settings(channel_id: str, area: str = Query("")):
    """获取频道的详细设置信息。"""
    sender = _get_sender()
    if not sender:
        return JSONResponse({"ok": False, "error": "sender 未初始化"}, status_code=503)
    data = sender.get_channel_setting_info(channel_id)
    if isinstance(data, dict) and "error" in data:
        return JSONResponse({"ok": False, "error": data["error"]})
    return JSONResponse({"ok": True, "settings": data})


@admin_router.post("/admin/api/channels/{channel_id}/settings")
async def admin_channel_settings_edit(channel_id: str, request: Request):
    """编辑频道设置（名称、人数上限、慢速模式等）。"""
    sender = _get_sender()
    if not sender:
        return JSONResponse({"ok": False, "error": "sender 未初始化"}, status_code=503)
    body = await request.json()
    area = (body.pop("area", "") or "").strip() or _resolve_area()
    if not area:
        return JSONResponse({"ok": False, "error": "area 不能为空"}, status_code=400)
    try:
        result = sender.update_channel(area=area, channel_id=channel_id, overrides=body)
        if isinstance(result, dict) and "error" in result:
            return JSONResponse({"ok": False, "error": result["error"]})
        _channels_cache.update(data=None, ts=0.0, area="")
        return JSONResponse({"ok": True, "message": "频道设置已保存"})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


# ---------------------------------------------------------------------------
# 频道可访问成员 API (私密频道)
# ---------------------------------------------------------------------------

@admin_router.get("/admin/api/channels/{channel_id}/accessible-members")
def admin_channel_accessible_members(channel_id: str):
    """返回频道当前的可访问成员列表（含名称）。"""
    sender = _get_sender()
    if not sender:
        return JSONResponse({"ok": False, "error": "sender 未初始化"}, status_code=503)
    setting = sender.get_channel_setting_info(channel_id)
    if isinstance(setting, dict) and "error" in setting:
        return JSONResponse({"ok": False, "error": setting["error"]})
    uids = list(setting.get("accessibleMembers") or [])
    if not uids:
        return JSONResponse({"ok": True, "members": []})
    infos = sender.get_person_infos_batch(uids)
    members = []
    for uid in uids:
        info = infos.get(uid, {})
        members.append({
            "uid": uid,
            "name": info.get("name") or info.get("nickname") or uid[:8],
            "avatar": info.get("avatar", ""),
        })
    return JSONResponse({"ok": True, "members": members})


@admin_router.get("/admin/api/online-members")
def admin_online_members(area: str = Query("")):
    """返回域内当前在线成员（用于私密频道成员选择）。"""
    sender = _get_sender()
    if not sender:
        return JSONResponse({"ok": False, "error": "sender 未初始化"}, status_code=503)
    resolved_area = area.strip() or _resolve_area()
    if not resolved_area:
        return JSONResponse({"ok": False, "error": "未找到可用域 ID"})
    all_members = []
    for page_start in range(0, 200, 50):
        result = sender.get_area_members(
            area=resolved_area, offset_start=page_start,
            offset_end=page_start + 49, quiet=True,
        )
        if "error" in result:
            break
        batch = result.get("members") or []
        all_members.extend(batch)
        if len(batch) < 50:
            break
    online_members = [m for m in all_members if m.get("online", 0) == 1]
    uids = [m.get("uid", "") for m in online_members if m.get("uid")]
    if not uids:
        return JSONResponse({"ok": True, "members": []})
    infos = sender.get_person_infos_batch(uids)
    members = []
    for m in online_members:
        uid = m.get("uid", "")
        if not uid:
            continue
        info = infos.get(uid, {})
        members.append({
            "uid": uid,
            "name": info.get("name") or info.get("nickname") or uid[:8],
        })
    return JSONResponse({"ok": True, "members": members})


# ---------------------------------------------------------------------------
# 语音频道监控 API
# ---------------------------------------------------------------------------

@admin_router.get("/admin/api/voice-channels")
def admin_voice_channels(area: str = Query("")):
    """返回域内语音频道及其在线用户。"""
    resolved_area = area.strip() or _resolve_area()
    if not resolved_area:
        return JSONResponse({"ok": False, "error": "未找到可用域 ID"})

    sender = _get_sender()
    if not sender:
        return JSONResponse({"ok": False, "error": "sender 未初始化"}, status_code=503)

    groups = sender.get_area_channels(area=resolved_area, quiet=True)
    voice_info = {}
    for g in groups:
        for ch in g.get("channels") or []:
            ch_type = str(ch.get("type", "")).upper()
            if ch_type in ("VOICE", "AUDIO"):
                voice_info[ch.get("id", "")] = {
                    "name": ch.get("name", ""),
                    "group": g.get("name", ""),
                }

    channel_members = sender.get_voice_channel_members(area=resolved_area)

    resolver = get_resolver()
    voice_channels = []
    for ch_id, info in voice_info.items():
        raw_members = channel_members.get(ch_id, [])
        users = []
        for m in raw_members:
            uid = m.get("uid", m.get("id", "")) if isinstance(m, dict) else str(m)
            if uid:
                users.append({"uid": uid, "name": resolver.user(uid) or uid[:8]})
        voice_channels.append({
            "id": ch_id,
            "name": info["name"],
            "group": info["group"],
            "users": users,
        })

    return JSONResponse({"ok": True, "voice_channels": voice_channels})


# ---------------------------------------------------------------------------
# 插件管理 API
# ---------------------------------------------------------------------------

def _get_plugin_runtime():
    from web_player import get_plugin_runtime
    return get_plugin_runtime()


def _get_plugin_host():
    from web_player import get_plugin_host
    return get_plugin_host()


def _descriptor_to_dict(d) -> dict:
    return {
        "name": d.name,
        "description": d.description,
        "version": d.version,
        "author": d.author,
        "builtin": d.builtin,
        "mention_prefixes": list(d.mention_prefixes),
        "slash_commands": list(d.slash_commands),
        "is_public_command": d.is_public_command,
    }


@admin_router.get("/admin/api/plugins")
def admin_plugins_list():
    runtime = _get_plugin_runtime()
    if not runtime:
        return JSONResponse({"ok": False, "error": "插件运行时未初始化"}, status_code=503)
    loaded = [_descriptor_to_dict(d) for d in runtime.list_descriptors()]
    loaded_names = {d["name"] for d in loaded}
    available = [n for n in runtime.discover() if n not in loaded_names]
    return JSONResponse({
        "ok": True,
        "loaded": loaded,
        "plugins": loaded,
        "available": available,
        "loaded_count": len(loaded),
        "available_count": len(available),
        "enabled_plugins": [item["name"] for item in loaded],
    })


@admin_router.post("/admin/api/plugins/{name}/load")
def admin_plugin_load(name: str):
    runtime = _get_plugin_runtime()
    host = _get_plugin_host()
    if not runtime:
        return JSONResponse({"ok": False, "error": "插件运行时未初始化"}, status_code=503)
    result = runtime.load(name, handler=host)
    if not result.ok:
        return JSONResponse({"ok": False, "error": result.message, "code": result.code.value})
    return JSONResponse({"ok": True, "message": result.message})


@admin_router.post("/admin/api/plugins/{name}/unload")
def admin_plugin_unload(name: str):
    runtime = _get_plugin_runtime()
    host = _get_plugin_host()
    if not runtime:
        return JSONResponse({"ok": False, "error": "插件运行时未初始化"}, status_code=503)
    result = runtime.unload(name, handler=host)
    if not result.ok:
        return JSONResponse({"ok": False, "error": result.message, "code": result.code.value})
    return JSONResponse({"ok": True, "message": result.message})


@admin_router.post("/admin/api/plugins/{name}/reload-config")
def admin_plugin_reload_config(name: str):
    runtime = _get_plugin_runtime()
    host = _get_plugin_host()
    if not runtime:
        return JSONResponse({"ok": False, "error": "插件运行时未初始化"}, status_code=503)
    result = runtime.reload_config(name, handler=host)
    if not result.ok:
        return JSONResponse({"ok": False, "error": result.message, "code": result.code.value})
    return JSONResponse({"ok": True, "message": result.message})


@admin_router.get("/admin/api/plugins/{name}/config")
def admin_plugin_config_get(name: str):
    from app.infrastructure.plugin_runtime.loader import DEFAULT_PLUGIN_CONFIG_DIR
    config_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        DEFAULT_PLUGIN_CONFIG_DIR,
    )
    config_path = os.path.join(config_dir, f"{name}.json")
    config_data = {}
    if os.path.isfile(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config_data = json.load(f)
        except Exception as exc:
            return JSONResponse({"ok": False, "error": f"读取配置失败: {exc}"})

    schema_path = os.path.join(config_dir, f"{name}.schema.json")
    schema_data = None
    if os.path.isfile(schema_path):
        try:
            with open(schema_path, "r", encoding="utf-8") as f:
                schema_data = json.load(f)
        except Exception:
            pass

    return JSONResponse({
        "ok": True,
        "name": name,
        "config": config_data,
        "config_exists": os.path.isfile(config_path),
        "schema": schema_data,
    })


@admin_router.post("/admin/api/plugins/{name}/config")
async def admin_plugin_config_save(name: str, request: Request):
    from app.infrastructure.plugin_runtime.loader import DEFAULT_PLUGIN_CONFIG_DIR
    config_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        DEFAULT_PLUGIN_CONFIG_DIR,
    )
    config_path = os.path.join(config_dir, f"{name}.json")

    try:
        body = await request.json()
        config_data = body.get("config", body)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"解析请求体失败: {exc}"}, status_code=400)

    os.makedirs(config_dir, exist_ok=True)
    try:
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config_data, f, ensure_ascii=False, indent=2)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"写入配置失败: {exc}"})

    runtime = _get_plugin_runtime()
    host = _get_plugin_host()
    reload_msg = ""
    if runtime and runtime.registry.get(name):
        result = runtime.reload_config(name, handler=host)
        reload_msg = result.message

    return JSONResponse({"ok": True, "message": "配置已保存", "reload": reload_msg})
