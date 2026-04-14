"""
FastAPI web server for the Chaturbate stream downloader.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from downloader import DownloadManager

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# Global download manager
manager = DownloadManager()

TEMPLATE_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"
DOWNLOADS_DIR = (Path(__file__).parent / "downloads").resolve()
DOWNLOADS_DIR.mkdir(exist_ok=True)

USERNAME_RE = re.compile(r"^[a-zA-Z0-9_]{1,50}$")

ALLOWED_ORIGINS = os.getenv("CORS_ORIGINS", "http://localhost:8000").split(",")


def _validate_username(username: str) -> str:
    """Validate and normalize a username."""
    username = username.strip().lower()
    if not USERNAME_RE.match(username):
        raise HTTPException(status_code=400, detail="Invalid username")
    return username


def _safe_downloads_path(filename: str) -> Path:
    """Resolve a filename inside DOWNLOADS_DIR, preventing path traversal."""
    file_path = (DOWNLOADS_DIR / filename).resolve()
    if not file_path.is_relative_to(DOWNLOADS_DIR):
        raise HTTPException(status_code=400, detail="Invalid filename")
    return file_path


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Handle startup/shutdown."""
    logger.info("Starting Chaturbate Downloader")
    yield
    # Shutdown: stop all downloads
    await manager.stop_all()
    logger.info("Shutting down")


app = FastAPI(title="Chaturbate Stream Downloader", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Static files
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ─── Web UI ────────────────────────────────────────────────


@app.get("/", response_class=HTMLResponse)
async def index():
    """Serve the main web UI."""
    html_path = TEMPLATE_DIR / "index.html"
    return HTMLResponse(content=html_path.read_text())


# ─── API Endpoints ─────────────────────────────────────────


@app.post("/api/download/start")
async def start_download(
    username: str,
    output_format: str = "mp4",
    max_duration: Optional[int] = None,
):
    """Start downloading a stream."""
    username = _validate_username(username)
    if output_format not in ("mp4", "ts"):
        raise HTTPException(status_code=400, detail="Format must be 'mp4' or 'ts'")
    if max_duration is not None and max_duration <= 0:
        raise HTTPException(status_code=400, detail="max_duration must be positive")
    # Convert minutes (from UI) to seconds for the backend
    duration_seconds = max_duration * 60 if max_duration else None
    result = await manager.start_download(
        username=username,
        output_format=output_format,
        max_duration=duration_seconds,
    )
    if "error" in result:
        raise HTTPException(status_code=409, detail=result["error"])
    return result


@app.post("/api/download/stop/{username}")
async def stop_download(username: str):
    """Stop a specific download."""
    username = _validate_username(username)
    result = await manager.stop_download(username)
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return result


@app.post("/api/download/stop-all")
async def stop_all():
    """Stop all active downloads."""
    return await manager.stop_all()


@app.get("/api/download/status")
async def get_all_status():
    """Get status of all downloads."""
    return manager.get_status()


@app.get("/api/download/status/{username}")
async def get_status(username: str):
    """Get status of a specific download."""
    username = _validate_username(username)
    result = manager.get_download(username)
    if result is None:
        raise HTTPException(status_code=404, detail="Download not found")
    return result


@app.get("/api/download/file/{username}")
async def download_file(username: str):
    """Download the completed file."""
    username = _validate_username(username)

    # Try download status first
    status = manager.get_download(username)
    if status and status.get("output_path"):
        file_path = Path(status["output_path"]).resolve()
        if file_path.is_relative_to(DOWNLOADS_DIR) and file_path.exists():
            media_type = "video/mp4" if file_path.suffix == ".mp4" else "video/MP2T"
            return FileResponse(
                path=str(file_path),
                media_type=media_type,
                filename=file_path.name,
            )

    # Fallback: scan downloads directory for any file starting with username_
    for f in sorted(DOWNLOADS_DIR.iterdir(), reverse=True):
        if f.is_file() and f.name.startswith(f"{username}_") and f.suffix == ".mp4":
            return FileResponse(
                path=str(f),
                media_type="video/mp4",
                filename=f.name,
            )

    raise HTTPException(status_code=404, detail="File not found")


@app.get("/api/downloads/list")
async def list_downloaded_files():
    """List all downloaded files."""
    files = []
    for f in DOWNLOADS_DIR.iterdir():
        if f.is_file() and f.suffix in (".mp4", ".ts"):
            stem = f.stem
            username = stem.rsplit("_20", 1)[0] if "_20" in stem else stem
            st = f.stat()
            files.append(
                {
                    "filename": f.name,
                    "username": username,
                    "size": st.st_size,
                    "size_mb": round(st.st_size / (1024 * 1024), 2),
                    "format": f.suffix.lstrip("."),
                }
            )
    return sorted(files, key=lambda x: x["filename"])


@app.delete("/api/downloads/{filename}")
async def delete_file(filename: str):
    """Delete a downloaded file."""
    file_path = _safe_downloads_path(filename)
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    file_path.unlink()
    return {"status": "deleted", "filename": filename}


# ─── Debug Endpoints ──────────────────────────────────────


@app.get("/api/debug/extract/{username}")
async def debug_extract(username: str):
    """Debug: test HLS URL extraction for a room without downloading."""
    from downloader.extractor import extract_hls_url
    import io

    username = _validate_username(username)

    log_capture = io.StringIO()
    handler = logging.StreamHandler(log_capture)
    handler.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(levelname)s %(name)s: %(message)s")
    handler.setFormatter(fmt)
    logging.getLogger("downloader").addHandler(handler)

    try:
        url = await extract_hls_url(username)
    finally:
        logging.getLogger("downloader").removeHandler(handler)

    logs = log_capture.getvalue()

    return {
        "username": username,
        "hls_url": url,
        "found": url is not None,
        "logs": logs,
    }


@app.get("/api/debug/playlist/{username}")
async def debug_playlist(username: str):
    """Debug: fetch and parse the HLS playlist, show its full contents."""
    import httpx
    import m3u8
    from urllib.parse import urljoin
    import io

    username = _validate_username(username)

    log_capture = io.StringIO()
    handler = logging.StreamHandler(log_capture)
    handler.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(levelname)s %(name)s: %(message)s")
    handler.setFormatter(fmt)
    logging.getLogger("downloader").addHandler(handler)

    try:
        from downloader.extractor import extract_hls_url, DEFAULT_HEADERS

        hls_url = await extract_hls_url(username)

        if not hls_url:
            return {
                "username": username,
                "error": "No HLS URL found",
                "logs": log_capture.getvalue(),
            }

        async with httpx.AsyncClient(
            headers=DEFAULT_HEADERS,
            timeout=httpx.Timeout(20.0),
            follow_redirects=True,
        ) as client:
            resp = await client.get(hls_url)
            master_body = resp.text
            master_status = resp.status_code
            master_playlist = m3u8.loads(master_body)
            is_variant = master_playlist.is_variant

            result = {
                "username": username,
                "hls_url": hls_url,
                "master_status": master_status,
                "master_is_variant": is_variant,
                "master_content": master_body[:3000],
                "master_content_length": len(master_body),
            }

            if is_variant and master_playlist.playlists:
                variants = []
                for p in master_playlist.playlists:
                    var_url = p.uri
                    if not var_url.startswith("http"):
                        var_url = urljoin(hls_url, var_url)
                    variants.append(
                        {
                            "uri": var_url,
                            "bandwidth": p.stream_info.bandwidth,
                            "resolution": str(p.stream_info.resolution)
                            if p.stream_info.resolution
                            else None,
                        }
                    )
                result["variants"] = variants

                best = max(
                    master_playlist.playlists,
                    key=lambda p: p.stream_info.bandwidth or 0,
                )
                best_url = best.uri
                if not best_url.startswith("http"):
                    best_url = urljoin(hls_url, best_url)

                result["selected_variant_url"] = best_url

                resp2 = await client.get(best_url)
                variant_body = resp2.text
                variant_playlist = m3u8.loads(variant_body)

                result["variant_status"] = resp2.status_code
                result["variant_content"] = variant_body[:5000]
                result["variant_content_length"] = len(variant_body)
                result["variant_segment_count"] = len(variant_playlist.segments)
                result["variant_has_segment_map"] = bool(variant_playlist.segment_map)
                result["variant_is_variant"] = variant_playlist.is_variant
                result["variant_target_duration"] = variant_playlist.target_duration

                segs = []
                for s in variant_playlist.segments[:5]:
                    seg_url = s.uri or ""
                    if seg_url and not seg_url.startswith("http"):
                        seg_url = urljoin(best_url, seg_url)
                    segs.append({"uri": seg_url[:150], "duration": s.duration})
                result["first_segments"] = segs

            elif master_playlist.segments:
                result["direct_segment_count"] = len(master_playlist.segments)
                segs = []
                for s in master_playlist.segments[:5]:
                    seg_url = s.uri or ""
                    if seg_url and not seg_url.startswith("http"):
                        seg_url = urljoin(hls_url, seg_url)
                    segs.append({"uri": seg_url[:150], "duration": s.duration})
                result["first_segments"] = segs
            else:
                result["no_segments_found"] = True
                result["raw_parse_check"] = "#EXTINF" in master_body

        result["logs"] = log_capture.getvalue()
        return result
    finally:
        logging.getLogger("downloader").removeHandler(handler)


def main():
    import uvicorn

    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
