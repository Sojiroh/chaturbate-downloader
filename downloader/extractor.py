"""
Extracts HLS stream URL from a Chaturbate room.

IMPORTANT: Chaturbate HLS tokens are single-use / session-bound.
We do NOT probe the URL here — just return it fresh so the downloader
can use it on first fetch before the token expires.

Strategies:
1. chatvideocontext API (most reliable, 2025+)
2. initialRoomDossier JSON embedded in page HTML (yt-dlp approach)
3. get_edge_hls_url_ajax with CSRF token (streamlink approach)
4. Regex fallback on raw HTML
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Optional
from urllib.parse import urlencode

import httpx

logger = logging.getLogger(__name__)

BASE_DOMAIN = "https://chaturbate.com"
ROOM_URL = f"{BASE_DOMAIN}/{{username}}/"
CHATVIDEO_API = f"{BASE_DOMAIN}/api/chatvideocontext/{{username}}/"
EDGE_HLS_API = f"{BASE_DOMAIN}/get_edge_hls_url_ajax/"

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

M3U8_REGEX = re.compile(r'(https?://[^\s"\'\\]+\.m3u8[^\s"\'\\]*)')
DOSSIER_REGEX = re.compile(r'initialRoomDossier\s*=\s*(["\'])(?P<value>(?:(?!\1).)+)\1')


def _extract_dossier_json(html: str) -> Optional[dict]:
    """Extract the initialRoomDossier JSON from page HTML."""
    match = DOSSIER_REGEX.search(html)
    if not match:
        return None

    raw = match.group("value")
    try:
        decoded = raw.encode("utf-8").decode("unicode_escape")
        return json.loads(decoded)
    except Exception:
        pass
    try:
        return json.loads(raw)
    except Exception:
        return None


def _find_m3u8_in_html(html: str) -> Optional[str]:
    """Search for m3u8 URLs directly in the HTML source."""
    urls = M3U8_REGEX.findall(html)
    if urls:
        for url in urls:
            if "playlist" in url and "chunklist" not in url:
                return url
        return urls[0]
    return None


async def _strategy_chatvideocontext(
    client: httpx.AsyncClient, username: str
) -> Optional[str]:
    """Strategy 1: chatvideocontext API."""
    url = CHATVIDEO_API.format(username=username)
    try:
        resp = await client.get(url)
        if resp.status_code != 200:
            logger.debug("chatvideocontext: HTTP %d for %s", resp.status_code, username)
            return None

        data = resp.json()
        hls = data.get("hls_source")
        if not hls:
            room_status = data.get("room_status", "")
            logger.info(
                "chatvideocontext: room_status='%s' for %s", room_status, username
            )
            return None

        logger.info("Strategy 1 (chatvideocontext) got URL for %s", username)
        return hls

    except Exception as exc:
        logger.debug("Strategy 1 failed: %s", exc)
        return None


async def _strategy_dossier(client: httpx.AsyncClient, username: str) -> Optional[str]:
    """Strategy 2: initialRoomDossier from page HTML."""
    try:
        page_url = ROOM_URL.format(username=username)
        resp = await client.get(page_url)
        if resp.status_code != 200:
            return None

        data = _extract_dossier_json(resp.text)
        if not data:
            return None

        for key in ("hls_source", "hls_source_sd", "hls_source_hd"):
            hls = data.get(key)
            if hls:
                logger.info("Strategy 2 (dossier) found '%s' for %s", key, username)
                return hls

        return None

    except Exception as exc:
        logger.debug("Strategy 2 failed: %s", exc)
        return None


async def _strategy_edge_ajax(
    client: httpx.AsyncClient, username: str
) -> Optional[str]:
    """Strategy 3: get_edge_hls_url_ajax with CSRF token."""
    csrf_token = uuid.uuid4().hex.upper()[:32]
    headers = {
        **DEFAULT_HEADERS,
        "X-Requested-With": "XMLHttpRequest",
        "X-CSRFToken": csrf_token,
        "Referer": ROOM_URL.format(username=username),
        "Content-Type": "application/x-www-form-urlencoded",
    }
    cookies = {"csrftoken": csrf_token}
    data = urlencode({"room_slug": username, "bandwidth": "high"})

    try:
        resp = await client.post(
            EDGE_HLS_API,
            content=data,
            headers=headers,
            cookies=cookies,
        )
        if resp.status_code != 200:
            return None

        result = resp.json()
        url = result.get("url") or result.get("hls_source")
        if url and result.get("success"):
            logger.info("Strategy 3 (edge_ajax) got URL for %s", username)
            return url

        logger.info("Strategy 3: room_status=%s", result.get("room_status"))
        return None

    except Exception as exc:
        logger.debug("Strategy 3 failed: %s", exc)
        return None


async def _strategy_regex_html(
    client: httpx.AsyncClient, username: str
) -> Optional[str]:
    """Strategy 4: regex search on page HTML."""
    try:
        page_url = ROOM_URL.format(username=username)
        resp = await client.get(page_url)
        if resp.status_code != 200:
            return None

        html = resp.text
        url = _find_m3u8_in_html(html)
        if url:
            logger.info("Strategy 4 (regex) found URL for %s", username)
            return url

        if "is currently offline" in html.lower():
            logger.info("Room %s appears offline from HTML", username)
        return None

    except Exception as exc:
        logger.debug("Strategy 4 failed: %s", exc)
        return None


async def extract_hls_url(username: str) -> Optional[str]:
    """
    Extract a fresh HLS playlist URL for a Chaturbate room.

    IMPORTANT: Does NOT probe the URL. The token is single-use, so
    the first HTTP GET must be done by the downloader.

    Returns:
        Fresh m3u8 URL with token, or None if offline/not found.
    """
    username = username.strip().lower()
    logger.info("=== Extracting HLS URL for room: %s ===", username)

    async with httpx.AsyncClient(
        headers=DEFAULT_HEADERS,
        timeout=httpx.Timeout(20.0),
        follow_redirects=True,
    ) as client:
        strategies = [
            ("chatvideocontext", _strategy_chatvideocontext),
            ("dossier", _strategy_dossier),
            ("edge_ajax", _strategy_edge_ajax),
            ("regex_html", _strategy_regex_html),
        ]

        for name, strategy in strategies:
            try:
                url = await strategy(client, username)
                if url:
                    logger.info("✓ HLS URL found via %s for %s", name, username)
                    return url
            except Exception as exc:
                logger.warning("Strategy '%s' exception: %s", name, exc)

        logger.error("All extraction strategies failed for '%s'", username)
        return None
