"""
Converts and muxes media files using ffmpeg.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from typing import Optional

logger = logging.getLogger(__name__)


def _ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def _probe_start_time(filepath: str) -> Optional[float]:
    """Get the start_time of the first stream in a media file using ffprobe.

    Uses stream=start_time which is more accurate than format=start_time
    for fMP4 files with a single track.
    """
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    cmd = [
        ffprobe,
        "-v",
        "quiet",
        "-select_streams",
        "0:0",
        "-show_entries",
        "stream=start_time",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        filepath,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        val = result.stdout.strip()
        if val and val != "N/A":
            return float(val)
    except Exception:
        pass
    return None


def _probe_duration(filepath: str) -> Optional[float]:
    """Get the duration of a media file using ffprobe."""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    cmd = [
        ffprobe,
        "-v",
        "quiet",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        filepath,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        val = result.stdout.strip()
        if val and val != "N/A":
            return float(val)
    except Exception:
        pass
    return None


def convert_to_mp4(input_file: str, output_mp4: str) -> bool:
    """
    Remux a file to MP4 using ffmpeg stream copy.
    """
    if not _ffmpeg_available():
        logger.error("ffmpeg not found in PATH.")
        return False

    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        input_file,
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        output_mp4,
    ]

    logger.info("Remuxing %s -> %s", input_file, output_mp4)

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode == 0:
            logger.info("Remux successful: %s", output_mp4)
            return True
        logger.error("ffmpeg failed: %s", result.stderr[-500:])
        return False
    except subprocess.TimeoutExpired:
        logger.error("ffmpeg timed out for %s", input_file)
        return False
    except Exception as exc:
        logger.error("Remux error: %s", exc)
        return False


def mux_video_audio(
    video_file: str,
    audio_file: str,
    output_file: str,
) -> bool:
    """
    Mux separate video and audio files into a single MP4.

    Uses -copyts so that ffmpeg preserves each input's original TFDT
    timestamps instead of resetting them to zero. This keeps the
    relative A/V offset intact and avoids drift caused by missing
    fMP4 segments (which leave holes in the timeline that must be
    respected, not collapsed).
    """
    if not _ffmpeg_available():
        logger.error("ffmpeg not found in PATH. Cannot mux.")
        return False

    # Informational: log start times and durations so sync issues
    # are visible in logs even though we no longer correct them
    # manually (-copyts does that now).
    video_start = _probe_start_time(video_file)
    audio_start = _probe_start_time(audio_file)
    if video_start is not None and audio_start is not None:
        logger.info(
            "Input start_times: video=%.3fs, audio=%.3fs, delta=%.3fs",
            video_start,
            audio_start,
            audio_start - video_start,
        )

    vid_dur = _probe_duration(video_file)
    aud_dur = _probe_duration(audio_file)
    if vid_dur is not None and aud_dur is not None:
        delta = abs(vid_dur - aud_dur)
        logger.info(
            "Track durations: video=%.1fs, audio=%.1fs, delta=%.1fs",
            vid_dur,
            aud_dur,
            delta,
        )
        if delta > 2.0:
            logger.warning(
                "Duration mismatch >2s — one track lost more segments than the other",
            )

    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        video_file,
        "-i",
        audio_file,
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-c",
        "copy",
        # Preserve original PTS/DTS from both inputs so their relative
        # alignment (encoded in TFDT) survives the mux.
        "-copyts",
        # When -copyts is used, shift so the earliest timestamp is 0
        # while keeping the relative offset between streams.
        "-start_at_zero",
        # Regenerate any missing PTS from DTS without discarding the
        # ones we just preserved.
        "-fflags",
        "+genpts",
        # Guard against any residual negative timestamps after the
        # shift above.
        "-avoid_negative_ts",
        "make_zero",
        "-movflags",
        "+faststart",
        output_file,
    ]

    logger.info("Muxing video + audio -> %s", output_file)

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if result.returncode == 0:
            logger.info("Mux successful: %s", output_file)
            return True
        logger.error("ffmpeg mux failed: %s", result.stderr[-500:])
        return False
    except subprocess.TimeoutExpired:
        logger.error("ffmpeg mux timed out")
        return False
    except Exception as exc:
        logger.error("Mux error: %s", exc)
        return False
