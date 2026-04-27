"""
Manages multiple concurrent downloads.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Dict, Optional
from urllib.parse import urlsplit, urlunsplit

from .extractor import extract_hls_url
from .hls import HLSDownloader, DownloadProgress

logger = logging.getLogger(__name__)


class DownloadManager:
    """Central manager for all active downloads."""

    def __init__(self, output_dir: str | Path = "downloads"):
        self.output_dir = Path(output_dir)
        self._downloads: Dict[str, DownloadProgress] = {}
        self._tasks: Dict[str, asyncio.Task | object] = {}
        self._downloader: Optional[HLSDownloader] = None
        self._lock = asyncio.Lock()
        self._stopping_all = False

    @staticmethod
    def _redact_url(url: str) -> str:
        """Remove query/fragment token material before logging URLs."""
        parts = urlsplit(url)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "…", ""))

    def _get_downloader(self) -> HLSDownloader:
        if self._downloader is None:
            self._downloader = HLSDownloader(
                output_dir=self.output_dir,
                on_progress=self._on_progress,
            )
            self._downloader.refresh_url_callback = self._refresh_url
        return self._downloader

    @staticmethod
    async def _refresh_url(username: str) -> Optional[str]:
        """Refresh the HLS URL by re-extracting from Chaturbate."""
        logger.info("Refreshing HLS URL for %s...", username)
        return await extract_hls_url(username)

    def _on_progress(self, progress: DownloadProgress):
        self._downloads[progress.username] = progress

    async def start_download(
        self,
        username: str,
        output_format: str = "mp4",
        max_duration: Optional[int] = None,
    ) -> dict:
        """Start downloading a stream."""
        username = username.strip().lower()
        reservation = object()

        async with self._lock:
            if self._stopping_all:
                return {"error": "Stopping all downloads"}
            existing = self._tasks.get(username)
            if existing is not None and not isinstance(existing, asyncio.Task):
                return {"error": f"Already starting {username}"}
            if isinstance(existing, asyncio.Task) and not existing.done():
                return {"error": f"Already downloading {username}"}
            # Reserve the slot immediately to prevent race conditions
            self._tasks[username] = reservation

        try:
            logger.info("Extracting HLS URL for '%s'...", username)
            hls_url = await extract_hls_url(username)
            if not hls_url:
                async with self._lock:
                    if self._tasks.get(username) is reservation:
                        self._tasks.pop(username, None)
                return {
                    "error": f"Could not get stream URL. Is the room '{username}' online?"
                }

            async with self._lock:
                # A stop request may have removed the reservation while URL
                # extraction was in progress. Do not create an untracked task.
                if self._tasks.get(username) is not reservation:
                    return {"status": "stopped", "username": username}

            downloader = self._get_downloader()
            progress = DownloadProgress(username=username)

            async def _run():
                try:
                    result = await downloader.download_stream(
                        username, hls_url, output_format, max_duration
                    )
                    self._downloads[username] = result
                    logger.info(
                        "Download task completed for %s: %s",
                        username,
                        result.output_path or result.error_message,
                    )
                except asyncio.CancelledError:
                    logger.info("Download cancelled for %s", username)
                    raise
                except Exception as exc:
                    logger.exception("Download failed for %s: %s", username, exc)
                    prog = self._downloads.get(username)
                    if prog:
                        prog.is_live = False
                        prog.error_message = str(exc)
                        prog.status = "error"
                finally:
                    async with self._lock:
                        if self._tasks.get(username) is task:
                            self._tasks.pop(username, None)

            async with self._lock:
                # Keep check + task creation atomic relative to stop requests.
                if self._tasks.get(username) is not reservation:
                    return {"status": "stopped", "username": username}

                logger.info(
                    "Got HLS URL for '%s': %s",
                    username,
                    self._redact_url(hls_url),
                )
                self._downloads[username] = progress
                task = asyncio.create_task(_run())
                self._tasks[username] = task

            return {"status": "started", "username": username}
        except Exception:
            async with self._lock:
                if self._tasks.get(username) is reservation:
                    self._tasks.pop(username, None)
            raise

    async def stop_download(self, username: str) -> dict:
        """Stop a specific download gracefully, allowing finalization/mux."""
        username = username.strip().lower()
        async with self._lock:
            task = self._tasks.get(username)
            if username not in self._tasks:
                return {"error": f"No active download for {username}"}
            if not isinstance(task, asyncio.Task):
                # Cancel a reserved-but-not-yet-started download.
                self._tasks.pop(username, None)
                return {"status": "stopped", "username": username}

        downloader = self._get_downloader()
        downloader.stop(username)

        if task and not task.done():
            # Wait for the task to finish naturally (stop_event makes the
            # download loop exit, then _finalize runs the mux).
            done, _ = await asyncio.wait([task], timeout=120)
            if not done:
                logger.warning("Task for %s did not finish in time, cancelling", username)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        async with self._lock:
            if self._tasks.get(username) is task:
                self._tasks.pop(username, None)
        return {"status": "stopped", "username": username}

    async def stop_all(self) -> dict:
        """Stop all active downloads gracefully, allowing finalization/mux."""
        downloader = self._get_downloader()
        async with self._lock:
            self._stopping_all = True
            snapshot = list(self._tasks.items())
            # Remove reservations atomically so in-flight starts cannot turn
            # them into tasks while stop-all is waiting on existing tasks.
            for username, entry in snapshot:
                if not isinstance(entry, asyncio.Task) and self._tasks.get(username) is entry:
                    self._tasks.pop(username, None)

        try:
            for username, task in snapshot:
                if isinstance(task, asyncio.Task):
                    downloader.stop(username)
            downloader.stop_all()

            pending = [
                task
                for _, task in snapshot
                if isinstance(task, asyncio.Task) and not task.done()
            ]
            if pending:
                done, not_done = await asyncio.wait(pending, timeout=120)
                for task in not_done:
                    logger.warning("Force-cancelling unfinished task")
                    task.cancel()
                if not_done:
                    await asyncio.gather(*not_done, return_exceptions=True)

            async with self._lock:
                for username, entry in snapshot:
                    if self._tasks.get(username) is entry:
                        self._tasks.pop(username, None)
            return {"status": "all_stopped"}
        finally:
            async with self._lock:
                self._stopping_all = False

    def get_status(self) -> list[dict]:
        """Get status of all downloads."""
        statuses = []
        for username, progress in self._downloads.items():
            info = progress.to_dict()
            task = self._tasks.get(username)
            info["active"] = isinstance(task, asyncio.Task) and not task.done()
            statuses.append(info)
        return statuses

    def get_download(self, username: str) -> Optional[dict]:
        """Get status of a single download."""
        if username in self._downloads:
            info = self._downloads[username].to_dict()
            task = self._tasks.get(username)
            info["active"] = isinstance(task, asyncio.Task) and not task.done()
            return info
        return None
