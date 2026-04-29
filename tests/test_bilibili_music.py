import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"

if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

try:
    import requests
    _REQUESTS_ERROR = None
except Exception as exc:
    _REQUESTS_ERROR = exc


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class BilibiliMusicTest(unittest.TestCase):
    def setUp(self) -> None:
        if _REQUESTS_ERROR is not None:
            self.skipTest(f"缺少 requests 依赖: {_REQUESTS_ERROR}")
        import bilibili_music

        self.module = bilibili_music
        self._old_config = bilibili_music._cached_config
        bilibili_music._cached_config = {
            "enabled": True,
            "cookie": "SESSDATA=sess; bili_jct=csrf",
        }

    def tearDown(self) -> None:
        self.module._cached_config = self._old_config

    def test_video_playurl_uses_cid_from_view(self) -> None:
        bili = self.module.BilibiliMusic()
        calls = []

        def fake_get(url, params=None, referer=None):
            calls.append((url, params or {}, referer))
            if url == self.module._API_VIDEO_VIEW:
                return {
                    "code": 0,
                    "data": {
                        "pages": [{"cid": 998877}],
                    },
                }
            if url == self.module._API_VIDEO_PLAYURL:
                self.assertEqual((params or {}).get("bvid"), "BV1test")
                self.assertEqual((params or {}).get("cid"), "998877")
                return {
                    "code": 0,
                    "data": {
                        "dash": {
                            "audio": [
                                {"baseUrl": "https://low.example/audio.m4s", "bandwidth": 64000},
                                {"baseUrl": "https://high.example/audio.m4s", "bandwidth": 128000},
                            ],
                        },
                    },
                }
            return {"code": -1}

        bili._get = fake_get

        url = bili._get_video_audio_url("BV1test")

        self.assertEqual(url, "https://high.example/audio.m4s")
        self.assertEqual(calls[0][0], self.module._API_VIDEO_VIEW)
        self.assertEqual(calls[1][0], self.module._API_VIDEO_PLAYURL)

    def test_get_retries_once_after_412(self) -> None:
        bili = self.module.BilibiliMusic()
        target_url = "https://api.bilibili.com/x/web-interface/search/type"
        calls = []

        class FakeCookies(dict):
            pass

        class FakeSession:
            def __init__(self):
                self.headers = {}
                self.cookies = FakeCookies()
                self.target_calls = 0

            def get(self, url, params=None, headers=None, timeout=10):
                calls.append(url)
                if url == self.module._BILIBILI_HOME:
                    self.cookies["buvid3"] = "seed"
                    return _FakeResponse(200, {"ok": True})
                self.target_calls += 1
                if self.target_calls == 1:
                    return _FakeResponse(412, {"code": -412})
                return _FakeResponse(200, {"code": 0, "data": {"result": []}})

        fake_session = FakeSession()
        fake_session.module = self.module
        bili._session = fake_session

        data = bili._get(target_url, params={"keyword": "测试"})

        self.assertEqual(data["code"], 0)
        self.assertEqual(calls, [target_url, self.module._BILIBILI_HOME, target_url])
        self.assertEqual(fake_session.cookies["buvid3"], "seed")


if __name__ == "__main__":
    unittest.main()
