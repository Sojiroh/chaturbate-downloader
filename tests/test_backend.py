import asyncio
import sys
from pathlib import Path

from fastapi import HTTPException
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app as webapp
from downloader.manager import DownloadManager


def test_validate_username_rejects_traversalish_names():
    bad_names = ["../alice", "alice/../bob", "..", "alice.bob", "alice-bob", "alice%2fbob"]

    for name in bad_names:
        try:
            webapp._validate_username(name)
        except HTTPException as exc:
            assert exc.status_code == 400
        else:
            raise AssertionError(f"{name!r} should have been rejected")


def test_downloads_list_excludes_split_temp_mp4_files(tmp_path, monkeypatch):
    (tmp_path / "alice_2026-04-27_10-00-00.mp4").write_bytes(b"done")
    (tmp_path / "alice_2026-04-27_10-00-00_video.mp4").write_bytes(b"video")
    (tmp_path / "alice_2026-04-27_10-00-00_audio.mp4").write_bytes(b"audio")
    monkeypatch.setattr(webapp, "DOWNLOADS_DIR", tmp_path)

    client = TestClient(webapp.app)
    response = client.get("/api/downloads/list")

    assert response.status_code == 200
    assert [item["filename"] for item in response.json()] == [
        "alice_2026-04-27_10-00-00.mp4"
    ]


def test_exact_filename_download_returns_exact_file(tmp_path, monkeypatch):
    requested = tmp_path / "alice_2026-04-27_10-00-00.mp4"
    other = tmp_path / "alice_2026-04-27_11-00-00.mp4"
    requested.write_bytes(b"exact file")
    other.write_bytes(b"other file")
    monkeypatch.setattr(webapp, "DOWNLOADS_DIR", tmp_path)

    client = TestClient(webapp.app)
    response = client.get(f"/api/downloads/file/{requested.name}")

    assert response.status_code == 200
    assert response.content == b"exact file"
    assert f'filename="{requested.name}"' in response.headers["content-disposition"]


def test_exact_filename_download_rejects_temp_track_file(tmp_path, monkeypatch):
    temp_track = tmp_path / "alice_2026-04-27_10-00-00_video.mp4"
    temp_track.write_bytes(b"temp")
    monkeypatch.setattr(webapp, "DOWNLOADS_DIR", tmp_path)

    client = TestClient(webapp.app)
    response = client.get(f"/api/downloads/file/{temp_track.name}")

    assert response.status_code == 404


def test_start_endpoint_rejects_ts_output_format():
    client = TestClient(webapp.app)

    response = client.post(
        "/api/download/start",
        params={"username": "alice", "output_format": "ts"},
        headers={"origin": "http://localhost:8000"},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Format must be 'mp4'"


def test_start_endpoint_rejects_cross_site_requests():
    client = TestClient(webapp.app)

    response = client.post(
        "/api/download/start",
        params={"username": "alice"},
        headers={"sec-fetch-site": "cross-site"},
    )

    assert response.status_code == 403


def test_start_endpoint_rejects_unconfigured_origin_even_with_matching_host():
    client = TestClient(webapp.app)

    response = client.post(
        "/api/download/start",
        params={"username": "alice"},
        headers={"origin": "http://evil.example:8000", "host": "evil.example:8000"},
    )

    assert response.status_code == 403


def test_redact_text_urls_handles_absolute_and_relative_query_tokens():
    text = "https://cdn.example/playlist.m3u8?token=secret\nsegment.m4s?verify=secret"

    redacted = webapp._redact_text_urls(text)

    assert "secret" not in redacted
    assert "https://cdn.example/playlist.m3u8?…" in redacted
    assert "segment.m4s?…" in redacted


def test_stop_during_start_reservation_prevents_untracked_task(monkeypatch, tmp_path):
    async def scenario():
        import downloader.manager as manager_module

        extract_started = asyncio.Event()
        extract_can_finish = asyncio.Event()
        download_called = False

        async def fake_extract_hls_url(username):
            extract_started.set()
            await extract_can_finish.wait()
            return "https://example.invalid/stream.m3u8?token=secret"

        class FakeDownloader:
            async def download_stream(self, *args, **kwargs):
                nonlocal download_called
                download_called = True
                raise AssertionError("download_stream should not be called after stop")

            def stop(self, username):
                pass

        monkeypatch.setattr(manager_module, "extract_hls_url", fake_extract_hls_url)
        manager = DownloadManager(output_dir=tmp_path)
        monkeypatch.setattr(manager, "_get_downloader", lambda: FakeDownloader())

        start_task = asyncio.create_task(manager.start_download("alice"))
        await asyncio.wait_for(extract_started.wait(), timeout=1)

        stop_result = await manager.stop_download("alice")
        assert stop_result == {"status": "stopped", "username": "alice"}

        extract_can_finish.set()
        start_result = await asyncio.wait_for(start_task, timeout=1)

        assert start_result == {"status": "stopped", "username": "alice"}
        assert not download_called
        assert "alice" not in manager._tasks

    asyncio.run(scenario())


def test_stop_all_during_start_clears_pending_stop_event(monkeypatch, tmp_path):
    async def scenario():
        import downloader.manager as manager_module

        extract_started = asyncio.Event()
        extract_can_finish = asyncio.Event()

        async def fake_extract_hls_url(username):
            extract_started.set()
            await extract_can_finish.wait()
            return "https://example.invalid/stream.m3u8?token=secret"

        monkeypatch.setattr(manager_module, "extract_hls_url", fake_extract_hls_url)
        manager = DownloadManager(output_dir=tmp_path)

        start_task = asyncio.create_task(manager.start_download("alice"))
        await asyncio.wait_for(extract_started.wait(), timeout=1)

        stop_result = await manager.stop_all()
        assert stop_result == {"status": "all_stopped"}

        extract_can_finish.set()
        start_result = await asyncio.wait_for(start_task, timeout=1)

        assert start_result == {"status": "stopped", "username": "alice"}
        assert "alice" not in manager._tasks
        assert manager._downloader is not None
        assert "alice" not in manager._downloader._stop_events

    asyncio.run(scenario())


def test_stopped_start_cannot_claim_newer_start_reservation(monkeypatch, tmp_path):
    async def scenario():
        import downloader.manager as manager_module

        first_started = asyncio.Event()
        first_can_finish = asyncio.Event()
        second_can_finish = asyncio.Event()
        calls = 0
        download_usernames = []

        async def fake_extract_hls_url(username):
            nonlocal calls
            calls += 1
            if calls == 1:
                first_started.set()
                await first_can_finish.wait()
                return "https://example.invalid/first.m3u8?token=old"
            await second_can_finish.wait()
            return "https://example.invalid/second.m3u8?token=new"

        class FakeDownloader:
            async def download_stream(self, username, *args, **kwargs):
                download_usernames.append(username)
                return manager_module.DownloadProgress(username=username, status="done")

            def stop(self, username):
                pass

            def stop_all(self):
                pass

        monkeypatch.setattr(manager_module, "extract_hls_url", fake_extract_hls_url)
        manager = DownloadManager(output_dir=tmp_path)
        monkeypatch.setattr(manager, "_get_downloader", lambda: FakeDownloader())

        first_task = asyncio.create_task(manager.start_download("alice"))
        await asyncio.wait_for(first_started.wait(), timeout=1)
        assert await manager.stop_download("alice") == {"status": "stopped", "username": "alice"}

        second_task = asyncio.create_task(manager.start_download("alice"))
        await asyncio.sleep(0)

        first_can_finish.set()
        assert await asyncio.wait_for(first_task, timeout=1) == {
            "status": "stopped",
            "username": "alice",
        }

        second_can_finish.set()
        assert await asyncio.wait_for(second_task, timeout=1) == {
            "status": "started",
            "username": "alice",
        }
        await asyncio.sleep(0)
        assert download_usernames == ["alice"]

    asyncio.run(scenario())


def test_status_handles_existing_download_while_new_start_is_reserved(monkeypatch, tmp_path):
    async def scenario():
        import downloader.manager as manager_module

        extract_started = asyncio.Event()
        extract_can_finish = asyncio.Event()

        async def fake_extract_hls_url(username):
            extract_started.set()
            await extract_can_finish.wait()
            return None

        monkeypatch.setattr(manager_module, "extract_hls_url", fake_extract_hls_url)
        manager = DownloadManager(output_dir=tmp_path)
        manager._downloads["alice"] = manager_module.DownloadProgress(username="alice", status="done")

        start_task = asyncio.create_task(manager.start_download("alice"))
        await asyncio.wait_for(extract_started.wait(), timeout=1)

        assert manager.get_download("alice")["active"] is False
        assert manager.get_status()[0]["active"] is False

        extract_can_finish.set()
        await asyncio.wait_for(start_task, timeout=1)

    asyncio.run(scenario())
