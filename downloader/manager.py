"""
Manages multiple concurrent downloads.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Dict, Optional

from .extractor import extract_hls_url
from .hls import HLSDownloader, DownloadProgress

logger = logging.getLogger(__name__)


class DownloadManager:
    """Central manager for all active downloads."""

    def __init__(self):
        self._downloads: Dict[str, DownloadProgress] = {}
        self._tasks: Dict[str, asyncio.Task] = {}
        self._downloader: Optional[HLSDownloader] = None
        self._lock = asyncio.Lock()

    def _get_downloader(self) -> HLSDownloader:
        if self._downloader is None:
            self._downloader = HLSDownloader(
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

        async with self._lock:
            if username in self._tasks and not self._tasks[username].done():
                return {"error": f"Already downloading {username}"}
            # Reserve the slot immediately to prevent race conditions
            self._tasks[username] = None  # type: ignore[assignment]

        try:
            logger.info("Extracting HLS URL for '%s'...", username)
            hls_url = await extract_hls_url(username)
            if not hls_url:
                async with self._lock:
                    self._tasks.pop(username, None)
                return {
                    "error": f"Could not get stream URL. Is the room '{username}' online?"
                }

            logger.info("Got HLS URL for '%s': %s", username, hls_url)

            downloader = self._get_downloader()
            progress = DownloadProgress(username=username)
            self._downloads[username] = progress

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
                        self._tasks.pop(username, None)

            task = asyncio.create_task(_run())
            async with self._lock:
                self._tasks[username] = task

            return {"status": "started", "username": username, "hls_url": hls_url}
        except Exception:
            async with self._lock:
                self._tasks.pop(username, None)
            raise

    async def stop_download(self, username: str) -> dict:
        """Stop a specific download gracefully, allowing finalization/mux."""
        username = username.strip().lower()
        if username not in self._tasks:
            return {"error": f"No active download for {username}"}

        downloader = self._get_downloader()
        downloader.stop(username)

        task = self._tasks.get(username)
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
            self._tasks.pop(username, None)
        return {"status": "stopped", "username": username}

    async def stop_all(self) -> dict:
        """Stop all active downloads gracefully, allowing finalization/mux."""
        downloader = self._get_downloader()
        downloader.stop_all()

        pending = [t for t in self._tasks.values() if t and not t.done()]
        if pending:
            done, not_done = await asyncio.wait(pending, timeout=120)
            for task in not_done:
                logger.warning("Force-cancelling unfinished task")
                task.cancel()
            if not_done:
                await asyncio.gather(*not_done, return_exceptions=True)

        async with self._lock:
            self._tasks.clear()
        return {"status": "all_stopped"}

    def get_status(self) -> list[dict]:
        """Get status of all downloads."""
        statuses = []
        for username, progress in self._downloads.items():
            info = progress.to_dict()
            task = self._tasks.get(username)
            info["active"] = task is not None and not task.done()
            statuses.append(info)
        return statuses

    def get_download(self, username: str) -> Optional[dict]:
        """Get status of a single download."""
        if username in self._downloads:
            info = self._downloads[username].to_dict()
            task = self._tasks.get(username)
            info["active"] = task is not None and not task.done()
            return info
        return None
