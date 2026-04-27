"""
HLS downloader with audio+video and automatic token refresh.

Chaturbate uses LL-HLS with separate video and audio playlists.
The master playlist has:
  - Video variants (different resolutions, video-only)
  - Audio media playlists (AAC 96k / 128k)

This downloader:
1. Parses the master playlist to find video + matching audio
2. Downloads both streams in parallel
3. Muxes them together with ffmpeg at the end
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional, Callable, Tuple
from urllib.parse import urljoin

import httpx
import m3u8

from .extractor import DEFAULT_HEADERS

logger = logging.getLogger(__name__)

DEFAULT_CONCURRENCY = 8
SEGMENT_TIMEOUT = 30.0
SEGMENT_MAX_RETRIES = 3
SEGMENT_RETRY_BASE_DELAY = 0.5
MAX_PLAYLIST_ERRORS = 15
MAX_EMPTY_POLLS = 60
MAX_TOKEN_REFRESHES = 10
MAX_PLAYLIST_RECURSION = 5
MAX_SEEN_URLS = 10000


@dataclass
class DownloadProgress:
    username: str
    total_segments: int = 0
    downloaded_segments: int = 0
    failed_segments: int = 0
    bytes_downloaded: int = 0
    start_time: float = field(default_factory=time.time)
    is_live: bool = True
    output_path: str = ""
    error_message: str = ""
    status: str = "starting"

    @property
    def progress_pct(self) -> float:
        if self.total_segments == 0:
            return 0.0
        return (self.downloaded_segments / self.total_segments) * 100

    @property
    def speed_mbps(self) -> float:
        elapsed = time.time() - self.start_time
        if elapsed == 0:
            return 0.0
        return (self.bytes_downloaded / elapsed) / (1024 * 1024)

    @property
    def elapsed_seconds(self) -> float:
        return time.time() - self.start_time

    def to_dict(self) -> dict:
        return {
            "username": self.username,
            "total_segments": self.total_segments,
            "downloaded_segments": self.downloaded_segments,
            "failed_segments": self.failed_segments,
            "bytes_downloaded": self.bytes_downloaded,
            "progress_pct": round(self.progress_pct, 1),
            "speed_mbps": round(self.speed_mbps, 2),
            "elapsed_seconds": round(self.elapsed_seconds, 1),
            "is_live": self.is_live,
            "output_path": self.output_path,
            "error_message": self.error_message,
            "status": self.status,
        }


class HLSDownloader:
    def __init__(
        self,
        output_dir: str = "downloads",
        concurrency: int = DEFAULT_CONCURRENCY,
        on_progress: Optional[Callable[[DownloadProgress], None]] = None,
    ):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.concurrency = concurrency
        self.on_progress = on_progress
        self._stop_events: dict[str, asyncio.Event] = {}
        self.refresh_url_callback: Optional[Callable] = None

    def stop(self, username: str):
        event = self._stop_events.setdefault(username, asyncio.Event())
        event.set()

    def stop_all(self):
        for event in self._stop_events.values():
            event.set()

    async def download_stream(
        self,
        username: str,
        m3u8_url: str,
        output_format: str = "mp4",
        max_duration: Optional[int] = None,
    ) -> DownloadProgress:
        """Download a live HLS stream with video + audio."""
        progress = DownloadProgress(username=username)
        stop_event = self._stop_events.setdefault(username, asyncio.Event())
        current_task = asyncio.current_task()
        if current_task is not None:
            current_task.add_done_callback(
                lambda _task: self._stop_events.pop(username, None)
                if self._stop_events.get(username) is stop_event
                else None
            )

        ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        file_prefix = f"{username}_{ts}"

        logger.info("Starting download for %s", username)

        async with httpx.AsyncClient(
            timeout=httpx.Timeout(SEGMENT_TIMEOUT),
            follow_redirects=True,
            headers=DEFAULT_HEADERS,
        ) as client:
            video_url, audio_url = await self._resolve_master(client, m3u8_url)

            if not video_url:
                progress.error_message = "No se pudo resolver el playlist de video"
                progress.status = "error"
                progress.is_live = False
                self._stop_events.pop(username, None)
                if self.on_progress:
                    self.on_progress(progress)
                return progress

            logger.info("Video playlist: %s", video_url[:100])
            if audio_url:
                logger.info("Audio playlist: %s", audio_url[:100])

            # Validate both playlists are accessible in parallel so
            # neither track gets a head-start.
            validation_tasks = [self._validate_playlist(client, video_url, "video")]
            if audio_url:
                validation_tasks.append(
                    self._validate_playlist(client, audio_url, "audio")
                )
            validation_results = await asyncio.gather(
                *validation_tasks,
                return_exceptions=True,
            )

            for vr in validation_results:
                if isinstance(vr, Exception):
                    progress.error_message = f"Playlist inaccesible: {vr}"
                    progress.status = "error"
                    progress.is_live = False
                    self._stop_events.pop(username, None)
                    if self.on_progress:
                        self.on_progress(progress)
                    return progress

            # Startup barrier: both tracks wait until the other has
            # resolved its first playlist snapshot before downloading
            # any segments, reducing initial A/V offset.
            start_barrier = asyncio.Event()

            video_file = self.output_dir / f"{file_prefix}_video.mp4"
            audio_file = self.output_dir / f"{file_prefix}_audio.mp4"

            progress.status = "downloading"
            if self.on_progress:
                self.on_progress(progress)

            video_coro = self._download_track(
                client,
                stop_event,
                video_url,
                video_file,
                username,
                "video",
                progress,
                max_duration,
                start_barrier,
                require_barrier=audio_url is not None,
            )

            if audio_url:
                audio_coro = self._download_track(
                    client,
                    stop_event,
                    audio_url,
                    audio_file,
                    username,
                    "audio",
                    progress,
                    max_duration,
                    start_barrier,
                    require_barrier=True,
                )
                results = await asyncio.gather(
                    video_coro,
                    audio_coro,
                    return_exceptions=True,
                )
                video_ok = results[0] if isinstance(results[0], bool) else False
                audio_ok = results[1] if isinstance(results[1], bool) else False
                if isinstance(results[0], Exception):
                    logger.error("[video] Exception: %s", results[0])
                if isinstance(results[1], Exception):
                    logger.error("[audio] Exception: %s", results[1])
            else:
                video_ok = await video_coro
                audio_ok = False

            return await self._finalize(
                progress,
                username,
                file_prefix,
                video_file,
                audio_file,
                audio_ok,
            )

    async def _resolve_master(
        self,
        client: httpx.AsyncClient,
        master_url: str,
    ) -> Tuple[Optional[str], Optional[str]]:
        """Parse master playlist to extract video variant URL and audio URL."""
        try:
            resp = await client.get(master_url)
            resp.raise_for_status()
        except Exception as exc:
            logger.error("Cannot fetch master playlist: %s", exc)
            return None, None

        master = m3u8.loads(resp.text)

        if not master.is_variant:
            return master_url, None

        if not master.playlists:
            logger.error("Master playlist has no variants")
            return None, None

        best = max(master.playlists, key=lambda p: p.stream_info.bandwidth or 0)
        video_url = best.uri
        if not video_url.startswith("http"):
            video_url = urljoin(master_url, video_url)

        logger.info(
            "Selected video variant: %d bps, resolution=%s",
            best.stream_info.bandwidth,
            best.stream_info.resolution,
        )

        audio_url = None
        audio_group = None

        if hasattr(best.stream_info, "audio") and best.stream_info.audio:
            audio_group = best.stream_info.audio
            logger.info("Video references audio group: %s", audio_group)

        if audio_group and hasattr(master, "media") and master.media:
            for media in master.media:
                if (
                    media.type == "AUDIO"
                    and media.group_id == audio_group
                    and media.uri
                ):
                    audio_url = media.uri
                    if not audio_url.startswith("http"):
                        audio_url = urljoin(master_url, audio_url)
                    logger.info(
                        "Found audio for group '%s': %s",
                        audio_group,
                        audio_url[:100],
                    )
                    break

        if not audio_url:
            logger.warning("No matching audio found for group '%s'", audio_group)

        return video_url, audio_url

    @staticmethod
    async def _validate_playlist(
        client: httpx.AsyncClient,
        url: str,
        label: str,
    ) -> bool:
        """Verify a playlist URL is accessible."""
        resp = await client.get(url)
        logger.info(
            "[%s] Playlist validation: HTTP %d, %d bytes",
            label,
            resp.status_code,
            len(resp.text),
        )
        resp.raise_for_status()
        return True

    async def _download_track(
        self,
        client: httpx.AsyncClient,
        stop_event: asyncio.Event,
        playlist_url: Optional[str],
        output_file: Path,
        username: str,
        track_name: str,
        progress: DownloadProgress,
        max_duration: Optional[int],
        start_barrier: Optional[asyncio.Event] = None,
        require_barrier: bool = False,
    ) -> bool:
        """Download a single track (video or audio) from a media playlist."""
        if not playlist_url:
            return False

        semaphore = asyncio.Semaphore(self.concurrency)
        downloaded_urls: deque[str] = deque(maxlen=MAX_SEEN_URLS)
        downloaded_set: set[str] = set()
        init_written = False
        consecutive_errors = 0
        consecutive_empty = 0
        start_time = time.time()
        current_url = playlist_url
        token_refreshes = 0
        total_bytes = 0
        total_segments = 0
        first_playlist_done = False

        logger.info("[%s] Starting track download: %s", track_name, playlist_url[:100])

        with open(output_file, "wb") as f:
            while not stop_event.is_set():
                if max_duration and (time.time() - start_time) >= max_duration:
                    break

                # Fetch playlist
                try:
                    playlist = await self._fetch_media_playlist(client, current_url)
                    consecutive_errors = 0
                except httpx.HTTPStatusError as exc:
                    if (
                        exc.response.status_code == 403
                        and token_refreshes < MAX_TOKEN_REFRESHES
                    ):
                        token_refreshes += 1
                        logger.warning(
                            "[%s] 403, refreshing token (#%d)",
                            track_name,
                            token_refreshes,
                        )
                        new_url = await self._refresh_url(username)
                        if new_url:
                            video_url, audio_url = await self._resolve_master(
                                client, new_url
                            )
                            if track_name == "audio" and audio_url:
                                current_url = audio_url
                            elif video_url:
                                current_url = video_url
                            # Reset init fragment flag — the new URL may
                            # serve a different moov/init segment.
                            init_written = False
                            # Clear dedup sets since segment URLs change
                            # after a token refresh.
                            downloaded_set.clear()
                            downloaded_urls.clear()
                            logger.info(
                                "[%s] Token refreshed, URL updated",
                                track_name,
                            )
                            await asyncio.sleep(1)
                            continue

                    consecutive_errors += 1
                    if consecutive_errors >= MAX_PLAYLIST_ERRORS:
                        logger.error("[%s] Too many errors", track_name)
                        break
                    await asyncio.sleep(min(2**consecutive_errors, 30))
                    continue

                except Exception:
                    consecutive_errors += 1
                    if consecutive_errors >= MAX_PLAYLIST_ERRORS:
                        break
                    await asyncio.sleep(min(2**consecutive_errors, 30))
                    continue

                if playlist is None:
                    consecutive_errors += 1
                    if consecutive_errors >= MAX_PLAYLIST_ERRORS:
                        break
                    await asyncio.sleep(2)
                    continue

                base_url = self._get_base_url(current_url, playlist)

                # Startup barrier: signal that this track has resolved
                # its first playlist and wait for the other track.
                if not first_playlist_done:
                    first_playlist_done = True
                    if start_barrier is not None:
                        start_barrier.set()
                        if require_barrier:
                            logger.debug(
                                "[%s] Waiting for other track at barrier",
                                track_name,
                            )
                            await start_barrier.wait()

                # Init fragment
                if (
                    not init_written
                    and hasattr(playlist, "segment_map")
                    and playlist.segment_map
                ):
                    for init_seg in playlist.segment_map:
                        if not init_seg.uri:
                            continue
                        init_url = init_seg.uri
                        if not init_url.startswith("http"):
                            init_url = urljoin(base_url, init_url)
                        try:
                            data = await self._fetch_segment(
                                client, semaphore, init_url
                            )
                            f.write(data)
                            f.flush()
                            init_written = True
                            total_bytes += len(data)
                            logger.info(
                                "[%s] Init fragment: %d bytes", track_name, len(data)
                            )
                        except Exception as exc:
                            logger.error(
                                "[%s] Init fragment failed: %s", track_name, exc
                            )

                # New segments
                new_segments = []
                for segment in playlist.segments:
                    seg_url = segment.uri
                    if not seg_url:
                        continue
                    if not seg_url.startswith("http"):
                        seg_url = urljoin(base_url, seg_url)
                    if seg_url not in downloaded_set:
                        new_segments.append(seg_url)

                if new_segments:
                    consecutive_empty = 0
                    total_segments += len(new_segments)

                    # Build a URL -> duration map so we can log how much
                    # timeline we lose when a segment fails.
                    seg_durations: dict[str, float] = {}
                    for segment in playlist.segments:
                        seg_uri = segment.uri
                        if not seg_uri:
                            continue
                        if not seg_uri.startswith("http"):
                            seg_uri = urljoin(base_url, seg_uri)
                        seg_durations[seg_uri] = float(segment.duration or 0.0)

                    # Log discontinuity markers — Chaturbate emits these
                    # when restarting the encoder, and ignoring them can
                    # introduce visible sync breaks.
                    for segment in playlist.segments:
                        if getattr(segment, "discontinuity", False):
                            logger.warning(
                                "[%s] EXT-X-DISCONTINUITY in playlist — "
                                "downstream mux may show a seam",
                                track_name,
                            )
                            break

                    # Download segments sequentially in playlist order
                    # with retry to avoid fMP4 timestamp gaps.
                    for seg_idx, seg_url in enumerate(new_segments):
                        data = await self._fetch_segment_with_retry(
                            client,
                            semaphore,
                            seg_url,
                            max_retries=3,
                            track_name=track_name,
                        )
                        downloaded_set.add(seg_url)
                        downloaded_urls.append(seg_url)
                        if data is not None:
                            f.write(data)
                            total_bytes += len(data)
                            progress.downloaded_segments += 1
                            progress.bytes_downloaded += len(data)
                        else:
                            progress.failed_segments += 1
                            lost = seg_durations.get(seg_url, 0.0)
                            logger.warning(
                                "[%s] Lost %.2fs of timeline (failed segment) — "
                                "A/V may drift from this point",
                                track_name,
                                lost,
                            )

                    f.flush()
                    progress.total_segments += len(new_segments)

                    # Evict old URLs from the set if deque wrapped around
                    if len(downloaded_set) > MAX_SEEN_URLS:
                        downloaded_set = set(downloaded_urls)

                    if self.on_progress:
                        self.on_progress(progress)
                else:
                    consecutive_empty += 1
                    if consecutive_empty >= MAX_EMPTY_POLLS:
                        break
                    target_dur = playlist.target_duration or 2
                    await asyncio.sleep(target_dur / 2)

        try:
            file_size = output_file.stat().st_size
        except OSError:
            file_size = 0
        logger.info(
            "[%s] Track done: %d bytes, %d segments",
            track_name,
            file_size,
            total_segments,
        )
        return file_size > 0

    async def _finalize(
        self,
        progress: DownloadProgress,
        username: str,
        file_prefix: str,
        video_file: Path,
        audio_file: Path,
        audio_ok: bool = False,
    ) -> DownloadProgress:
        """Mux video + audio and clean up temp files."""
        progress.is_live = False
        self._stop_events.pop(username, None)

        video_exists = video_file.exists() and video_file.stat().st_size > 0

        if not video_exists:
            progress.error_message = "Sin datos de video descargados."
            progress.status = "error"
            for f in (video_file, audio_file):
                if f.exists():
                    f.unlink(missing_ok=True)
            if self.on_progress:
                self.on_progress(progress)
            return progress

        final_output = self.output_dir / f"{file_prefix}.mp4"
        audio_exists = (
            audio_ok and audio_file.exists() and audio_file.stat().st_size > 0
        )
        progress.status = "converting"
        if self.on_progress:
            self.on_progress(progress)

        if audio_exists:
            from .converter import mux_video_audio

            logger.info("Muxing video + audio for %s...", username)
            success = await asyncio.to_thread(
                mux_video_audio,
                str(video_file),
                str(audio_file),
                str(final_output),
            )
            if success:
                video_file.unlink(missing_ok=True)
                audio_file.unlink(missing_ok=True)
                progress.output_path = str(final_output)
                logger.info("Final muxed file: %s", final_output)
            else:
                logger.warning("Mux failed, keeping video-only")
                video_file.replace(final_output)
                audio_file.unlink(missing_ok=True)
                progress.output_path = str(final_output)
                progress.error_message = "Mux failed, file is video-only"
        else:
            video_file.replace(final_output)
            progress.output_path = str(final_output)
            if not audio_file.exists():
                progress.error_message = "Solo video (sin audio)"
            else:
                audio_file.unlink(missing_ok=True)

        progress.status = "done"
        if self.on_progress:
            self.on_progress(progress)
        return progress

    async def _refresh_url(self, username: str) -> Optional[str]:
        if self.refresh_url_callback:
            try:
                return await self.refresh_url_callback(username)
            except Exception as exc:
                logger.error("URL refresh callback failed: %s", exc)
                return None
        from .extractor import extract_hls_url

        try:
            return await extract_hls_url(username)
        except Exception as exc:
            logger.error("URL refresh failed: %s", exc)
            return None

    async def _fetch_media_playlist(
        self,
        client: httpx.AsyncClient,
        url: str,
        _depth: int = 0,
    ) -> Optional[m3u8.M3U8]:
        """Fetch and parse a media playlist (not master)."""
        if _depth > MAX_PLAYLIST_RECURSION:
            logger.error("Too many playlist recursion levels")
            return None

        resp = await client.get(url)
        logger.debug(
            "Playlist HTTP %d, len=%d for %s",
            resp.status_code,
            len(resp.text),
            url[:80],
        )
        resp.raise_for_status()

        body = resp.text.strip()
        if not body:
            return None

        playlist = m3u8.loads(body)

        if playlist.is_variant and playlist.playlists:
            best = max(playlist.playlists, key=lambda p: p.stream_info.bandwidth or 0)
            variant_url = best.uri
            if not variant_url.startswith("http"):
                variant_url = urljoin(url, variant_url)
            return await self._fetch_media_playlist(client, variant_url, _depth + 1)

        if not playlist.segments:
            return None

        return playlist

    @staticmethod
    def _get_base_url(playlist_url: str, playlist: m3u8.M3U8) -> str:
        if playlist.base_path:
            return playlist.base_path
        return playlist_url.rsplit("/", 1)[0] + "/"

    async def _fetch_segment(
        self,
        client: httpx.AsyncClient,
        semaphore: asyncio.Semaphore,
        url: str,
    ) -> bytes:
        async with semaphore:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.content

    async def _fetch_segment_with_retry(
        self,
        client: httpx.AsyncClient,
        semaphore: asyncio.Semaphore,
        url: str,
        max_retries: int = SEGMENT_MAX_RETRIES,
        track_name: str = "",
    ) -> Optional[bytes]:
        """Fetch a segment with retries. Returns None if all attempts fail."""
        for attempt in range(max_retries):
            try:
                return await self._fetch_segment(client, semaphore, url)
            except Exception as exc:
                if attempt < max_retries - 1:
                    delay = SEGMENT_RETRY_BASE_DELAY * (2**attempt)
                    logger.debug(
                        "[%s] Segment retry %d/%d in %.1fs: %s",
                        track_name,
                        attempt + 1,
                        max_retries,
                        delay,
                        str(exc)[:80],
                    )
                    await asyncio.sleep(delay)
                else:
                    logger.error(
                        "[%s] Segment failed after %d attempts: %s",
                        track_name,
                        max_retries,
                        url[:80],
                    )
        return None
