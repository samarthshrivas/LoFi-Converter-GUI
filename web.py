import os
import base64
import streamlit as st
import uuid
import time
import threading
import logging
from typing import Optional
from streamlit.components.v1 import html

logger = logging.getLogger(__name__)


# Function to delete temporary audio files
def delete_temp_files(audio_file, output_file, mp3_file):
    if os.path.exists(audio_file):
        os.remove(audio_file)
    if os.path.exists(output_file):
        os.remove(output_file)
    if mp3_file and os.path.exists(mp3_file):
        os.remove(mp3_file)


# ---------------------------------------------------------------------------
# Free proxy manager – fetches & caches working HTTP proxies so we can
# bypass YouTube's cloud-IP bot detection / geo-restrictions.
#
# Proxy lists are fetched from public sources, tested in parallel, and
# cached for 10 minutes so subsequent downloads are fast.
# ---------------------------------------------------------------------------

_PROXY_CACHE: list[str] = []
_PROXY_CACHE_LOCK = threading.Lock()
_PROXY_CACHE_TIME = 0.0
_PROXY_CACHE_TTL = 600  # re-fetch every 10 minutes
_MAX_PROXY_TEST = 30
_PROXY_TEST_TIMEOUT = 5.0  # seconds per proxy

_PROXY_SOURCES = [
    "https://api.proxyscrape.com/v2/?request=displayproxies"
    "&protocol=http&timeout=10000&country=all&ssl=all&anonymity=all",
    "https://raw.githubusercontent.com/TheSpeedX/SOCKS-Proxy/master/http.txt",
    "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/http.txt",
    "https://raw.githubusercontent.com/jetkai/proxy-list/main/online/proxies/http.txt",
    "https://raw.githubusercontent.com/robertklep/dutch-proxy-list/main/http.txt",
    "https://www.proxy-list.download/api/v1/get?type=http",
]


def _fetch_proxy_list() -> list[str]:
    """Gather proxies from multiple public sources (best-effort)."""
    import requests as _req

    seen: set[str] = set()
    proxies: list[str] = []
    for src in _PROXY_SOURCES:
        try:
            resp = _req.get(src, timeout=10)
            if resp.status_code == 200:
                for line in resp.text.splitlines():
                    line = line.strip()
                    if not line or ":" not in line:
                        continue
                    if line not in seen:
                        seen.add(line)
                        proxies.append(line)
        except Exception:
            continue
    return proxies


def _test_proxy(proxy_addr: str) -> bool:
    """Quick connectivity check – does this proxy reach the internet?"""
    import requests as _req

    url = f"http://{proxy_addr}"
    try:
        r = _req.get(
            "https://www.google.com/generate_204",
            proxies={"http": url, "https": url},
            timeout=_PROXY_TEST_TIMEOUT,
        )
        return r.status_code == 204
    except Exception:
        return False


def _refresh_proxy_cache() -> list[str]:
    """Fetch fresh proxies and test them (parallel)."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    candidates = _fetch_proxy_list()
    if not candidates:
        return []

    # De-duplicate & shuffle
    seen: set[str] = set()
    unique = [p for p in candidates if not (p in seen or seen.add(p))]
    import random

    random.shuffle(unique)

    batch = unique[:_MAX_PROXY_TEST]
    working: list[str] = []

    # Test proxies in parallel threads
    with ThreadPoolExecutor(max_workers=20) as pool:
        fut_map = {pool.submit(_test_proxy, p): p for p in batch}
        for fut in as_completed(fut_map):
            if fut.result():
                working.append(fut_map[fut])

    return working


def get_working_proxies() -> list[str]:
    """Return cached list of working ``ip:port`` proxies.

    The list is refreshed in the background every ``_PROXY_CACHE_TTL``
    seconds.  While a refresh is in progress, the stale cache is still
    returned so callers never block.
    """
    global _PROXY_CACHE, _PROXY_CACHE_TIME

    now = time.time()
    with _PROXY_CACHE_LOCK:
        cache_age = now - _PROXY_CACHE_TIME
        if _PROXY_CACHE and cache_age < _PROXY_CACHE_TTL:
            return list(_PROXY_CACHE)
        # Stale cache still usable while we refresh
        stale = list(_PROXY_CACHE) if _PROXY_CACHE else []

    # Refresh asynchronously (don't block the caller)
    def _do_refresh():
        global _PROXY_CACHE, _PROXY_CACHE_TIME
        fresh = _refresh_proxy_cache()
        with _PROXY_CACHE_LOCK:
            if fresh:
                _PROXY_CACHE = fresh
            _PROXY_CACHE_TIME = time.time()
        logger.info("Proxy cache refreshed: %d working", len(fresh) if fresh else 0)

    t = threading.Thread(target=_do_refresh, daemon=True)
    t.start()

    return stale  # return stale list while refresh runs in background


def _reset_proxy():
    """Remove any globally-installed proxy so urllib goes direct again."""
    from urllib import request as _ur

    _ur.install_opener(_ur.build_opener())


# ---------------------------------------------------------------------------
# Patch pytubefix's request layer – handle decode errors gracefully so that
# non-UTF-8 responses (e.g. error pages) don't crash the app.
# ---------------------------------------------------------------------------
def _patch_pytubefix_http():
    """Monkey-patch pytubefix.request to not crash on non-UTF-8 responses."""
    import pytubefix.request as _req
    import requests as _requests

    _orig_get = _req.get
    _orig_post = _req.post

    def _safe_get(url, extra_headers=None, timeout=None):
        try:
            return _orig_get(url, extra_headers=extra_headers, timeout=timeout)
        except (UnicodeDecodeError, _requests.RequestException, Exception) as exc:
            raise Exception(f"GET failed: {exc}")

    def _safe_post(url, extra_headers=None, data=None, timeout=None):
        try:
            return _orig_post(
                url, extra_headers=extra_headers, data=data, timeout=timeout
            )
        except (UnicodeDecodeError, _requests.RequestException, Exception) as exc:
            raise Exception(f"POST failed: {exc}")

    _req.get = _safe_get
    _req.post = _safe_post


# Apply the patch once at module level
_patch_pytubefix_http()

# Warm the proxy cache in the background so it's ready for the first download
import threading as _thr

_thr.Thread(target=get_working_proxies, daemon=True).start()


# ---------------------------------------------------------------------------
# Direct YouTube audio download – makes the InnerTube API call ourselves via
# `requests` + proxy, avoiding pytubefix's SABR/PoToken issues on cloud IPs.
#
# We try MULTIPLE client configurations because different YouTube clients
# return different stream types.  Some clients return direct stream URLs
# even when others enforce SABR (which needs a PoToken we can't generate
# on Streamlit Cloud).
# ---------------------------------------------------------------------------

_YT_PLAYER_URL = "https://www.youtube.com/youtubei/v1/player"

# These client configs are extracted from pytubefix 10.10.1's
# `_default_clients` – only clients with `require_po_token=False`.
_CLIENT_CONFIGS = [
    {
        "name": "ANDROID_VR",
        "api_key": "AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8",
        "context": {
            "context": {
                "client": {
                    "clientName": "ANDROID_VR",
                    "clientVersion": "1.60.19",
                    "deviceMake": "Oculus",
                    "deviceModel": "Quest 3",
                    "osName": "Android",
                    "osVersion": "12L",
                    "androidSdkVersion": "32",
                }
            }
        },
        "headers": {
            "User-Agent": (
                "com.google.android.apps.youtube.vr.oculus/1.60.19 "
                "(Linux; U; Android 12L; eureka-user Build/SQ3A.220605.009.A1) gzip"
            ),
            "X-Youtube-Client-Name": "28",
            "Content-Type": "application/json",
        },
    },
    {
        "name": "ANDROID_MUSIC",
        "api_key": "AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8",
        "context": {
            "context": {
                "client": {
                    "clientName": "ANDROID_MUSIC",
                    "clientVersion": "7.27.52",
                    "androidSdkVersion": "30",
                    "osName": "Android",
                    "osVersion": "11",
                }
            }
        },
        "headers": {
            "User-Agent": (
                "com.google.android.apps.youtube.music/7.27.52 "
                "(Linux; U; Android 11) gzip"
            ),
            "X-Youtube-Client-Name": "21",
            "Content-Type": "application/json",
        },
    },
    {
        "name": "WEB_CREATOR",
        "api_key": "AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8",
        "context": {
            "context": {
                "client": {
                    "clientName": "WEB_CREATOR",
                    "clientVersion": "1.20220726.00.00",
                }
            }
        },
        "headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
            "X-Youtube-Client-Name": "62",
            "Content-Type": "application/json",
        },
    },
    {
        "name": "TV",
        "api_key": "AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8",
        "context": {
            "context": {
                "client": {
                    "clientName": "TVHTML5",
                    "clientVersion": "7.20240813.07.00",
                    "platform": "TV",
                }
            }
        },
        "headers": {
            "User-Agent": (
                "Mozilla/5.0 (ChromiumStyle; TV) AppleWebKit/537.36 (KHTML, like Gecko)"
            ),
            "X-Youtube-Client-Name": "7",
            "Content-Type": "application/json",
        },
    },
    {
        "name": "IOS",
        "api_key": "AIzaSyB-63vPrdThhKuerbB2N_l7Kwwcxj6yUAc",
        "context": {
            "context": {
                "client": {
                    "clientName": "IOS",
                    "clientVersion": "19.45.4",
                    "deviceMake": "Apple",
                    "deviceModel": "iPhone16,2",
                    "platform": "MOBILE",
                    "osName": "iPhone",
                    "osVersion": "18.1.0.22B83",
                }
            }
        },
        "headers": {
            "User-Agent": (
                "com.google.ios.youtube/19.45.4 "
                "(iPhone16,2; U; CPU iOS 18_1_0 like Mac OS X;)"
            ),
            "X-Youtube-Client-Name": "5",
            "Content-Type": "application/json",
        },
    },
    {
        "name": "ANDROID_TESTSUITE",
        "api_key": "AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8",
        "context": {
            "context": {
                "client": {
                    "clientName": "ANDROID_TESTSUITE",
                    "clientVersion": "1.9",
                    "platform": "MOBILE",
                    "osName": "Android",
                    "osVersion": "14",
                    "androidSdkVersion": "34",
                }
            }
        },
        "headers": {
            "User-Agent": (
                "com.google.android.youtube/19.29.39 (Linux; U; Android 14) gzip"
            ),
            "X-Youtube-Client-Name": "30",
            "X-Youtube-Client-Version": "1.9",
            "Content-Type": "application/json",
        },
    },
]

# pytubefix clients to try in the fallback (all have require_po_token=False)
_CLIENTS_FALLBACK = [
    "ANDROID_VR",
    "ANDROID_MUSIC",
    "WEB_CREATOR",
    "TV",
    "IOS",
    "ANDROID_TESTSUITE",
]


def _download_via_api(video_id: str, proxy_addr: Optional[str], uu: str):
    """Try to download *video_id* through the InnerTube API via *proxy*.

    Tries each client config from ``_CLIENT_CONFIGS`` in order.  Returns
    ``(file_path, audio_bytes, title)`` or ``None``.
    """
    import requests as _req

    proxies = None
    if proxy_addr:
        proxies = {"http": f"http://{proxy_addr}", "https": f"http://{proxy_addr}"}

    sess = _req.Session()
    if proxies:
        sess.proxies.update(proxies)

    for cfg in _CLIENT_CONFIGS:
        name = cfg["name"]

        # ── 1. Call the InnerTube player API ──────────────────────────
        body = dict(cfg["context"])
        body["videoId"] = video_id
        body["contentCheckOk"] = "true"

        try:
            resp = sess.post(
                _YT_PLAYER_URL,
                params={"prettyPrint": "false", "key": cfg["api_key"]},
                json=body,
                headers=cfg["headers"],
                timeout=15,
            )
            if resp.status_code != 200:
                logger.debug("API %s -> HTTP %s (skip)", name, resp.status_code)
                continue
            data = resp.json()
        except Exception as exc:
            logger.debug("API %s -> exception: %s", name, exc)
            continue

        playability = data.get("playabilityStatus", {})
        if playability.get("status") != "OK":
            logger.debug("API %s -> status=%s (skip)", name, playability.get("status"))
            continue

        video_details = data.get("videoDetails") or {}
        title = video_details.get("title", "Unknown Title")

        # ── 2. Find the best audio stream with a direct URL ──────────
        streaming_data = data.get("streamingData") or {}
        formats = streaming_data.get("adaptiveFormats") or []

        audio = [f for f in formats if f.get("mimeType", "").startswith("audio/")]
        if not audio:
            logger.debug("API %s -> no audio formats (skip)", name)
            continue

        audio.sort(key=lambda f: f.get("bitrate", 0), reverse=True)
        best = audio[0]
        stream_url = best.get("url")
        if not stream_url:
            # SABR-protected or ciphered – no direct URL, skip
            logger.debug("API %s -> no direct URL (SABR/cipher, skip)", name)
            continue

        mime = best.get("mimeType", "")
        ext = "m4a"
        if "opus" in mime:
            ext = "webm"
        elif "mp3" in mime:
            ext = "mp3"

        # ── 3. Download the audio stream ─────────────────────────────
        try:
            # Chunked download with a total timeout guard
            audio_resp = sess.get(stream_url, timeout=(10, 60), stream=True)
            if audio_resp.status_code != 200:
                logger.debug(
                    "API %s -> stream HTTP %s (skip)", name, audio_resp.status_code
                )
                continue
            # Read in chunks to avoid OOM on large files
            chunks = []
            for chunk in audio_resp.iter_content(
                chunk_size=2 * 1024 * 1024, decode_unicode=False
            ):
                if chunk:
                    chunks.append(chunk)
            audio_bytes = b"".join(chunks)
        except Exception as exc:
            logger.debug("API %s -> stream download exception: %s", name, exc)
            continue

        file_path = os.path.join("uploaded_files", f"{uu}.{ext}")
        with open(file_path, "wb") as f:
            f.write(audio_bytes)
        logger.info(
            "API %s -> download OK (%s, %d bytes)", name, title, len(audio_bytes)
        )
        return file_path, audio_bytes, title

    return None


# Download YouTube audio – multi-strategy
def download_youtube_audio(youtube_link):
    uu = str(uuid.uuid4())
    os.makedirs("uploaded_files", exist_ok=True)

    # ── Clean URL ─────────────────────────────────────────────────────
    import re as _re

    m = _re.search(r"(https?://www\.youtube\.com/watch\?v=[^&]+)", youtube_link)
    clean_url = m.group(1) if m else youtube_link
    video_id_match = _re.search(r"v=([a-zA-Z0-9_-]{11})", clean_url)
    video_id = video_id_match.group(1) if video_id_match else ""

    if not video_id:
        return None, ["Error: Could not extract video ID from URL"]

    # ── Gather working proxies (cached) ───────────────────────────────
    _proxies = get_working_proxies()
    proxy_chain = [None] + _proxies

    # ── Strategy A: direct InnerTube API call (no pytubefix) ──────────
    # Tries each client config × each proxy.  No PoToken needed.
    last_error = None
    for proxy_addr in proxy_chain:
        try:
            result = _download_via_api(video_id, proxy_addr, uu)
            if result is not None:
                return result
        except Exception as e:
            last_error = str(e)
            continue

    # ── Strategy B: pytubefix fallback ─────────────────────────────────
    # For each proxy, try every client with use_po_token=False first.
    # Only as a last resort try use_po_token=True (requires botGuard
    # Node.js, which won't work on Streamlit Cloud).
    for proxy_addr in proxy_chain:
        proxies_dict = None
        if proxy_addr:
            proxies_dict = {
                "http": f"http://{proxy_addr}",
                "https": f"http://{proxy_addr}",
            }

        # Try each client WITHOUT PoToken first
        for client_name in _CLIENTS_FALLBACK:
            try:
                from pytubefix import YouTube

                yt = YouTube(
                    clean_url,
                    use_oauth=False,
                    allow_oauth_cache=False,
                    client=client_name,
                    proxies=proxies_dict,
                    use_po_token=False,
                )
                song_name = yt.title or "Unknown Title"
                stream = yt.streams.get_audio_only()
                if not stream:
                    continue

                audio_file = stream.download(
                    output_path="uploaded_files",
                    filename=f"{uu}.{stream.subtype}",
                )
                with open(audio_file, "rb") as f:
                    audio_bytes = f.read()
                logger.info(
                    "pytubefix %s (no PoToken) -> OK (%s)", client_name, song_name
                )
                return (audio_file, audio_bytes, song_name)

            except Exception as e:
                last_error = f"{client_name} (no PoToken): {e}"
                logger.debug("pytubefix %s (no PoToken) -> %s", client_name, e)
                _reset_proxy()
                continue

    # ── Strategy C: pytubefix WITH PoToken (last resort) ──────────────
    # PoToken generation needs botGuard (Node.js subprocess) which ONLY
    # works on local machines, NOT on Streamlit Cloud.
    for proxy_addr in proxy_chain:
        proxies_dict = None
        if proxy_addr:
            proxies_dict = {
                "http": f"http://{proxy_addr}",
                "https": f"http://{proxy_addr}",
            }

        for client_name in _CLIENTS_FALLBACK:
            try:
                from pytubefix import YouTube

                yt = YouTube(
                    clean_url,
                    use_oauth=False,
                    allow_oauth_cache=False,
                    client=client_name,
                    proxies=proxies_dict,
                    use_po_token=True,
                )
                song_name = yt.title or "Unknown Title"
                stream = yt.streams.get_audio_only()
                if not stream:
                    continue

                audio_file = stream.download(
                    output_path="uploaded_files",
                    filename=f"{uu}.{stream.subtype}",
                )
                with open(audio_file, "rb") as f:
                    audio_bytes = f.read()
                logger.info(
                    "pytubefix %s (with PoToken) -> OK (%s)", client_name, song_name
                )
                return (audio_file, audio_bytes, song_name)

            except Exception as e:
                last_error = f"{client_name} (with PoToken): {e}"
                logger.debug("pytubefix %s (with PoToken) -> %s", client_name, e)
                _reset_proxy()
                continue

    return None, [f"Error: {last_error or 'All download strategies exhausted'}"]


# Client-side lofi processor using Web Audio API
def client_side_lofi_processor(
    audio_bytes: bytes, song_name: str, mime_type: str = "audio/mpeg"
) -> str:
    """Generate an HTML component that processes audio in-browser via Web Audio API."""
    audio_b64 = base64.b64encode(audio_bytes).decode()
    safe_name = "".join(c for c in song_name if c.isalnum() or c in "._- ").strip()

    html_content = (
        """
<div id="lofi-processor">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }

        body {
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: linear-gradient(160deg, #0f0f1a 0%, #1a1a2e 50%, #16213e 100%);
            color: #f0ede8;
            padding: 20px;
            min-height: 100%;
            overflow-y: auto;
            -webkit-overflow-scrolling: touch;
        }

        ::-webkit-scrollbar { width: 6px; }
        ::-webkit-scrollbar-track { background: transparent; }
        ::-webkit-scrollbar-thumb { background: rgba(240, 165, 0, 0.3); border-radius: 3px; }

        /* === Header === */
        .lp-header {
            display: flex;
            align-items: center;
            gap: 14px;
            margin-bottom: 20px;
            padding-bottom: 16px;
            border-bottom: 1px solid rgba(255,255,255,0.06);
        }
        .lp-header-icon {
            width: 44px; height: 44px;
            background: linear-gradient(135deg, #f59e0b, #f97316);
            border-radius: 12px;
            display: flex; align-items: center; justify-content: center;
            font-size: 22px;
            box-shadow: 0 4px 16px rgba(245, 158, 11, 0.3);
            flex-shrink: 0;
        }
        .lp-header-title {
            font-size: 20px; font-weight: 700; letter-spacing: -0.3px;
        }
        .lp-header-sub {
            font-size: 12px; color: rgba(255,255,255,0.45);
            margin-top: 2px;
        }

        /* === Cards === */
        .lp-card {
            background: rgba(255,255,255,0.04);
            border: 1px solid rgba(255,255,255,0.06);
            border-radius: 14px;
            padding: 16px;
            backdrop-filter: blur(12px);
            -webkit-backdrop-filter: blur(12px);
            transition: border-color 0.2s, box-shadow 0.2s;
        }
        .lp-card:hover {
            border-color: rgba(255,255,255,0.1);
            box-shadow: 0 8px 32px rgba(0,0,0,0.2);
        }
        .lp-card-label {
            font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.8px;
            color: rgba(255,255,255,0.35); margin-bottom: 10px;
        }

        /* === Custom Audio Player === */
        .cp {
            display: flex; align-items: center; gap: 12px;
            padding: 4px 0; width: 100%;
        }
        .cp-btn {
            width: 40px; height: 40px; border-radius: 50%;
            border: none; cursor: pointer; flex-shrink: 0;
            background: linear-gradient(135deg, #f59e0b, #d97706);
            color: #fff;
            display: flex; align-items: center; justify-content: center;
            box-shadow: 0 2px 8px rgba(245, 158, 11, 0.3);
            transition: transform 0.15s, box-shadow 0.15s;
        }
        .cp-btn:hover { transform: scale(1.08); box-shadow: 0 4px 16px rgba(245, 158, 11, 0.5); }
        .cp-btn:active { transform: scale(0.95); }
        .cp-btn svg { display: block; }

        .cp-bar {
            flex: 1; height: 5px; border-radius: 3px;
            background: rgba(255,255,255,0.08); cursor: pointer;
            position: relative; overflow: visible;
        }
        .cp-fill {
            height: 100%; width: 0%; border-radius: 3px;
            background: linear-gradient(to right, #f59e0b, #f97316);
            transition: width 0.1s linear;
            position: relative;
        }
        .cp-fill::after {
            content: ''; position: absolute; right: -5px; top: 50%;
            transform: translateY(-50%);
            width: 11px; height: 11px; border-radius: 50%;
            background: #f59e0b;
            border: 2px solid rgba(255,255,255,0.15);
            box-shadow: 0 2px 6px rgba(245, 158, 11, 0.4);
            opacity: 0; transition: opacity 0.15s;
        }
        .cp-bar:hover .cp-fill::after { opacity: 1; }

        .cp-time {
            font-size: 12px; font-variant-numeric: tabular-nums;
            color: rgba(255,255,255,0.45);
            min-width: 85px; text-align: right; flex-shrink: 0;
            letter-spacing: 0.2px;
        }
        .lp-source-card { margin-bottom: 18px; }

        /* === Collapsible Controls === */
        .lp-collapse { margin-bottom: 18px; }
        .lp-collapse-header {
            display: flex; align-items: center; justify-content: space-between;
            padding: 12px 16px;
            background: rgba(255,255,255,0.04);
            border: 1px solid rgba(255,255,255,0.06);
            border-radius: 12px;
            cursor: pointer; user-select: none;
            transition: background 0.2s, border-color 0.2s;
        }
        .lp-collapse-header:hover {
            background: rgba(255,255,255,0.07);
            border-color: rgba(255,255,255,0.1);
        }
        .lp-collapse-left {
            display: flex; align-items: center; gap: 10px;
        }
        .lp-collapse-title {
            font-size: 13px; font-weight: 600; color: rgba(255,255,255,0.7);
        }
        .lp-collapse-badge {
            font-size: 10px; color: rgba(255,255,255,0.35);
            background: rgba(255,255,255,0.06);
            padding: 2px 8px; border-radius: 5px;
        }
        .lp-chevron {
            transition: transform 0.3s ease;
            color: rgba(255,255,255,0.35);
        }
        .lp-chevron.open { transform: rotate(180deg); }
        .lp-collapse-body {
            max-height: 0; overflow: hidden;
            transition: max-height 0.35s ease;
        }
        .lp-collapse-body.open { max-height: 800px; }

        /* === Controls Grid === */
        .lp-controls {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
            gap: 12px;
            margin-bottom: 18px;
        }
        .lp-control-group {
            background: rgba(255,255,255,0.04);
            border: 1px solid rgba(255,255,255,0.06);
            border-radius: 12px;
            padding: 14px 16px;
            transition: border-color 0.2s, background 0.2s;
        }
        .lp-control-group:hover {
            border-color: rgba(245, 158, 11, 0.25);
            background: rgba(245, 158, 11, 0.03);
        }
        .lp-control-group label {
            display: flex; justify-content: space-between; align-items: center;
            font-size: 13px; font-weight: 500; color: rgba(255,255,255,0.7);
            margin-bottom: 8px;
        }
        .lp-control-group label .lp-value {
            font-weight: 600; color: #f59e0b;
            font-variant-numeric: tabular-nums;
            min-width: 48px; text-align: right;
        }

        /* === Custom Range Slider === */
        input[type=range] {
            -webkit-appearance: none; appearance: none;
            width: 100%; height: 5px;
            border-radius: 3px;
            outline: none; cursor: pointer;
            background: linear-gradient(to right, #f59e0b 0%, #f59e0b var(--fill, 50%), rgba(255,255,255,0.08) var(--fill, 50%), rgba(255,255,255,0.08) 100%);
            transition: background 0.15s;
        }
        input[type=range]::-webkit-slider-thumb {
            -webkit-appearance: none; appearance: none;
            width: 18px; height: 18px;
            border-radius: 50%;
            background: radial-gradient(circle at 35% 35%, #fbbf24, #f59e0b);
            cursor: pointer;
            border: 2px solid rgba(255,255,255,0.15);
            box-shadow: 0 2px 8px rgba(245, 158, 11, 0.35);
            transition: transform 0.15s, box-shadow 0.15s;
        }
        input[type=range]::-webkit-slider-thumb:hover {
            transform: scale(1.2);
            box-shadow: 0 4px 16px rgba(245, 158, 11, 0.5);
        }
        input[type=range]::-webkit-slider-thumb:active {
            transform: scale(0.95);
        }
        input[type=range]::-moz-range-track {
            height: 5px; border-radius: 3px;
            background: rgba(255,255,255,0.08);
        }
        input[type=range]::-moz-range-thumb {
            width: 18px; height: 18px; border-radius: 50%;
            background: linear-gradient(135deg, #fbbf24, #f59e0b);
            border: 2px solid rgba(255,255,255,0.15);
            cursor: pointer;
        }
        input[type=range]::-moz-range-progress {
            height: 5px; border-radius: 3px;
            background: #f59e0b;
        }

        /* === Action Buttons === */
        .lp-actions {
            display: flex; gap: 12px; flex-wrap: wrap;
            margin-bottom: 18px;
        }
        .lp-btn {
            flex: 1; min-width: 160px;
            display: inline-flex; align-items: center; justify-content: center; gap: 8px;
            padding: 13px 24px;
            font-size: 15px; font-weight: 600;
            border: none; border-radius: 12px;
            cursor: pointer; text-decoration: none;
            transition: all 0.2s ease;
            letter-spacing: 0.2px;
            position: relative; overflow: hidden;
        }
        .lp-btn::after {
            content: ''; position: absolute; inset: 0;
            background: linear-gradient(135deg, rgba(255,255,255,0.1), transparent);
            pointer-events: none;
        }
        .lp-btn:disabled {
            opacity: 0.5; cursor: not-allowed; transform: none !important;
        }
        .lp-btn-primary {
            background: linear-gradient(135deg, #f59e0b, #d97706);
            color: #fff;
            box-shadow: 0 4px 16px rgba(245, 158, 11, 0.3);
        }
        .lp-btn-primary:hover:not(:disabled) {
            transform: translateY(-2px);
            box-shadow: 0 8px 24px rgba(245, 158, 11, 0.4);
        }
        .lp-btn-primary:active:not(:disabled) { transform: translateY(0); }
        .lp-btn-secondary {
            background: rgba(255,255,255,0.08);
            color: #f0ede8;
            border: 1px solid rgba(255,255,255,0.1);
        }
        .lp-btn-secondary:hover:not(:disabled) {
            background: rgba(255,255,255,0.12);
            transform: translateY(-2px);
        }
        .lp-btn-secondary:active:not(:disabled) { transform: translateY(0); }
        .lp-btn-preview-active {
            background: rgba(239, 68, 68, 0.15) !important;
            border-color: rgba(239, 68, 68, 0.3) !important;
            color: #fca5a5 !important;
        }
        .lp-btn-download {
            background: linear-gradient(135deg, #10b981, #059669);
            color: #fff;
            box-shadow: 0 4px 16px rgba(16, 185, 129, 0.3);
            margin-top: 10px; width: 100%;
        }
        .lp-btn-download:hover:not(:disabled) {
            transform: translateY(-2px);
            box-shadow: 0 8px 24px rgba(16, 185, 129, 0.4);
        }
        .lp-download-row {
            display: flex; gap: 8px; margin-top: 10px;
        }
        .lp-download-row .lp-btn-download {
            flex: 1; margin-top: 0; min-width: 0;
        }
        .lp-btn-dl-mp3 {
            background: linear-gradient(135deg, #6366f1, #4f46e5) !important;
            box-shadow: 0 4px 16px rgba(99, 102, 241, 0.3) !important;
        }
        .lp-btn-dl-mp3:hover:not(:disabled) {
            box-shadow: 0 8px 24px rgba(99, 102, 241, 0.4) !important;
        }

        /* === Status === */
        .lp-status {
            display: none;
            padding: 14px 18px;
            border-radius: 12px;
            font-size: 14px; font-weight: 500;
            margin-bottom: 14px;
            animation: lpFadeIn 0.3s ease;
        }
        .lp-status.info {
            display: flex; align-items: center; gap: 10px;
            background: rgba(245, 158, 11, 0.1);
            border: 1px solid rgba(245, 158, 11, 0.2);
            color: #fbbf24;
        }
        .lp-status.success {
            display: block;
            background: rgba(16, 185, 129, 0.1);
            border: 1px solid rgba(16, 185, 129, 0.2);
            color: #34d399;
        }
        .lp-status.error {
            display: block;
            background: rgba(239, 68, 68, 0.1);
            border: 1px solid rgba(239, 68, 68, 0.2);
            color: #f87171;
        }
        .lp-spinner {
            display: inline-block; width: 18px; height: 18px;
            border: 2.5px solid rgba(245, 158, 11, 0.15);
            border-top-color: #f59e0b;
            border-radius: 50%;
            animation: lpSpin 0.7s linear infinite;
            flex-shrink: 0;
        }

        /* === Result Section === */
        .lp-result {
            display: none;
            background: rgba(255,255,255,0.04);
            border: 1px solid rgba(255,255,255,0.08);
            border-radius: 14px;
            padding: 18px;
            animation: lpFadeIn 0.4s ease;
        }
        .lp-result-header {
            font-size: 15px; font-weight: 600; color: #34d399;
            margin-bottom: 12px;
            display: flex; align-items: center; gap: 8px;
        }
        .lp-result audio {
            width: 100%; height: 44px;
            border-radius: 8px; margin-bottom: 8px;
        }

        /* === Animations === */
        @keyframes lpFadeIn {
            from { opacity: 0; transform: translateY(8px); }
            to { opacity: 1; transform: translateY(0); }
        }
        @keyframes lpSpin {
            to { transform: rotate(360deg); }
        }

        /* === Responsive === */
        @media (max-width: 640px) {
            body { padding: 12px; }
            .lp-header { margin-bottom: 12px; padding-bottom: 10px; gap: 10px; }
            .lp-header-icon { width: 36px; height: 36px; font-size: 18px; }
            .lp-header-title { font-size: 16px; }
            .lp-header-sub { font-size: 11px; }
            .lp-controls { grid-template-columns: 1fr 1fr; gap: 6px; }
            .lp-control-group { padding: 8px 10px; }
            .lp-control-group label { font-size: 12px; margin-bottom: 4px; }
            .lp-collapse { margin-bottom: 12px; }
            .lp-collapse-header { padding: 8px 12px; }
            .lp-collapse-title { font-size: 12px; }
            .lp-actions { gap: 8px; margin-bottom: 12px; }
            .lp-btn { min-width: 100%; padding: 10px 14px; font-size: 13px; }
            .lp-card { padding: 10px; }
            .lp-source-card { margin-bottom: 12px; }
            .lp-result { padding: 12px; }
            .lp-result-header { font-size: 13px; margin-bottom: 8px; }
            .lp-result audio { height: 38px; }
            .lp-status { padding: 10px 14px; font-size: 12px; margin-bottom: 10px; }
            .cp-btn { width: 32px; height: 32px; }
            .cp-btn svg { width: 14px; height: 14px; }
            .cp { gap: 8px; }
            .cp-time { font-size: 11px; min-width: 72px; }
            .cp-bar { height: 4px; }
            .cp-fill::after { width: 9px; height: 9px; right: -4px; }
        }
        @media (max-width: 420px) {
            body { padding: 8px; }
            .lp-header { margin-bottom: 10px; padding-bottom: 8px; gap: 8px; }
            .lp-header-icon { width: 30px; height: 30px; font-size: 15px; }
            .lp-header-title { font-size: 14px; }
            .lp-header-sub { font-size: 10px; }
            .lp-controls { grid-template-columns: 1fr; gap: 5px; }
            .lp-control-group { padding: 6px 8px; }
            .lp-control-group label { font-size: 11px; margin-bottom: 3px; }
            .lp-control-group label .lp-value { min-width: 36px; font-size: 11px; }
            .lp-collapse { margin-bottom: 10px; }
            .lp-collapse-header { padding: 6px 10px; }
            .lp-collapse-title { font-size: 11px; }
            .lp-collapse-badge { font-size: 9px; padding: 1px 6px; }
            .lp-actions { gap: 6px; margin-bottom: 10px; }
            .lp-btn { padding: 8px 12px; font-size: 12px; min-width: 0; }
            .lp-card { padding: 8px; }
            .lp-source-card { margin-bottom: 10px; }
            .lp-result { padding: 10px; }
            .lp-result-header { font-size: 12px; margin-bottom: 6px; }
            .lp-result audio { height: 34px; }
            .lp-btn-download { margin-top: 6px; padding: 8px 12px; font-size: 12px; }
            .lp-download-row { flex-direction: column; gap: 6px; }
            .lp-status { padding: 8px 10px; font-size: 11px; margin-bottom: 8px; }
            .cp-btn { width: 28px; height: 28px; }
            .cp-btn svg { width: 12px; height: 12px; }
            .cp { gap: 6px; }
            .cp-time { font-size: 10px; min-width: 64px; }
            .cp-bar { height: 3px; }
            .cp-fill::after { width: 8px; height: 8px; right: -4px; }
            input[type=range] { height: 4px; }
            input[type=range]::-webkit-slider-thumb { width: 14px; height: 14px; }
        }
    </style>

    <!-- Lamejs MP3 encoder (loaded from CDN) -->
    <script src="https://cdn.jsdelivr.net/npm/lamejs@1.2.1/lame.min.js"></script>

    <!-- Header -->
    <div class="lp-header">
        <div class="lp-header-icon">&#x1F3A4;</div>
        <div>
            <div class="lp-header-title">Lofi Processor</div>
            <div class="lp-header-sub">All effects run in your browser &mdash; nothing leaves your machine</div>
        </div>
    </div>

    <!-- Source Audio -->
    <div class="lp-card lp-source-card">
        <div class="lp-card-label">Source Audio</div>
        <audio id="source-audio" src="data:{MIME_TYPE};base64,{AUDIO_B64}" preload="metadata" style="display:none;"></audio>
        <div class="cp" id="cp-src">
            <button class="cp-btn" id="cp-btn-src" aria-label="Play">
                <svg viewBox="0 0 24 24" width="18" height="18"><polygon points="6,3 20,12 6,21" fill="currentColor"/></svg>
            </button>
            <div class="cp-bar" id="cp-bar-src">
                <div class="cp-fill" id="cp-fill-src"></div>
            </div>
            <span class="cp-time" id="cp-time-src">0:00 / 0:00</span>
        </div>
    </div>

    <!-- Controls -->
    <div class="lp-collapse">
        <div class="lp-collapse-header" onclick="toggleControls()">
            <div class="lp-collapse-left">
                <span class="lp-collapse-title">Effects Controls</span>
                <span class="lp-collapse-badge">7 effects</span>
            </div>
            <svg class="lp-chevron" viewBox="0 0 24 24" width="16" height="16">
                <polyline points="6,9 12,15 18,9" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
            </svg>
        </div>
        <div class="lp-collapse-body" id="lp-controls-body">
            <div class="lp-controls">
                <div class="lp-control-group">
                    <label>Low-Pass Filter <span class="lp-value" id="v-freq">1200</span> Hz</label>
                    <input type="range" id="lpf-freq" min="200" max="4000" value="1200" step="10">
                </div>
                <div class="lp-control-group">
                    <label>Reverb Mix <span class="lp-value" id="v-reverb">0.30</span></label>
                    <input type="range" id="reverb-mix" min="0" max="1.0" value="0.30" step="0.05">
                </div>
                <div class="lp-control-group">
                    <label>Reverb Decay <span class="lp-value" id="v-decay">2.0</span> s</label>
                    <input type="range" id="reverb-decay" min="0.5" max="5.0" value="2.0" step="0.1">
                </div>
                <div class="lp-control-group">
                    <label>Delay <span class="lp-value" id="v-delay">100</span> ms</label>
                    <input type="range" id="delay-time" min="0" max="500" value="100" step="10">
                </div>
                <div class="lp-control-group">
                    <label>Delay Feedback <span class="lp-value" id="v-fb">0.30</span></label>
                    <input type="range" id="delay-fb" min="0" max="0.9" value="0.30" step="0.05">
                </div>
                <div class="lp-control-group">
                    <label>Speed <span class="lp-value" id="v-speed">0.92</span>x</label>
                    <input type="range" id="play-rate" min="0.70" max="1.0" value="0.92" step="0.01">
                </div>
                <div class="lp-control-group">
                    <label>Wet/Dry Mix <span class="lp-value" id="v-wet">0.50</span></label>
                    <input type="range" id="wet-dry" min="0" max="1.0" value="0.50" step="0.05">
                </div>
            </div>
        </div>
    </div>

    <!-- Actions -->
    <div class="lp-actions">
        <button onclick="processLofi()" class="lp-btn lp-btn-primary" id="btn-convert">
            &#x1F504; Convert to Lofi
        </button>
        <button onclick="playPreview()" class="lp-btn lp-btn-secondary" id="btn-preview">
            &#x25B6;&#xFE0F; Quick Preview
        </button>
    </div>

    <!-- Status -->
    <div id="lp-status" class="lp-status"></div>

    <!-- Result -->
    <div id="lp-result" class="lp-result">
        <div class="lp-result-header">&#x2705; Processed Audio</div>
        <audio id="output-audio" style="display:none;"></audio>
        <div class="cp" id="cp-out" style="margin-bottom:8px;">
            <button class="cp-btn" id="cp-btn-out" aria-label="Play">
                <svg viewBox="0 0 24 24" width="18" height="18"><polygon points="6,3 20,12 6,21" fill="currentColor"/></svg>
            </button>
            <div class="cp-bar" id="cp-bar-out">
                <div class="cp-fill" id="cp-fill-out"></div>
            </div>
            <span class="cp-time" id="cp-time-out">0:00 / 0:00</span>
        </div>
        <div class="lp-download-row">
            <a id="download-wav" class="lp-btn lp-btn-download" download>&#x1F4E5; Download WAV</a>
            <a id="download-mp3" class="lp-btn lp-btn-download lp-btn-dl-mp3" download>&#x1F4E5; Download MP3</a>
        </div>
    </div>
</div>

<script>
(function() {
    var actx = null;

    // === Slider fill tracking ===
    document.querySelectorAll('input[type=range]').forEach(function(slider) {
        function updateFill() {
            var min = parseFloat(slider.min);
            var max = parseFloat(slider.max);
            var val = parseFloat(slider.value);
            var pct = ((val - min) / (max - min)) * 100;
            slider.style.setProperty('--fill', pct + '%');
        }
        slider.addEventListener('input', updateFill);
        updateFill();
    });

    // === Sync slider values to display labels ===
    function syncValue(sliderId, displayId, formatter) {
        var slider = document.getElementById(sliderId);
        var display = document.getElementById(displayId);
        if (slider && display) {
            function update() {
                display.textContent = formatter ? formatter(slider.value) : slider.value;
                var pct = ((slider.value - slider.min) / (slider.max - slider.min)) * 100;
                slider.style.setProperty('--fill', pct + '%');
            }
            slider.addEventListener('input', update);
            update();
        }
    }
    syncValue('lpf-freq', 'v-freq');
    syncValue('reverb-mix', 'v-reverb', function(v) { return parseFloat(v).toFixed(2); });
    syncValue('reverb-decay', 'v-decay');
    syncValue('delay-time', 'v-delay');
    syncValue('delay-fb', 'v-fb', function(v) { return parseFloat(v).toFixed(2); });
    syncValue('play-rate', 'v-speed');
    syncValue('wet-dry', 'v-wet', function(v) { return parseFloat(v).toFixed(2); });

    // === Custom Audio Players ===
    function setupPlayer(audio, btn, fill, time, bar) {
        var playSvg = '<svg viewBox="0 0 24 24" width="18" height="18"><polygon points="6,3 20,12 6,21" fill="currentColor"/></svg>';
        var pauseSvg = '<svg viewBox="0 0 24 24" width="18" height="18"><rect x="6" y="4" width="4" height="16" rx="1" fill="currentColor"/><rect x="14" y="4" width="4" height="16" rx="1" fill="currentColor"/></svg>';

        function fmt(t) {
            if (isNaN(t) || !isFinite(t)) return '0:00';
            var m = Math.floor(t / 60);
            var s = Math.floor(t % 60);
            return m + ':' + (s < 10 ? '0' : '') + s;
        }

        function toggle() {
            if (audio.paused) { audio.play().catch(function(){}); }
            else { audio.pause(); }
        }

        function refresh() {
            var pct = audio.duration ? (audio.currentTime / audio.duration) * 100 : 0;
            fill.style.width = pct + '%';
            time.textContent = fmt(audio.currentTime) + ' / ' + fmt(audio.duration);
            btn.innerHTML = audio.paused ? playSvg : pauseSvg;
        }

        btn.onclick = toggle;
        audio.addEventListener('play', refresh);
        audio.addEventListener('pause', refresh);
        audio.addEventListener('timeupdate', refresh);
        audio.addEventListener('loadedmetadata', refresh);
        audio.addEventListener('ended', function() {
            btn.innerHTML = playSvg;
            fill.style.width = '0%';
            time.textContent = '0:00 / ' + fmt(audio.duration);
        });

        bar.onclick = function(e) {
            var rect = bar.getBoundingClientRect();
            var pct = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
            audio.currentTime = pct * (audio.duration || 0);
        };

        if (audio.readyState >= 1) refresh();
    }

    function initPlayers() {
        var a = document.getElementById('source-audio');
        if (a) {
            setupPlayer(
                a,
                document.getElementById('cp-btn-src'),
                document.getElementById('cp-fill-src'),
                document.getElementById('cp-time-src'),
                document.getElementById('cp-bar-src')
            );
        }
        var b = document.getElementById('output-audio');
        if (b) {
            setupPlayer(
                b,
                document.getElementById('cp-btn-out'),
                document.getElementById('cp-fill-out'),
                document.getElementById('cp-time-out'),
                document.getElementById('cp-bar-out')
            );
        }
    }
    initPlayers();

    // === Toggle Controls ===
    window.toggleControls = function() {
        var body = document.getElementById('lp-controls-body');
        var chevron = document.querySelector('.lp-chevron');
        if (!body) return;
        var isOpen = body.classList.toggle('open');
        if (chevron) chevron.classList.toggle('open', isOpen);
    };

    // === Status helpers ===
    function showStatus(msg, type) {
        var s = document.getElementById('lp-status');
        if (!s) return;
        s.style.display = type === 'loading' ? 'flex' : 'block';
        s.className = 'lp-status';
        if (type === 'loading') {
            s.innerHTML = '<span class="lp-spinner"></span> ' + msg;
            s.classList.add('info');
        } else if (type === 'success') {
            s.textContent = msg;
            s.classList.add('success');
        } else {
            s.textContent = msg;
            s.classList.add('error');
        }
    }
    function hideStatus() {
        var s = document.getElementById('lp-status');
        if (s) { s.style.display = 'none'; s.className = 'lp-status'; }
    }

    // === Impulse Response ===
    function makeIR(ctx, dur, decay) {
        var len = ctx.sampleRate * dur;
        var buf = ctx.createBuffer(2, len, ctx.sampleRate);
        for (var ch = 0; ch < 2; ch++) {
            var d = buf.getChannelData(ch);
            for (var i = 0; i < len; i++) {
                d[i] = (Math.random() * 2 - 1) * Math.pow(1 - i / len, decay * 3);
            }
        }
        return buf;
    }

    // === WAV Export ===
    function bufToWav(buf) {
        var nc = buf.numberOfChannels, sr = buf.sampleRate;
        var chs = [];
        for (var c = 0; c < nc; c++) chs.push(buf.getChannelData(c));
        var len = chs[0].length, ds = len * nc * 2, bs = 44 + ds;
        var ab = new ArrayBuffer(bs), v = new DataView(ab);
        function wS(off, s) { for (var i = 0; i < s.length; i++) v.setUint8(off + i, s.charCodeAt(i)); }
        wS(0, 'RIFF'); v.setUint32(4, 36 + ds, true); wS(8, 'WAVE');
        wS(12, 'fmt '); v.setUint32(16, 16, true); v.setUint16(20, 1, true);
        v.setUint16(22, nc, true); v.setUint32(24, sr, true);
        v.setUint32(28, sr * nc * 2, true); v.setUint16(32, nc * 2, true); v.setUint16(34, 16, true);
        wS(36, 'data'); v.setUint32(40, ds, true);
        var off = 44;
        for (var i = 0; i < len; i++) {
            for (var c = 0; c < nc; c++) {
                var s = Math.max(-1, Math.min(1, chs[c][i]));
                v.setInt16(off, s < 0 ? s * 0x8000 : s * 0x7FFF, true);
                off += 2;
            }
        }
        return new Blob([ab], { type: 'audio/wav' });
    }

    // === MP3 Export (uses lamejs from CDN) ===
    function bufToMp3(buf) {
        var nc = buf.numberOfChannels;
        var sr = buf.sampleRate;
        function to16(f32) {
            var len = f32.length, i16 = new Int16Array(len);
            for (var i = 0; i < len; i++) {
                var s = Math.max(-1, Math.min(1, f32[i]));
                i16[i] = s < 0 ? s * 0x8000 : s * 0x7FFF;
            }
            return i16;
        }
        var enc = new lamejs.Mp3Encoder(nc, sr, 128);
        var data = [];
        var left = to16(buf.getChannelData(0));
        var right = nc > 1 ? to16(buf.getChannelData(1)) : null;
        var block = 1152;
        for (var i = 0; i < left.length; i += block) {
            var end = Math.min(i + block, left.length);
            var lc = left.subarray(i, end);
            var rc = right ? right.subarray(i, end) : null;
            var mp3 = rc ? enc.encodeBuffer(lc, rc) : enc.encodeBuffer(lc);
            if (mp3.length > 0) data.push(mp3);
        }
        var mp3 = enc.flush();
        if (mp3.length > 0) data.push(mp3);
        return new Blob(data, { type: 'audio/mpeg' });
    }

    // === Preview state ===
    var previewSource = null;

    function stopPreviewInternal() {
        var btn = document.getElementById('btn-preview');
        if (previewSource) {
            try {
                previewSource.stop();
                previewSource.disconnect();
            } catch(e) {}
            previewSource = null;
        }
        if (btn) {
            btn.innerHTML = '&#x25B6;&#xFE0F; Quick Preview';
            btn.classList.remove('lp-btn-preview-active');
        }
    }

    // === Process Lofi ===
    window.processLofi = async function() {
        // Stop preview if playing
        stopPreviewInternal();

        try {
            var resultSection = document.getElementById('lp-result');
            if (resultSection) resultSection.style.display = 'none';
            hideStatus();

            var freq = parseFloat(document.getElementById('lpf-freq').value);
            var revMix = parseFloat(document.getElementById('reverb-mix').value);
            var revDecay = parseFloat(document.getElementById('reverb-decay').value);
            var delayT = parseFloat(document.getElementById('delay-time').value) / 1000;
            var fb = parseFloat(document.getElementById('delay-fb').value);
            var rate = parseFloat(document.getElementById('play-rate').value);
            var wet = parseFloat(document.getElementById('wet-dry').value);

            var btn = document.getElementById('btn-convert');
            if (btn) btn.disabled = true;

            showStatus('Loading audio...', 'loading');
            var src = document.getElementById('source-audio');
            var r = await fetch(src.src);
            var ab = await r.arrayBuffer();
            if (!actx) actx = new (window.AudioContext || window.webkitAudioContext)();

            showStatus('Decoding audio...', 'loading');
            var buf = await actx.decodeAudioData(ab);
            var outLen = Math.ceil(buf.length / rate);

            showStatus('Applying effects...', 'loading');
            var offCtx = new OfflineAudioContext(buf.numberOfChannels, outLen, buf.sampleRate);

            var source = offCtx.createBufferSource();
            source.buffer = buf;
            source.playbackRate.value = rate;

            var dryGain = offCtx.createGain();
            dryGain.gain.value = 1 - wet;
            var wetGain = offCtx.createGain();
            wetGain.gain.value = wet;

            var filter = offCtx.createBiquadFilter();
            filter.type = 'lowpass';
            filter.frequency.value = freq;

            // Dry chain: source -> filter -> dryGain -> dest
            source.connect(filter);
            filter.connect(dryGain);
            dryGain.connect(offCtx.destination);

            // Wet chain: filter -> [reverb] -> [delay] -> wetGain -> dest
            var chain = filter;

            if (revMix > 0) {
                var conv = offCtx.createConvolver();
                conv.buffer = makeIR(offCtx, revDecay, 1.0);
                chain.connect(conv);
                var rg = offCtx.createGain();
                rg.gain.value = revMix;
                conv.connect(rg);
                chain = rg;
            }

            if (delayT > 0) {
                var d = offCtx.createDelay(1.0);
                d.delayTime.value = delayT;
                chain.connect(d);
                var dg = offCtx.createGain();
                dg.gain.value = 0.7;
                d.connect(dg);
                dg.connect(wetGain);
                if (fb > 0) {
                    var fg = offCtx.createGain();
                    fg.gain.value = fb;
                    dg.connect(fg);
                    fg.connect(d);
                }
            } else {
                chain.connect(wetGain);
            }

            wetGain.connect(offCtx.destination);
            source.start(0);

            showStatus('Rendering... (this may take a moment for longer tracks)', 'loading');
            var rendered = await offCtx.startRendering();

            showStatus('Creating download...', 'loading');
            var wavBlob = bufToWav(rendered);
            var url = URL.createObjectURL(wavBlob);

            var outAudio = document.getElementById('output-audio');
            if (outAudio) {
                outAudio.pause();
                outAudio.src = url;
                outAudio.load();
            }

            var dlWav = document.getElementById('download-wav');
            if (dlWav) {
                dlWav.href = url;
                dlWav.download = '{SAFE_NAME}_lofi.wav';
            }

            var dlMp3 = document.getElementById('download-mp3');
            if (dlMp3) {
                try {
                    if (typeof lamejs !== 'undefined' && lamejs) {
                        var mp3Blob = bufToMp3(rendered);
                        var mp3Url = URL.createObjectURL(mp3Blob);
                        dlMp3.href = mp3Url;
                        dlMp3.download = '{SAFE_NAME}_lofi.mp3';
                    } else {
                        dlMp3.style.display = 'none';
                    }
                } catch(e) {
                    console.warn('MP3 encoding failed:', e);
                    dlMp3.style.display = 'none';
                }
            }

            if (resultSection) resultSection.style.display = 'block';
            showStatus('Done! Your lo-fi track is ready.', 'success');

            // Scroll result into view on mobile
            setTimeout(function() {
                var target = resultSection || document.getElementById('lp-status');
                if (target) target.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
            }, 150);

            if (btn) btn.disabled = false;
        } catch (err) {
            console.error(err);
            showStatus('Error: ' + err.message, 'error');
            var btn = document.getElementById('btn-convert');
            if (btn) btn.disabled = false;
        }
    };

    // === Quick Preview (toggleable, full effects) ===
    window.playPreview = async function() {
        var btn = document.getElementById('btn-preview');

        if (previewSource) {
            stopPreviewInternal();
            return;
        }

        try {
            if (!actx) actx = new (window.AudioContext || window.webkitAudioContext)();
            if (actx.state === 'suspended') await actx.resume();

            // Read ALL current slider values
            var freq = parseFloat(document.getElementById('lpf-freq').value);
            var revMix = parseFloat(document.getElementById('reverb-mix').value);
            var revDecay = parseFloat(document.getElementById('reverb-decay').value);
            var delayT = parseFloat(document.getElementById('delay-time').value) / 1000;
            var fb = parseFloat(document.getElementById('delay-fb').value);
            var rate = parseFloat(document.getElementById('play-rate').value);
            var wet = parseFloat(document.getElementById('wet-dry').value);

            var r = await fetch(document.getElementById('source-audio').src);
            var ab = await r.arrayBuffer();
            var buf = await actx.decodeAudioData(ab);

            // Build full effects graph matching processLofi
            var src = actx.createBufferSource();
            src.buffer = buf;
            src.playbackRate.value = rate;

            var filter = actx.createBiquadFilter();
            filter.type = 'lowpass';
            filter.frequency.value = freq;

            var dryGain = actx.createGain();
            dryGain.gain.value = 1 - wet;
            var wetGain = actx.createGain();
            wetGain.gain.value = wet;

            // Dry chain: source -> filter -> dryGain -> dest
            src.connect(filter);
            filter.connect(dryGain);
            dryGain.connect(actx.destination);

            // Wet chain: filter -> [reverb] -> [delay] -> wetGain -> dest
            var chain = filter;

            if (revMix > 0) {
                var conv = actx.createConvolver();
                conv.buffer = makeIR(actx, revDecay, 1.0);
                chain.connect(conv);
                var rg = actx.createGain();
                rg.gain.value = revMix;
                conv.connect(rg);
                chain = rg;
            }

            if (delayT > 0) {
                var d = actx.createDelay(1.0);
                d.delayTime.value = delayT;
                chain.connect(d);
                var dg = actx.createGain();
                dg.gain.value = 0.7;
                d.connect(dg);
                dg.connect(wetGain);
                if (fb > 0) {
                    var fg = actx.createGain();
                    fg.gain.value = fb;
                    dg.connect(fg);
                    fg.connect(d);
                }
            } else {
                chain.connect(wetGain);
            }

            wetGain.connect(actx.destination);
            src.start(0);

            // Track and make toggleable
            previewSource = src;
            if (btn) {
                btn.innerHTML = '&#x23F9;&#xFE0F; Stop Preview';
                btn.classList.add('lp-btn-preview-active');
            }

            src.onended = function() {
                previewSource = null;
                if (btn) {
                    btn.innerHTML = '&#x25B6;&#xFE0F; Quick Preview';
                    btn.classList.remove('lp-btn-preview-active');
                }
            };
        } catch (e) {
            console.error(e);
            previewSource = null;
            if (btn) {
                btn.innerHTML = '&#x25B6;&#xFE0F; Quick Preview';
                btn.classList.remove('lp-btn-preview-active');
            }
        }
    };
})();
</script>
    """.replace("{AUDIO_B64}", audio_b64)
        .replace("{SAFE_NAME}", safe_name)
        .replace("{MIME_TYPE}", mime_type)
    )
    return html_content


# Main function for the web app
def main():
    st.set_page_config(
        page_title="Lofi Converter",
        page_icon="\U0001f3a4",
        layout="wide",
        initial_sidebar_state="collapsed",
    )

    # Custom CSS for Streamlit page
    st.markdown(
        """
    <style>
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');

        #root, .stApp {
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
        }

        /* === Header === */
        .main-header {
            display: flex; align-items: center; gap: 16px;
            padding: 28px 0 8px 0; margin-bottom: 4px;
        }
        .main-header-icon {
            width: 52px; height: 52px;
            background: linear-gradient(135deg, #f59e0b, #f97316);
            border-radius: 16px;
            display: flex; align-items: center; justify-content: center;
            font-size: 26px;
            box-shadow: 0 4px 20px rgba(245, 158, 11, 0.3);
            flex-shrink: 0;
        }
        .main-header-text h1 {
            font-size: 28px; font-weight: 800;
            letter-spacing: -0.5px; margin: 0; padding: 0;
            color: #1a1a2e; line-height: 1.2;
        }
        .main-header-text p {
            font-size: 14px; color: #6b7280;
            margin: 4px 0 0 0; font-weight: 400;
        }

        /* === Cards === */
        .section-card {
            background: #ffffff;
            border: 1px solid #e5e7eb;
            border-radius: 16px;
            padding: 24px;
            margin-bottom: 20px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.04);
            transition: box-shadow 0.2s;
        }
        .section-card:hover {
            box-shadow: 0 4px 16px rgba(0,0,0,0.06);
        }
        .section-card h3 {
            font-size: 16px; font-weight: 600; color: #374151;
            margin: 0 0 16px 0;
            display: flex; align-items: center; gap: 8px;
        }

        /* === Inputs === */
        .stTextInput input {
            border-radius: 10px !important;
            border: 1px solid #e5e7eb !important;
            padding: 12px 16px !important;
            font-size: 15px !important;
            transition: border-color 0.2s, box-shadow 0.2s !important;
        }
        .stTextInput input:focus {
            border-color: #f59e0b !important;
            box-shadow: 0 0 0 3px rgba(245, 158, 11, 0.1) !important;
        }
        .stFileUploader > div {
            border-radius: 10px !important;
            border: 1px dashed #d1d5db !important;
            padding: 8px !important;
            transition: border-color 0.2s;
        }
        .stFileUploader:hover > div {
            border-color: #f59e0b !important;
        }

        /* === Audio display === */
        .audio-card {
            background: #f9fafb;
            border: 1px solid #e5e7eb;
            border-radius: 12px;
            padding: 16px;
            margin: 0 0 12px 0;
        }
        .audio-card .song-name {
            font-size: 14px; font-weight: 600; color: #374151;
            margin-bottom: 10px;
            overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
        }
        .audio-card audio { width: 100%; border-radius: 8px; }

        /* === Status badges === */
        .info-badge {
            display: inline-flex; align-items: center; gap: 8px;
            background: #fffbeb; border: 1px solid #fde68a;
            border-radius: 10px; padding: 10px 16px;
            font-size: 13px; color: #92400e; margin: 4px 0 12px 0;
            line-height: 1.4;
        }

        /* === Divider === */
        .custom-divider {
            height: 1px;
            background: linear-gradient(to right, transparent, #e5e7eb, transparent);
            margin: 24px 0;
        }

        /* === Footer === */
        .footer {
            text-align: center; padding: 32px 0 16px;
            font-size: 13px; color: #9ca3af;
        }
        .footer a {
            color: #f59e0b; text-decoration: none; font-weight: 500;
            transition: color 0.2s;
        }
        .footer a:hover { color: #d97706; text-decoration: underline; }

        /* === Dark mode support === */
        @media (prefers-color-scheme: dark) {
            .main-header-text h1 { color: #f5f4f0; }
            .main-header-text p { color: #9e9bb0; }
            .section-card {
                background: #1e1e30;
                border-color: rgba(255,255,255,0.08);
                box-shadow: 0 1px 3px rgba(0,0,0,0.2);
            }
            .section-card:hover { box-shadow: 0 4px 16px rgba(0,0,0,0.3); }
            .section-card h3 { color: #f0ede8; }
            .stTextInput input {
                background: #1e1e30 !important;
                border-color: rgba(255,255,255,0.1) !important;
                color: #f0ede8 !important;
            }
            .stTextInput label { color: #d1d0cc !important; }
            .audio-card {
                background: rgba(255,255,255,0.04);
                border-color: rgba(255,255,255,0.08);
            }
            .audio-card .song-name { color: #f0ede8; }
            .info-badge {
                background: rgba(245, 158, 11, 0.1);
                border-color: rgba(245, 158, 11, 0.2);
                color: #fbbf24;
            }
            .stFileUploader > div {
                border-color: rgba(255,255,255,0.15) !important;
            }
        }

        /* === Responsive === */
        @media (max-width: 768px) {
            .main-header { padding: 16px 0 8px; }
            .main-header-icon { width: 42px; height: 42px; font-size: 20px; }
            .main-header-text h1 { font-size: 22px; }
            .section-card { padding: 16px; }
        }
    </style>
    """,
        unsafe_allow_html=True,
    )

    # === Header ===
    st.markdown(
        """
    <div class="main-header">
        <div class="main-header-icon">\U0001f3a4</div>
        <div class="main-header-text">
            <h1>Lofi Converter</h1>
            <p>Turn any audio into lo-fi \u2014 processed entirely in your browser</p>
        </div>
    </div>
    """,
        unsafe_allow_html=True,
    )

    # === Session state ===
    if "downloaded_data" not in st.session_state:
        st.session_state.downloaded_data = None

    # === Input Section ===
    st.markdown("<h3>\U0001f4e4 Import Audio</h3>", unsafe_allow_html=True)

    col1, col2 = st.columns(2)
    with col1:
        youtube_link = st.text_input(
            "YouTube URL",
            placeholder="https://youtube.com/watch?v=...",
            label_visibility="collapsed",
        )
    with col2:
        uploaded_file = st.file_uploader(
            "Upload file",
            type=["mp3", "wav", "m4a", "flac", "ogg"],
            label_visibility="collapsed",
        )

    st.markdown(
        '<div style="font-size:12px;color:#9ca3af;margin-top:6px;">'
        "Paste a YouTube link or upload an audio file to get started</div>",
        unsafe_allow_html=True,
    )
    st.markdown("</div>", unsafe_allow_html=True)

    # === Process YouTube Link ===
    if youtube_link:
        if (
            st.session_state.downloaded_data is None
            or st.session_state.downloaded_data[3] != youtube_link
        ):
            with st.spinner("Downloading audio..."):
                d = download_youtube_audio(youtube_link)
                if isinstance(d, tuple) and len(d) == 3:
                    st.session_state.downloaded_data = d + (youtube_link,)
                else:
                    if d and len(d) == 2:
                        err_msg = d[1][0] if isinstance(d[1], list) else str(d[1])
                        st.error(f"\u26a0\ufe0f Download failed")

                        # Show proxy info if available
                        _n = len(get_working_proxies())
                        _proxy_hint = (
                            f" ({_n} proxies available)"
                            if _n
                            else " (no proxies available)"
                        )

                        st.info(
                            f"This can happen when YouTube blocks cloud IPs."
                            f"{_proxy_hint}"
                            f"\n\nTry a different video or upload a file instead."
                            f"\n\n{err_msg}",
                            icon="\u2139\ufe0f",
                        )

    # === Process Uploaded File ===
    if uploaded_file:
        if (
            st.session_state.downloaded_data is None
            or st.session_state.downloaded_data[3] != uploaded_file.name
        ):
            with st.spinner("Processing uploaded audio..."):
                uu = str(uuid.uuid4())
                if not os.path.exists("uploaded_files"):
                    os.makedirs("uploaded_files")

                file_ext = os.path.splitext(uploaded_file.name)[1]
                audio_file = f"uploaded_files/{uu}{file_ext}"

                with open(audio_file, "wb") as f:
                    f.write(uploaded_file.getbuffer())

                with open(audio_file, "rb") as f:
                    audio_bytes = f.read()

                st.session_state.downloaded_data = (
                    audio_file,
                    audio_bytes,
                    uploaded_file.name,
                    uploaded_file.name,
                )

    # === Audio Display & Processor ===
    if st.session_state.downloaded_data:
        audio_file, audio_bytes, song_name, _ = st.session_state.downloaded_data

        ext = os.path.splitext(audio_file)[1].lower()
        mime_map = {
            ".m4a": "audio/mp4",
            ".mp4": "audio/mp4",
            ".webm": "audio/webm",
            ".mp3": "audio/mpeg",
            ".wav": "audio/wav",
            ".flac": "audio/flac",
            ".ogg": "audio/ogg",
        }
        mime = mime_map.get(ext, "audio/mpeg")

        processor_html = client_side_lofi_processor(
            audio_bytes, song_name or "audio", mime
        )
        html(processor_html, height=800)

        st.markdown("</div>", unsafe_allow_html=True)

    # === Footer ===
    st.markdown(
        '<div class="footer">'
        'Give a \u2b50 on <a href="https://github.com/samarthshrivas/LoFi-Converter-GUI">GitHub</a>'
        "</div>",
        unsafe_allow_html=True,
    )


if __name__ == "__main__":
    main()
