import base64
import json
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"

if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))


import oopz_password_login as password_login


def _jwt_with_payload(payload: dict) -> str:
    header = {"alg": "RS256", "typ": "JWT"}

    def _part(data: dict) -> str:
        raw = json.dumps(data, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    return f"{_part(header)}.{_part(payload)}.signature"


class OopzPasswordLoginHelpersTest(unittest.TestCase):
    def test_replace_config_value_updates_only_requested_field(self) -> None:
        content = 'OOPZ_CONFIG = {"device_id": "old-device", "jwt_token": "old-token"}'

        updated, replaced = password_login._replace_config_value(content, "jwt_token", "new-token")

        self.assertTrue(replaced)
        self.assertIn('"device_id": "old-device"', updated)
        self.assertIn('"jwt_token": "new-token"', updated)

    def test_sanitize_credentials_masks_secrets_and_reports_jwt_expiry(self) -> None:
        token = _jwt_with_payload({"exp": int(time.time()) + 3600})
        credentials = {
            "person_uid": "1234567890",
            "device_id": "device-abcdef",
            "jwt_token": token,
            "private_key_pem": "-----BEGIN PRIVATE KEY-----\nxxx\n-----END PRIVATE KEY-----",
            "app_version": "69514",
        }

        sanitized = password_login._sanitize_credentials(credentials)

        self.assertEqual(sanitized["person_uid"], "1234***7890")
        self.assertEqual(sanitized["device_id"], "devi***cdef")
        self.assertNotEqual(sanitized["jwt_token"], token)
        self.assertTrue(sanitized["private_key"])
        self.assertFalse(sanitized["expired"])
        self.assertGreater(sanitized["expires_in_seconds"], 0)

    def test_save_credentials_writes_config_and_private_key_files(self) -> None:
        credentials = {
            "app_version": "70000",
            "device_id": "device-new",
            "person_uid": "person-new",
            "jwt_token": "jwt-new",
            "private_key_pem": "-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----",
        }

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.py"
            private_key_path = root / "private_key.py"
            config_path.write_text(
                (
                    'OOPZ_CONFIG = {\n'
                    '    "app_version": "old",\n'
                    '    "device_id": "old",\n'
                    '    "person_uid": "old",\n'
                    '    "jwt_token": "old",\n'
                    '}\n'
                ),
                encoding="utf-8",
            )

            with (
                patch.object(password_login, "CONFIG_PATH", str(config_path)),
                patch.object(password_login, "PRIVATE_KEY_PATH", str(private_key_path)),
                patch.object(password_login, "_apply_config_to_runtime"),
            ):
                saved = password_login.save_credentials(credentials)

            self.assertEqual(saved, ["config.py", "private_key.py"])
            config_text = config_path.read_text(encoding="utf-8")
            self.assertIn('"app_version": "70000"', config_text)
            self.assertIn('"device_id": "device-new"', config_text)
            self.assertIn('"person_uid": "person-new"', config_text)
            self.assertIn('"jwt_token": "jwt-new"', config_text)
            self.assertIn("PRIVATE_KEY_PEM", private_key_path.read_text(encoding="utf-8"))


class OopzClientCredentialsTest(unittest.TestCase):
    def test_update_credentials_refreshes_identity_and_closes_socket(self) -> None:
        config = types.ModuleType("config")
        config.OOPZ_CONFIG = {
            "person_uid": "old-person",
            "device_id": "old-device",
            "jwt_token": "old-token",
        }
        config.DEFAULT_HEADERS = {
            "User-Agent": "ua",
            "Origin": "https://web.oopz.cn",
            "Cache-Control": "no-cache",
            "Accept-Language": "zh-CN",
            "Accept-Encoding": "gzip",
        }
        name_resolver = types.ModuleType("name_resolver")
        name_resolver.get_resolver = lambda: None
        proxy_utils = types.ModuleType("proxy_utils")
        proxy_utils.get_websocket_proxy_kwargs = lambda proxy: {}
        websocket = types.ModuleType("websocket")

        sys.modules.pop("oopz_client", None)
        fake_modules = {
            "config": config,
            "name_resolver": name_resolver,
            "proxy_utils": proxy_utils,
            "websocket": websocket,
        }

        with patch.dict(sys.modules, fake_modules):
            import oopz_client

            class _Socket:
                closed = False

                def close(self):
                    self.closed = True

            client = oopz_client.OopzClient.__new__(oopz_client.OopzClient)
            client._person_id = "old-person"
            client._device_id = "old-device"
            client._jwt_token = "old-token"
            client._hb_body = json.dumps({"person": "old-person"})
            client._ws = _Socket()

            client.update_credentials("new-person", "new-device", "new-token")

            self.assertEqual(client._person_id, "new-person")
            self.assertEqual(client._device_id, "new-device")
            self.assertEqual(client._jwt_token, "new-token")
            self.assertEqual(json.loads(client._hb_body), {"person": "new-person"})
            self.assertTrue(client._ws.closed)

        sys.modules.pop("oopz_client", None)


if __name__ == "__main__":
    unittest.main()
