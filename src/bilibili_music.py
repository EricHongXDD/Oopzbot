"""B 站音乐平台实现。

使用 B 站公开搜索 API + 音频流提取。
配置 BILIBILI_MUSIC_CONFIG.cookie 以获取更高音质。
"""

from __future__ import annotations

import re
import requests
from http.cookies import SimpleCookie
from typing import Optional
from urllib.parse import quote

from logger_config import get_logger

logger = get_logger("BilibiliMusic")

_API_SEARCH = "https://api.bilibili.com/x/web-interface/search/type"
_API_AUDIO_INFO = "https://www.bilibili.com/audio/music-service-c/web/song/info"
_API_AUDIO_URL = "https://www.bilibili.com/audio/music-service-c/web/url"
_API_VIDEO_VIEW = "https://api.bilibili.com/x/web-interface/view"
_API_VIDEO_PLAYURL = "https://api.bilibili.com/x/player/playurl"
_BILIBILI_HOME = "https://www.bilibili.com/"

_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
    ),
    "Referer": _BILIBILI_HOME,
}


_cached_config: dict | None = None


def _load_config() -> dict:
    global _cached_config
    if _cached_config is not None:
        return _cached_config
    try:
        from config import BILIBILI_MUSIC_CONFIG
        _cached_config = BILIBILI_MUSIC_CONFIG
        return _cached_config
    except (ImportError, AttributeError):
        _cached_config = {}
        return _cached_config


def _cookie_dict(raw: str) -> dict[str, str]:
    """把后台配置里的 Cookie 字符串解析为 Session Cookie。"""
    text = (raw or "").strip()
    if not text:
        return {}
    try:
        parsed = SimpleCookie()
        parsed.load(text)
        return {name: morsel.value for name, morsel in parsed.items() if morsel.value}
    except Exception:
        pairs: dict[str, str] = {}
        for item in text.split(";"):
            if "=" not in item:
                continue
            name, value = item.split("=", 1)
            name = name.strip()
            value = value.strip()
            if name and value:
                pairs[name] = value
        return pairs


def _cookie_debug_summary(raw: str) -> str:
    """生成不含 Cookie 值的调试摘要。"""
    names = sorted(_cookie_dict(raw).keys())
    return "configured=%s names=%s" % (bool(names), ",".join(names) or "-")


def _bilibili_referer_for_video(bvid: str) -> str:
    return f"https://www.bilibili.com/video/{bvid}"


class BilibiliMusic:
    """B 站音乐平台，实现 MusicPlatform 协议。"""

    name = "bilibili"
    display_name = "B站"

    def __init__(self):
        cfg = _load_config()
        self.enabled = cfg.get("enabled", False)
        self.cookie = cfg.get("cookie", "")
        self.last_song_url_error = ""
        self._session = requests.Session()
        self._session.headers.update(_HEADERS)
        cookie_pairs = _cookie_dict(self.cookie)
        if cookie_pairs:
            self._session.cookies.update(cookie_pairs)
        logger.debug("B 站音乐平台初始化: enabled=%s %s", self.enabled, _cookie_debug_summary(self.cookie))

    def _prime_session(self) -> None:
        """刷新 B 站首页 Cookie，降低公开接口偶发 412 的概率。"""
        try:
            resp = self._session.get(_BILIBILI_HOME, timeout=10)
            logger.debug(
                "B 站首页 Cookie 预热完成: status=%s cookies=%s",
                getattr(resp, "status_code", "-"),
                ",".join(sorted(self._session.cookies.keys())) or "-",
            )
        except Exception as e:
            logger.debug("B 站首页 Cookie 预热失败: %s", e)

    def _get(self, url: str, params: dict | None = None, referer: str | None = None) -> Optional[dict]:
        request_headers = {}
        if referer:
            request_headers["Referer"] = referer
        try:
            resp = self._session.get(url, params=params, headers=request_headers, timeout=10)
            if resp.status_code == 412:
                logger.debug("B 站 API 返回 412，刷新首页 Cookie 后重试: url=%s params=%s", url, params or {})
                self._prime_session()
                resp = self._session.get(url, params=params, headers=request_headers, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, dict):
                code = data.get("code")
                if code not in (None, 0):
                    logger.debug(
                        "B 站 API 返回业务错误: url=%s code=%s message=%s",
                        url,
                        code,
                        data.get("message") or data.get("msg"),
                    )
                return data
            logger.error("B 站 API 返回格式异常 (%s): %s", url, type(data).__name__)
            return None
        except Exception as e:
            logger.error("B 站 API 请求失败 (%s): %s", url, e)
            return None

    def _set_last_error(self, message: str) -> None:
        self.last_song_url_error = message

    def search(self, keyword: str, limit: int = 1) -> Optional[dict]:
        results = self._search_audio(keyword, limit)
        if results:
            return results[0]
        results = self._search_video(keyword, limit)
        if results:
            return results[0]
        return None

    def search_many(self, keyword: str, limit: int = 10, offset: int = 0) -> list[dict]:
        page = (offset // max(limit, 1)) + 1
        results = self._search_audio(keyword, limit, page)
        if not results:
            results = self._search_video(keyword, limit, page)
        return results or []

    def _search_audio(self, keyword: str, limit: int = 10, page: int = 1) -> list[dict]:
        data = self._get(_API_SEARCH, params={
            "search_type": "audio",
            "keyword": keyword,
            "page": page,
            "pagesize": limit,
        }, referer=f"https://search.bilibili.com/all?keyword={quote(keyword)}")
        if not data or data.get("code") != 0:
            return []
        items = (data.get("data") or {}).get("result") or []
        return [p for item in items if (p := self._parse_audio(item))]

    def _search_video(self, keyword: str, limit: int = 10, page: int = 1) -> list[dict]:
        data = self._get(_API_SEARCH, params={
            "search_type": "video",
            "keyword": keyword + " 音乐",
            "page": page,
            "pagesize": limit,
        }, referer=f"https://search.bilibili.com/all?keyword={quote(keyword)}")
        if not data or data.get("code") != 0:
            return []
        items = (data.get("data") or {}).get("result") or []
        return [p for item in items if (p := self._parse_video(item))]

    def get_song_url(self, song_id) -> Optional[str]:
        self.last_song_url_error = ""
        sid = str(song_id)
        if sid.startswith("au"):
            url = self._get_audio_url(sid[2:])
            if not url and not self.last_song_url_error:
                self._set_last_error(f"B站无法获取音频链接: {sid}")
            return url
        if sid.startswith("BV") or sid.startswith("bv"):
            url = self._get_video_audio_url(sid)
            if not url and not self.last_song_url_error:
                self._set_last_error(f"B站无法获取视频音频流: {sid}")
            return url
        return self._get_audio_url(sid) or self._get_video_audio_url(sid)

    def _get_audio_url(self, au_id: str) -> Optional[str]:
        data = self._get(
            _API_AUDIO_URL,
            params={"sid": au_id, "privilege": 2, "quality": 2},
            referer=f"https://www.bilibili.com/audio/au{au_id}",
        )
        if not data:
            self._set_last_error(f"B站音频链接请求失败: au{au_id}")
            return None
        if data.get("code") != 0:
            self._set_last_error(
                f"B站音频链接接口返回错误: code={data.get('code')} message={data.get('message') or data.get('msg') or ''}"
            )
            return None
        cdns = (data.get("data") or {}).get("cdns") or []
        if not cdns:
            self._set_last_error(f"B站音频链接为空: au{au_id}")
            return None
        return cdns[0] if cdns else None

    def _get_video_cid(self, bvid: str) -> Optional[str]:
        data = self._get(
            _API_VIDEO_VIEW,
            params={"bvid": bvid},
            referer=_bilibili_referer_for_video(bvid),
        )
        if not data or data.get("code") != 0:
            logger.debug("B 站视频 cid 获取失败: bvid=%s", bvid)
            self._set_last_error(f"B站视频 cid 获取失败: {bvid}")
            return None
        video = data.get("data") or {}
        cid = video.get("cid")
        pages = video.get("pages") or []
        if not cid and pages:
            first = pages[0] if isinstance(pages[0], dict) else {}
            cid = first.get("cid")
        if not cid:
            logger.debug("B 站视频详情缺少 cid: bvid=%s", bvid)
            self._set_last_error(f"B站视频详情缺少 cid: {bvid}")
            return None
        return str(cid)

    def _get_video_audio_url(self, bvid: str) -> Optional[str]:
        cid = self._get_video_cid(bvid)
        if not cid:
            return None
        data = self._get(_API_VIDEO_PLAYURL, params={
            "bvid": bvid,
            "cid": cid,
            "fnval": 16,
            "qn": 64,
            "fourk": 1,
        }, referer=_bilibili_referer_for_video(bvid))
        if not data:
            self._set_last_error(f"B站视频播放链接请求失败: {bvid}")
            return None
        if data.get("code") != 0:
            logger.debug("B 站视频播放链接接口失败: bvid=%s cid=%s", bvid, cid)
            self._set_last_error(
                f"B站视频播放链接接口返回错误: code={data.get('code')} message={data.get('message') or data.get('msg') or ''}"
            )
            return None
        dash = (data.get("data") or {}).get("dash") or {}
        audio_list = dash.get("audio") or []
        if audio_list:
            audio_list = sorted(audio_list, key=lambda item: int(item.get("bandwidth") or 0), reverse=True)
            logger.debug("B 站视频音频链接获取成功: bvid=%s cid=%s audio_count=%s", bvid, cid, len(audio_list))
            return audio_list[0].get("baseUrl") or audio_list[0].get("base_url")
        durl = (data.get("data") or {}).get("durl") or []
        if durl:
            logger.debug("B 站视频播放链接回退到 durl: bvid=%s cid=%s", bvid, cid)
            return durl[0].get("url")
        logger.debug("B 站视频播放链接响应中没有 dash.audio/durl: bvid=%s cid=%s", bvid, cid)
        self._set_last_error(f"B站视频播放链接没有音频流: {bvid}")
        return None

    def get_song_detail(self, song_id) -> Optional[dict]:
        sid = str(song_id)
        if sid.startswith("BV") or sid.startswith("bv"):
            return self._get_video_detail(sid)
        if sid.startswith("au"):
            sid = sid[2:]
        data = self._get(_API_AUDIO_INFO, params={"sid": sid})
        if data and data.get("code") == 0 and data.get("data"):
            return self._parse_audio_detail(data["data"])
        return None

    def _get_video_detail(self, bvid: str) -> Optional[dict]:
        data = self._get(
            _API_VIDEO_VIEW,
            params={"bvid": bvid},
            referer=_bilibili_referer_for_video(bvid),
        )
        if not data or data.get("code") != 0 or not data.get("data"):
            return None
        video = data["data"]
        owner = video.get("owner") if isinstance(video.get("owner"), dict) else {}
        duration_s = int(video.get("duration") or 0)
        cover = video.get("pic") or ""
        if cover.startswith("//"):
            cover = "https:" + cover
        return {
            "id": video.get("bvid") or bvid,
            "name": video.get("title") or "未知",
            "artists": owner.get("name") or video.get("author") or "未知",
            "album": "",
            "duration": duration_s * 1000,
            "durationText": f"{duration_s // 60}:{duration_s % 60:02d}",
            "cover": cover,
        }

    def get_lyric(self, song_id) -> Optional[str]:
        sid = str(song_id)
        if sid.startswith("au"):
            sid = sid[2:]
        data = self._get(
            "https://www.bilibili.com/audio/music-service-c/web/song/lyric",
            params={"sid": sid},
            referer=f"https://www.bilibili.com/audio/au{sid}",
        )
        if not data or data.get("code") != 0:
            return None
        lyric = (data.get("data") or {}).get("lrc") or ""
        return lyric if lyric and "[" in lyric else None

    def summarize(self, keyword: str) -> dict:
        song = self.search(keyword)
        if not song:
            return {"code": "error", "message": f"B站未找到: {keyword}", "data": None}
        url = self.get_song_url(song["id"])
        if not url:
            return {"code": "error", "message": f"B站无法获取播放链接: {song['name']}", "data": None}
        song["url"] = url
        msg = (
            f"歌曲: {song['name']}\n"
            f"作者: {song['artists']}\n"
            f"时长: {song['durationText']}"
        )
        return {"code": "success", "message": msg, "data": song}

    def summarize_by_id(self, song_id) -> dict:
        song = self.get_song_detail(song_id)
        if not song:
            return {"code": "error", "message": f"B站无法获取信息: {song_id}", "data": None}
        url = self.get_song_url(song["id"])
        if not url:
            return {"code": "error", "message": f"B站无法获取播放链接: {song['name']}", "data": None}
        song["url"] = url
        return {"code": "success", "message": "", "data": song}

    def _parse_audio(self, item: dict) -> Optional[dict]:
        au_id = item.get("id") or ""
        if not au_id:
            return None
        title = item.get("title") or "未知"
        title = re.sub(r"<[^>]+>", "", title)
        author = item.get("author") or item.get("up_name") or "未知"
        duration_s = int(item.get("duration") or 0)
        cover = item.get("cover") or ""
        return {
            "id": f"au{au_id}",
            "name": title,
            "artists": author,
            "album": "",
            "duration": duration_s * 1000,
            "durationText": f"{duration_s // 60}:{duration_s % 60:02d}",
            "cover": cover,
        }

    def _parse_video(self, item: dict) -> Optional[dict]:
        bvid = item.get("bvid") or ""
        if not bvid:
            return None
        title = item.get("title") or "未知"
        title = re.sub(r"<[^>]+>", "", title)
        author = item.get("author") or item.get("up_name") or "未知"
        duration_str = item.get("duration") or "0:00"
        duration_s = 0
        if isinstance(duration_str, str) and ":" in duration_str:
            parts = duration_str.split(":")
            try:
                duration_s = int(parts[0]) * 60 + int(parts[1])
            except ValueError:
                pass
        elif isinstance(duration_str, (int, float)):
            duration_s = int(duration_str)
        cover = item.get("pic") or ""
        if cover.startswith("//"):
            cover = "https:" + cover
        return {
            "id": bvid,
            "name": title,
            "artists": author,
            "album": "",
            "duration": duration_s * 1000,
            "durationText": f"{duration_s // 60}:{duration_s % 60:02d}",
            "cover": cover,
        }

    def _parse_audio_detail(self, data: dict) -> Optional[dict]:
        au_id = data.get("id") or data.get("sid") or ""
        if not au_id:
            return None
        duration_s = int(data.get("duration") or 0)
        return {
            "id": f"au{au_id}",
            "name": data.get("title") or "未知",
            "artists": data.get("author") or data.get("uname") or "未知",
            "album": "",
            "duration": duration_s * 1000,
            "durationText": f"{duration_s // 60}:{duration_s % 60:02d}",
            "cover": data.get("cover") or "",
        }
