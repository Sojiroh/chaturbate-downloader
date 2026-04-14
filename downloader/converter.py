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
    """Get the start_time of a media file using ffprobe."""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    cmd = [
        ffprobe,
        "-v",
        "quiet",
        "-show_entries",
        "format=start_time",
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

    Probes start timestamps of both tracks and uses -itsoffset to
    compensate for the A/V offset that occurs when video and audio
    are downloaded independently from separate HLS playlists.
    """
    if not _ffmpeg_available():
        logger.error("ffmpeg not found in PATH. Cannot mux.")
        return False

    # Detect A/V timestamp offset
    video_start = _probe_start_time(video_file)
    audio_start = _probe_start_time(audio_file)

    cmd = ["ffmpeg", "-y"]

    if video_start is not None and audio_start is not None:
        offset = audio_start - video_start
        if abs(offset) > 0.05:
            logger.info(
                "A/V offset detected: video=%.3fs, audio=%.3fs, delta=%.3fs",
                video_start,
                audio_start,
                offset,
            )
            # -itsoffset shifts the next input's timestamps.
            # Negative of offset brings audio in line with video.
            cmd.extend([
                "-i",
                video_file,
                "-itsoffset",
                f"{-offset:.3f}",
                "-i",
                audio_file,
            ])
        else:
            logger.info("A/V offset negligible (%.3fs), no correction needed", offset)
            cmd.extend(["-i", video_file, "-i", audio_file])
    else:
        logger.info("Could not probe start times, muxing without offset correction")
        cmd.extend(["-i", video_file, "-i", audio_file])

    cmd.extend([
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        "-shortest",
        output_file,
    ])

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
