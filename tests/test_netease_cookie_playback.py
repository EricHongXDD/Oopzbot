import sys
import types
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
    def __init__(self, payload: dict):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class NeteaseCookiePlaybackTest(unittest.TestCase):
    def setUp(self) -> None:
        if _REQUESTS_ERROR is not None:
            self.skipTest(f"缺少 requests 依赖: {_REQUESTS_ERROR}")
        self._old_config_module = sys.modules.get("config")
        self._old_netease_module = sys.modules.get("netease")
        fake_config = types.ModuleType("config")
        fake_config.NETEASE_CLOUD = {
            "base_url": "http://netease.example",
            "cookie": "MUSIC_U=abc",
            "audio_quality": "exhigh",
        }
        sys.modules["config"] = fake_config
        sys.modules.pop("netease", None)
        import netease

        self.module = netease

    def tearDown(self) -> None:
        sys.modules.pop("netease", None)
        if self._old_netease_module is not None:
            sys.modules["netease"] = self._old_netease_module
        if self._old_config_module is not None:
            sys.modules["config"] = self._old_config_module
        else:
            sys.modules.pop("config", None)

    def _client(self, session):
        client = self.module.NeteaseCloud.__new__(self.module.NeteaseCloud)
        client.base_url = "http://netease.example"
        client.cookie = "MUSIC_U=abc"
        client._session = session
        client._last_song_url_error = ""
        return client

    def test_get_song_url_posts_cookie_in_body_before_get(self) -> None:
        calls = []

        class FakeSession:
            def post(self, url, data=None, headers=None, timeout=10):
                calls.append(("POST", url, data or {}, headers or {}))
                return _FakeResponse({
                    "code": 200,
                    "data": [{
                        "id": 1,
                        "url": "https://music.example/full.mp3",
                        "time": 222000,
                        "size": 8_000_000,
                    }],
                })

            def get(self, url, params=None, headers=None, timeout=10):
                calls.append(("GET", url, params or {}, headers or {}))
                return _FakeResponse({"code": 500, "data": []})

        client = self._client(FakeSession())

        url = client.get_song_url(1, expected_duration_ms=222000, song_name="稻香")

        self.assertEqual(url, "https://music.example/full.mp3")
        self.assertEqual(len(calls), 1)
        method, request_url, data, headers = calls[0]
        self.assertEqual(method, "POST")
        self.assertEqual(request_url, "http://netease.example/song/url/v1")
        self.assertEqual(data["id"], 1)
        self.assertEqual(data["level"], "exhigh")
        self.assertEqual(data["cookie"], "MUSIC_U=abc")
        self.assertEqual(headers["Cookie"], "MUSIC_U=abc")

    def test_get_song_url_rejects_free_trial_audio(self) -> None:
        class FakeSession:
            def __init__(self):
                self.calls = []

            def post(self, url, data=None, headers=None, timeout=10):
                self.calls.append(("POST", url, data or {}))
                return _FakeResponse({
                    "code": 200,
                    "data": [{
                        "id": 2,
                        "url": "https://music.example/trial.mp3",
                        "time": 30040,
                        "size": 600_000,
                        "freeTrialInfo": {"start": 0, "end": 30},
                    }],
                })

            def get(self, url, params=None, headers=None, timeout=10):
                self.calls.append(("GET", url, params or {}))
                return _FakeResponse({"code": 200, "data": [{"id": 2, "url": None}]})

        session = FakeSession()
        client = self._client(session)

        url = client.get_song_url(2, expected_duration_ms=240000, song_name="会员歌")

        self.assertIsNone(url)
        self.assertIn("试听音频", client.last_song_url_error)
        self.assertGreaterEqual(len(session.calls), 2)


if __name__ == "__main__":
    unittest.main()
