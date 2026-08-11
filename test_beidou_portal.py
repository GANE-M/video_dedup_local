from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from web_gateway.app import create_app
from web_gateway.database import GatewayDatabase
from web_gateway.settings import GatewaySettings


class BeidouPortalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.settings = GatewaySettings(
            project_root=Path(__file__).resolve().parent,
            storage_root=root / "videos",
            service_root=root / "service",
            database_path=root / "service" / "gateway.sqlite3",
            beidou_data_root=root / "service" / "beidou",
            beidou_database_path=root / "service" / "beidou" / "downloads.db",
            beidou_library_root=root / "downloads",
            public_base_url="https://video.example.test",
        )
        self.settings.ensure_directories()
        database = GatewayDatabase(self.settings.database_path)
        self.secret, _ = database.create_access_key("portal-test")
        self.client = TestClient(create_app(self.settings, start_worker=False))

    def tearDown(self) -> None:
        self.client.close()
        self.temporary.cleanup()

    def authenticate(self) -> None:
        response = self.client.post(
            "/beidou/api/session", headers={"X-API-Key": self.secret}
        )
        self.assertEqual(response.status_code, 200, response.text)

    def test_pages_are_mounted_and_api_requires_main_key(self) -> None:
        home = self.client.get("/")
        self.assertIn('href="/beidou/"', home.text)
        self.assertIn('href="/beidou/library"', home.text)
        self.assertEqual(self.client.get("/beidou/").status_code, 200)
        self.assertEqual(self.client.get("/beidou/library").status_code, 200)
        self.assertEqual(self.client.get("/beidou/api/status").status_code, 401)
        self.authenticate()
        self.assertEqual(self.client.get("/beidou/api/status").status_code, 200)

    def test_processed_metadata_is_persisted_for_platform_database(self) -> None:
        project = self.settings.storage_root / "owner" / "sample-drama"
        processed = project / "processed"
        processed.mkdir(parents=True)
        (processed / "episode-01.mp4").write_bytes(b"video")
        metadata = {
            "language": "Arabic",
            "platform": "reelshort",
            "is_ai_generated": "no",
            "title": "Arabic release title",
            "bio": "Arabic release bio",
            "hashtags": ["#reelshort", "#fyp", "#drama", "#love", "#shorts"],
            "title_zh": "中文标题",
            "bio_zh": "中文简介",
            "classification": {
                "audience": "女频",
                "setting": "现代",
                "confidence": 0.93,
                "rationale": "爱情主线",
            },
        }
        (processed / "publishing_metadata.json").write_text(
            json.dumps(metadata, ensure_ascii=False), encoding="utf-8"
        )
        self.authenticate()
        response = self.client.get(
            "/beidou/api/library", params={"root": str(self.settings.storage_root)}
        )
        self.assertEqual(response.status_code, 200, response.text)
        item = response.json()["items"][0]
        self.assertEqual(item["title_zh"], "中文标题")
        self.assertEqual(item["bio_zh"], "中文简介")
        self.assertEqual(item["platform"], "reelshort")
        self.assertEqual(item["audience_category"], "女频")
        self.assertEqual(item["setting_category"], "现代")
        self.assertEqual(item["hashtags"][1], "#fyp")

    def test_library_scan_cannot_escape_configured_roots(self) -> None:
        self.authenticate()
        response = self.client.get(
            "/beidou/api/library", params={"root": str(Path.home())}
        )
        self.assertEqual(response.status_code, 403)
        response = self.client.post(
            "/beidou/api/jobs",
            json={
                "mode": "selected",
                "task_ids": [1],
                "output_dir": str(Path.home()),
            },
        )
        self.assertEqual(response.status_code, 403)


if __name__ == "__main__":
    unittest.main()
