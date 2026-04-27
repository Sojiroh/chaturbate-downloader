# CB Stream Saver

A self-hosted FastAPI web application for downloading live HLS streams from
Chaturbate rooms. Runs locally, exposes a small browser UI, and handles the
full pipeline: URL extraction, segment downloading with automatic token
refresh, parallel video + audio fetching, and final remux into an MP4 with
ffmpeg.

This project is intended for personal, local use only.

---

## Features

- **Web UI** — start, monitor, and stop downloads from the browser.
- **Multiple concurrent downloads** — each stream runs in its own task under
  a central `DownloadManager`.
- **Robust URL extraction** — four fallback strategies (`chatvideocontext`
  API, embedded `initialRoomDossier` JSON, `get_edge_hls_url_ajax` CSRF, and
  a regex sweep on the page HTML).
- **Audio + video muxing** — Chaturbate serves LL-HLS with split video and
  audio playlists. Both tracks are downloaded in parallel and muxed with
  ffmpeg while preserving source timestamps.
- **Token refresh on 403** — HLS tokens are single-use and short-lived, so
  the downloader re-extracts the URL on `403` responses (up to 10 refreshes
  per track).
- **Optional max duration** — cap a download at N minutes.
- **File management endpoints** — list, download, and delete finished files.
- **Debug endpoints** — inspect extraction and playlist parsing without
  committing to a full download. Token-bearing URLs are redacted in responses.
- **Path-traversal safe** — all filenames are resolved inside the
  `downloads/` directory.
- **Local request protection** — state-changing endpoints reject cross-site
  browser requests and unconfigured origins.

---

## How it works

```
  Browser UI  ──►  FastAPI (app.py)  ──►  DownloadManager
                                              │
                                              ▼
                                        extractor.py
                                    (4 strategies for HLS URL)
                                              │
                                              ▼
                                          hls.py
                               (parses master playlist,
                                downloads video + audio in parallel,
                                refreshes token on 403)
                                              │
                                              ▼
                                       converter.py
                                     (ffmpeg mux + A/V sync)
                                              │
                                              ▼
                                    downloads/<user>_<ts>.mp4
```

Each HLS master playlist contains video variants and a separate audio media
playlist. `HLSDownloader` picks the highest-bandwidth video variant, finds
its matching audio group, and streams both to temporary `.mp4` files. When
the download ends (user stop, timeout, stream going offline, or too many
errors), `mux_video_audio` combines them into a single MP4 with
`-movflags +faststart`.

---

## Requirements

- **Python 3.10+**
- **[uv](https://docs.astral.sh/uv/)** for dependency management
- **ffmpeg** and **ffprobe** on `PATH` (required for final MP4 muxing and
  timestamp/duration diagnostics)

Python dependencies (declared in `pyproject.toml`):

- `fastapi`, `uvicorn[standard]`
- `httpx`
- `m3u8`
- `jinja2`
- `python-multipart`

Development dependencies:

- `pytest`

---

## Installation

Clone the repo and run the setup script:

```bash
./setup.sh
```

The script verifies `uv` and Python 3.10+, runs `uv sync` to install
dependencies, and warns if `ffmpeg` is missing.

Install `ffmpeg` if needed:

```bash
# Arch
sudo pacman -S ffmpeg

# Debian / Ubuntu
sudo apt install ffmpeg

# macOS
brew install ffmpeg
```

---

## Running

```bash
uv run python app.py
```

Then open [http://localhost:8000](http://localhost:8000).

### Environment variables

| Variable       | Default                  | Purpose                                       |
| -------------- | ------------------------ | --------------------------------------------- |
| `HOST`         | `127.0.0.1`              | Bind address for uvicorn                      |
| `PORT`         | `8000`                   | Port                                          |
| `CORS_ORIGINS` | `http://localhost:8000,http://127.0.0.1:8000,http://[::1]:8000` | Comma-separated list of allowed UI origins for CORS and state-changing requests |

Example:

```bash
HOST=0.0.0.0 PORT=9000 uv run python app.py
```

---

## Web UI

The main page (`templates/index.html`) shows three sections:

1. **New Download** — enter a username and optionally set a max duration in
   minutes. Output is MP4.
2. **Active Downloads** — live progress cards (segments, speed, elapsed
   time, status) with per-download stop and a global "Stop All" button.
3. **Completed Downloads** — list of finished files with download and
   delete actions.

Downloads land in `./downloads/<username>_<YYYY-MM-DD_HH-MM-SS>.mp4`.

---

## API reference

All endpoints are under the FastAPI app at `/`.

### Downloads

| Method | Path                                | Description                                    |
| ------ | ----------------------------------- | ---------------------------------------------- |
| POST   | `/api/download/start`               | Start a download (query: `username`, `output_format`, `max_duration` in minutes) |
| POST   | `/api/download/stop/{username}`     | Stop one download gracefully (waits for mux)   |
| POST   | `/api/download/stop-all`            | Stop all active downloads                      |
| GET    | `/api/download/status`              | List status of every tracked download          |
| GET    | `/api/download/status/{username}`   | Status of one download                         |
| GET    | `/api/download/file/{username}`     | Legacy helper: stream the latest completed file for `{username}` |

### Files

| Method | Path                         | Description                               |
| ------ | ---------------------------- | ----------------------------------------- |
| GET    | `/api/downloads/list`             | List completed `.mp4` files in `downloads/` |
| GET    | `/api/downloads/file/{filename}`  | Stream one exact completed file by filename |
| DELETE | `/api/downloads/{filename}`       | Delete a completed file (path-traversal safe) |

### Debug

| Method | Path                              | Description                                                |
| ------ | --------------------------------- | ---------------------------------------------------------- |
| GET    | `/api/debug/extract/{username}`   | Run the extractor for a room and return the URL + logs    |
| GET    | `/api/debug/playlist/{username}`  | Fetch and parse the master + selected variant playlists   |

Username validation (`^[a-zA-Z0-9_]{1,50}$`) is applied on every endpoint
that accepts one.

State-changing endpoints (`POST`/`DELETE`) reject browser cross-site requests
and origins outside `CORS_ORIGINS`. Keep `CORS_ORIGINS` in sync if you change
`HOST`/`PORT` or put the UI behind a different local origin.

---

## Project structure

```
chaturbate/
├── app.py                 # FastAPI app, routes, validation, lifespan
├── downloader/
│   ├── __init__.py        # Public exports
│   ├── extractor.py       # 4 strategies to pull a fresh HLS URL
│   ├── hls.py             # LL-HLS downloader (video + audio, token refresh)
│   ├── converter.py       # ffmpeg remux + video/audio mux with A/V sync
│   └── manager.py         # DownloadManager: tasks, state, lifecycle
├── templates/
│   └── index.html         # Single-page UI
├── static/
│   ├── app.js             # Frontend logic (polling, forms, file list)
│   └── style.css
├── tests/
│   └── test_backend.py    # Backend safety/regression tests
├── downloads/             # Output directory (gitignored)
├── pyproject.toml         # uv / PEP 621 project definition
├── setup.sh               # One-shot environment check and install
└── uv.lock
```

---

## Notes and caveats

- **Single-use tokens.** Chaturbate's HLS URLs are session-bound. The
  extractor never probes the URL itself — the first HTTP GET must be made
  by the downloader, or the token gets burned. API responses and logs redact
  token-bearing URLs where possible.
- **A/V drift.** Video and audio come from separate HLS playlists and
  occasionally start at slightly different timestamps. `mux_video_audio`
  probes both with `ffprobe` for diagnostics and uses `-copyts` /
  `-start_at_zero` so ffmpeg preserves their relative timestamps.
- **Mux fallback.** If muxing fails, the video-only file is kept and
  `error_message` is set to `"Mux failed, file is video-only"`.
- **Graceful stop.** `stop_download` sets an event instead of cancelling
  the task, so the current segment batch finishes and the mux runs before
  the task exits. There's a 120 s watchdog if the task hangs.
- **Local-only security model.** The server is intended for local use. It
  rejects obvious browser cross-site writes, but it is not a full
  authentication layer. Do not expose it to the public internet. The default
  `HOST` is `127.0.0.1` for this reason.

---

## Tests

```bash
uv run pytest
```

The current tests cover validation, path/file safety, completed-file listing,
origin checks, URL redaction, and download-manager start/stop race regressions.

---

## Troubleshooting

- **"Could not get stream URL. Is the room online?"** — All four
  extraction strategies returned nothing. The room is offline, banned, or
  Chaturbate changed its API. Hit `/api/debug/extract/{username}` to see
  which strategy failed and why.
- **403s in the logs** — Normal; the downloader will refresh the token
  automatically up to 10 times per track. Persistent 403s usually mean the
  room went private or offline.
- **`ffmpeg not found in PATH`** — Install `ffmpeg` (see above). Without
  it, downloads still run but the final mux step will fail.
- **No audio in the output** — Check the logs for
  `No matching audio found for group`. Some variants reference audio
  groups that aren't in the master playlist; the downloader keeps the
  video-only file in that case.

---

## License / disclaimer

This project is provided as-is for personal, local, educational use. Respect
Chaturbate's terms of service and the rights of performers. Do not
redistribute downloaded content.
