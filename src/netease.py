import time
import threading
import requests
from collections import OrderedDict
from typing import Optional
from urllib.parse import urlparse

from config import NETEASE_CLOUD
from logger_config import get_logger

logger = get_logger("Netease")


def _safe_params(params: Optional[dict]) -> dict:
    safe = dict(params or {})
    for key in ("cookie", "Cookie"):
        if key in safe:
            safe[key] = "<redacted>"
    return safe


def _mask_audio_url(url: object) -> str:
    text = str(url or "").strip()
    if not text:
        return ""
    try:
        parsed = urlparse(text)
        tail = parsed.path.rsplit("/", 1)[-1] if parsed.path else ""
        suffix = "?..." if parsed.query else ""
        return f"{parsed.scheme}://{parsed.netloc}/.../{tail}{suffix}"
    except Exception:
        no_query = text.split("?", 1)[0]
        return no_query[:120] + ("..." if len(no_query) > 120 else "")


def _compact_trial_value(value):
    if not isinstance(value, dict):
        return value
    keys = (
        "start",
        "end",
        "type",
        "resConsumable",
        "userConsumable",
        "listenType",
        "cannotListenReason",
        "playReason",
        "freeLimitTagType",
    )
    return {key: value.get(key) for key in keys if key in value}


def _song_url_debug_summary(item: object) -> dict:
    if not isinstance(item, dict):
        return {"type": type(item).__name__}
    return {
        "id": item.get("id"),
        "code": item.get("code"),
        "level": item.get("level"),
        "encodeType": item.get("encodeType"),
        "type": item.get("type"),
        "br": item.get("br"),
        "size": item.get("size"),
        "time": item.get("time"),
        "fee": item.get("fee"),
        "payed": item.get("payed"),
        "flag": item.get("flag"),
        "urlSource": item.get("urlSource"),
        "rightSource": item.get("rightSource"),
        "freeTrialInfo": _compact_trial_value(item.get("freeTrialInfo")),
        "freeTrialPrivilege": _compact_trial_value(item.get("freeTrialPrivilege")),
        "freeTimeTrialPrivilege": _compact_trial_value(item.get("freeTimeTrialPrivilege")),
        "url": _mask_audio_url(item.get("url")),
    }


def _looks_like_trial_audio(item: object, expected_duration_ms: int = 0) -> bool:
    if not isinstance(item, dict):
        return False
    if item.get("freeTrialInfo"):
        return True
    try:
        duration_ms = int(item.get("time") or 0)
    except (TypeError, ValueError):
        duration_ms = 0
    try:
        size = int(item.get("size") or 0)
    except (TypeError, ValueError):
        size = 0
    try:
        expected_ms = int(expected_duration_ms or 0)
    except (TypeError, ValueError):
        expected_ms = 0
    return expected_ms > 90_000 and 0 < duration_ms <= 65_000 and 0 < size < 2_000_000


def _trial_audio_message(song_name: str = "") -> str:
    label = f"《{song_name}》" if song_name else "该歌曲"
    return f"{label}只返回了 30 秒左右的试听音频，可能需要会员、单曲购买或受版权限制"


class _SearchCache:
    """线程安全的 LRU + TTL 搜索缓存，基于 OrderedDict 实现 O(1) 淘汰。"""

    def __init__(self, max_size: int = 128, ttl: int = 300):
        self._data: OrderedDict[str, tuple[float, object]] = OrderedDict()
        self._max_size = max_size
        self._ttl = ttl
        self._lock = threading.Lock()

    def get(self, key: str):
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            ts, val = entry
            if time.time() - ts > self._ttl:
                self._data.pop(key, None)
                return None
            self._data.move_to_end(key)
            return val

    def put(self, key: str, val):
        with self._lock:
            if key in self._data:
                self._data.move_to_end(key)
            elif len(self._data) >= self._max_size:
                self._data.popitem(last=False)
            self._data[key] = (time.time(), val)


class NeteaseCloud:
    """网易云音乐搜索与获取"""

    name = "netease"
    display_name = "网易云"

    def __init__(self):
        self.base_url = NETEASE_CLOUD.get("base_url", "").rstrip("/")
        self.cookie = NETEASE_CLOUD.get("cookie", "")
        self._search_cache = _SearchCache()
        self._session = requests.Session()
        self._last_song_url_error = ""
        if not self.base_url:
            logger.warning("网易云 API 地址未配置 (NETEASE_CLOUD.base_url)")

    @property
    def last_song_url_error(self) -> str:
        return self._last_song_url_error

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[dict] = None,
        include_cookie_param: bool = False,
    ) -> Optional[dict]:
        """发起网易云 API 请求；需要登录态的接口用 POST body 携带 cookie。"""
        if not self.base_url:
            return None
        try:
            request_params = dict(params or {})
            headers = {}
            if self.cookie:
                headers["Cookie"] = self.cookie
            if include_cookie_param and self.cookie:
                request_params["cookie"] = self.cookie
            logger.debug(
                "网易云 API 请求: method=%s path=%s params=%s cookie_configured=%s cookie_in_body=%s",
                method.upper(),
                path,
                _safe_params(request_params),
                bool(str(self.cookie or "").strip()),
                include_cookie_param and bool(str(self.cookie or "").strip()),
            )
            if method.upper() == "POST":
                resp = self._session.post(
                    f"{self.base_url}{path}",
                    data=request_params,
                    headers=headers,
                    timeout=10,
                )
            else:
                resp = self._session.get(
                    f"{self.base_url}{path}",
                    params=request_params,
                    headers=headers,
                    timeout=10,
                )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"网易云 API 请求失败: {e}")
            return None

    def _get(self, path: str, params: Optional[dict] = None) -> Optional[dict]:
        """发起 GET 请求（复用连接池）"""
        return self._request("GET", path, params=params)

    def _post_with_cookie(self, path: str, params: Optional[dict] = None) -> Optional[dict]:
        """发起带 cookie body 的 POST 请求，供需要会员/登录态的接口使用。"""
        return self._request("POST", path, params=params, include_cookie_param=True)

    def search(self, keyword: str, limit: int = 1) -> Optional[dict]:
        """
        搜索歌曲

        返回格式::
            {
                "id": 歌曲ID,
                "name": "歌名",
                "artists": "歌手",
                "album": "专辑",
                "duration": 毫秒,
                "cover": "封面URL"
            }
        """
        cache_key = f"s:{keyword}:{limit}"
        cached = self._search_cache.get(cache_key)
        if cached is not None:
            return cached

        data = self._get("/cloudsearch", params={"keywords": keyword, "limit": limit, "type": 1})
        if not data or data.get("code") != 200:
            return None

        songs = data.get("result", {}).get("songs", [])
        if not songs:
            return None

        result = self._parse_song(songs[0])
        if result:
            self._search_cache.put(cache_key, result)
        return result

    def search_many(self, keyword: str, limit: int = 10, offset: int = 0) -> list[dict]:
        """搜索歌曲，返回多条结果列表"""
        data = self._get("/cloudsearch", params={
            "keywords": keyword,
            "limit": limit,
            "offset": max(0, int(offset or 0)),
            "type": 1,
        })
        if not data or data.get("code") != 200:
            return []

        songs = data.get("result", {}).get("songs", [])
        results = []
        for song in songs:
            parsed = self._parse_song(song)
            if parsed:
                results.append(parsed)
        return results

    def get_song_url(self, song_id: int, expected_duration_ms: int = 0, song_name: str = "") -> Optional[str]:
        """获取歌曲播放 URL。level 可选 standard(体积小/弱网友好) 或 exhigh(音质更好)。"""
        level = NETEASE_CLOUD.get("audio_quality", "standard")
        self._last_song_url_error = ""
        logger.debug(
            "网易云获取播放链接开始: song_id=%s level=%s expected_duration_ms=%s cookie_configured=%s",
            song_id,
            level,
            expected_duration_ms or 0,
            bool(str(self.cookie or "").strip()),
        )
        requests_to_try = (
            ("/song/url/v1", {"id": song_id, "level": level}),
            ("/song/url", {"id": song_id}),
        )
        for path, params in requests_to_try:
            attempts = [("POST", True), ("GET", False)] if self.cookie else [("GET", False)]
            for method, include_cookie_param in attempts:
                data = (
                    self._post_with_cookie(path, params=params)
                    if method == "POST"
                    else self._get(path, params=params)
                )
                if not data or data.get("code") != 200:
                    logger.debug(
                        "网易云播放链接接口未成功: song_id=%s method=%s path=%s code=%s message=%s",
                        song_id,
                        method,
                        path,
                        data.get("code") if isinstance(data, dict) else None,
                        data.get("message") if isinstance(data, dict) else None,
                    )
                    continue
                urls = data.get("data", [])
                if not urls:
                    logger.debug(
                        "网易云播放链接接口 data 为空: song_id=%s method=%s path=%s",
                        song_id,
                        method,
                        path,
                    )
                    continue
                first = urls[0]
                summary = _song_url_debug_summary(first)
                logger.debug(
                    "网易云播放链接响应: song_id=%s method=%s path=%s summary=%s",
                    song_id,
                    method,
                    path,
                    summary,
                )
                if isinstance(first, dict) and first.get("url"):
                    if _looks_like_trial_audio(first, expected_duration_ms=expected_duration_ms):
                        self._last_song_url_error = _trial_audio_message(song_name)
                        logger.warning(
                            "网易云返回疑似试听音频: song_id=%s method=%s path=%s summary=%s",
                            song_id,
                            method,
                            path,
                            summary,
                        )
                        continue
                    return first["url"]
                logger.debug(
                    "网易云播放链接为空: song_id=%s method=%s path=%s summary=%s",
                    song_id,
                    method,
                    path,
                    summary,
                )
                if include_cookie_param:
                    logger.debug("网易云 POST cookie body 未拿到可用播放链接，继续尝试 GET 兼容路径")
        if self._last_song_url_error:
            logger.warning(
                "网易云播放链接被拒绝: song_id=%s level=%s reason=%s",
                song_id,
                level,
                self._last_song_url_error,
            )
        else:
            self._last_song_url_error = "无法获取播放链接"
            logger.warning("网易云未获取到播放链接: song_id=%s level=%s", song_id, level)
        return None

    def get_user_id(self) -> Optional[int]:
        """获取当前登录用户的 ID"""
        data = self._get("/user/account")
        if not data or data.get("code") != 200:
            return None
        profile = data.get("profile")
        return profile.get("userId") if profile else None

    def get_liked_ids(self, uid: int) -> list:
        """获取用户喜欢的歌曲 ID 列表"""
        data = self._get("/likelist", params={"uid": uid})
        if not data or data.get("code") != 200:
            return []
        return data.get("ids", [])

    def get_song_detail(self, song_id: int) -> Optional[dict]:
        """通过歌曲 ID 获取歌曲详细信息"""
        data = self._get("/song/detail", params={"ids": str(song_id)})
        if not data or data.get("code") != 200:
            return None

        songs = data.get("songs", [])
        if not songs:
            return None

        return self._parse_song(songs[0])

    def get_song_details_batch(self, song_ids: list) -> list:
        """批量获取歌曲详细信息（一次最多传 50 个 ID）"""
        if not song_ids:
            return []
        ids_str = ",".join(str(sid) for sid in song_ids)
        data = self._get("/song/detail", params={"ids": ids_str})
        if not data or data.get("code") != 200:
            return []

        results = []
        for song in data.get("songs", []):
            try:
                parsed = self._parse_song(song)
                if parsed:
                    results.append(parsed)
            except Exception as e:
                logger.warning(f"解析歌曲失败 (id={song.get('id')}): {e}")
        return results

    def summarize_by_id(self, song_id: int) -> dict:
        """通过歌曲 ID 获取完整信息（详情 + URL）"""
        song_info = self.get_song_detail(song_id)
        if not song_info:
            return {"code": "error", "message": f"无法获取歌曲信息: {song_id}", "data": None}

        url = self.get_song_url(
            song_id,
            expected_duration_ms=song_info.get("duration", 0) or 0,
            song_name=song_info.get("name", ""),
        )
        if not url:
            detail = self.last_song_url_error or f"无法获取播放链接: {song_info['name']}"
            return {"code": "error", "message": detail, "data": None}

        song_info["url"] = url
        return {"code": "success", "message": "", "data": song_info}

    def summarize(self, keyword: str) -> dict:
        """
        搜索并汇总歌曲信息（搜索 + 获取 URL），
        返回统一格式供 music.py 调用。
        """
        song_info = self.search(keyword)
        if not song_info:
            return {"code": "error", "message": f"未找到: {keyword}", "data": None}

        url = self.get_song_url(
            song_info["id"],
            expected_duration_ms=song_info.get("duration", 0) or 0,
            song_name=song_info.get("name", ""),
        )
        if not url:
            detail = self.last_song_url_error or f"无法获取播放链接: {song_info['name']}"
            return {"code": "error", "message": detail, "data": None}

        song_info["url"] = url

        msg = (
            f"歌曲: {song_info['name']}\n"
            f"歌手: {song_info['artists']}\n"
            f"专辑: {song_info['album']}\n"
            f"时长: {song_info['durationText']}"
        )
        return {"code": "success", "message": msg, "data": song_info}

    def get_lyrics(self, song_id: int) -> tuple[Optional[str], Optional[str]]:
        """获取歌曲 LRC 歌词和翻译歌词，一次请求同时返回 (lyric, tlyric)。"""
        data = self._get("/lyric/new", params={"id": song_id})
        if not data or data.get("code") != 200:
            return None, None
        lrc_text = (data.get("lrc") or {}).get("lyric", "")
        tlrc_text = (data.get("tlyric") or {}).get("lyric", "")
        lyric = lrc_text if lrc_text and "[" in lrc_text else None
        tlyric = tlrc_text if tlrc_text and "[" in tlrc_text else None
        return lyric, tlyric

    def get_lyric(self, song_id: int) -> Optional[str]:
        """获取歌曲 LRC 歌词文本，无歌词返回 None。"""
        lyric, _ = self.get_lyrics(song_id)
        return lyric

    def get_tlyric(self, song_id: int) -> Optional[str]:
        """获取歌曲翻译歌词，无翻译返回 None。"""
        _, tlyric = self.get_lyrics(song_id)
        return tlyric

    def _parse_song(self, song: dict) -> Optional[dict]:
        """从 API 返回的原始歌曲数据中提取标准化字段，防御所有 None 值"""
        if not song or not song.get("id"):
            return None
        ar = song.get("ar") or []
        artists = " / ".join(a.get("name") or "未知" for a in ar) or "未知"
        album = song.get("al") or {}
        duration_ms = song.get("dt") or 0
        return {
            "id": song["id"],
            "name": song.get("name") or "未知歌曲",
            "artists": artists,
            "album": album.get("name") or "",
            "duration": duration_ms,
            "durationText": self._format_duration(duration_ms),
            "cover": album.get("picUrl") or "",
        }

    @staticmethod
    def _format_duration(ms: int) -> str:
        s = (ms or 0) // 1000
        return f"{s // 60}:{s % 60:02d}"
