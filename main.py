"""
Qobuz Local API Server
Exposes Qobuz functionalities on localhost:8000
Compatible with programs like Spotiflac
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import time
import uvicorn
from contextlib import asynccontextmanager
from typing import Literal

import httpx
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

load_dotenv()

# ─── Logging ───────────────────────────────────────────────────────────────
_log_level = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, _log_level, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("qobuz-api")

# ─── Configuration ─────────────────────────────────────────────────────────
QOBUZ_BASE = "https://www.qobuz.com/api.json/0.2"
APP_ID = os.getenv("QOBUZ_APP_ID", "").strip("'\"")
SECRET = os.getenv("QOBUZ_SECRET", "").strip("'\"")
TOKEN  = os.getenv("QOBUZ_TOKEN",  "").strip("'\"")

# [IMPROVEMENT 5] DEV_MODE: logs status, headers and body of every upstream response.
# Enable with DEV_MODE=true in .env — never enable in production.
DEV_MODE = os.getenv("DEV_MODE", "False").lower() in ("true", "1", "yes")

# Quality IDs
QUALITY_MAP = {
    "mp3":   5,   # MP3 320kbps
    "flac":  6,   # FLAC 16-bit (CD quality)
    "hi24":  7,   # FLAC 24-bit ≤96kHz
    "hi96": 27,   # FLAC 24-bit >96kHz (Hi-Res)
}

# [IMPROVEMENT 1] Rate-limit retry settings
_RATE_LIMIT_MAX_RETRIES = 3
_RATE_LIMIT_BASE_DELAY  = 1.0   # seconds, doubles each attempt
_RATE_LIMIT_MAX_DELAY   = 15.0  # hard cap

# Semaphore: max 3 concurrent downloads at any time
DOWNLOAD_SEM = asyncio.Semaphore(3)

# Shared HTTP client — initialised in lifespan, reused across all requests
http_client: httpx.AsyncClient | None = None


# ─── Helpers ───────────────────────────────────────────────────────────────
def sanitize_filename(name: str) -> str:
    """
    Removes characters that are illegal in file names on Windows, macOS, and Linux.
    Replaces them with '-', then collapses duplicate separators.
    """
    name = re.sub(r'[\\/*?:"<>|]', "-", name)
    name = re.sub(r"-{2,}", "-", name)
    name = re.sub(r" {2,}", " ", name)
    return name.strip(" -")


# ─── Status file helpers ───────────────────────────────────────────────────
_STATUS_FILE = "status.json"

def _read_status(album_dir: str) -> dict:
    path = os.path.join(album_dir, _STATUS_FILE)
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def _write_status(album_dir: str, status: dict) -> None:
    """Writes status atomically (rename trick) to avoid partial reads."""
    path = os.path.join(album_dir, _STATUS_FILE)
    tmp  = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(status, f, indent=2)
    os.replace(tmp, path)

# ─── [IMPROVEMENT 5] DEV_MODE upstream logger ─────────────────────────────
def _log_response(method: str, url: str, resp: httpx.Response) -> None:
    """Log upstream response details when DEV_MODE is enabled."""
    if not DEV_MODE:
        return
    logger.debug(
        "[DEV] %s %s → %s\n  headers: %s\n  body: %s",
        method,
        url,
        resp.status_code,
        dict(resp.headers),
        resp.text[:2000],
    )

@asynccontextmanager
async def lifespan(app: FastAPI):
    global http_client
    if DEV_MODE:
        logger.warning("DEV_MODE is enabled — upstream responses will be logged at DEBUG level")
    http_client = httpx.AsyncClient(
        http2=True,
        timeout=httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=30.0),
        limits=httpx.Limits(
            max_keepalive_connections=100,
            max_connections=200,
            keepalive_expiry=30.0,
        ),
    )
    yield
    await http_client.aclose()

# ─── App ───────────────────────────────────────────────────────────────────
app = FastAPI(
    title="Qobuz Local API",
    description="Local API to download music from Qobuz",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─── Authentication ────────────────────────────────────────────────────────
async def get_token() -> str:
    if not TOKEN:
        raise HTTPException(400, "No token configured in .env — set QOBUZ_TOKEN to your user auth token")
    return TOKEN

def make_sig(track_id: str, format_id: int) -> tuple[str, str]:
    """Generates the MD5 signature required by track/getFileUrl."""
    ts  = str(int(time.time()))
    raw = f"trackgetFileUrlformat_id{format_id}intentstreamtrack_id{track_id}{ts}{SECRET}"
    sig = hashlib.md5(raw.encode()).hexdigest()
    return sig, ts


# ─── [IMPROVEMENT 1 + 3 + 4] Core request helper ──────────────────────────
async def qobuz_get(endpoint: str, params: dict) -> dict:
    """
    Authenticated GET against the Qobuz API with:
      - Retry + exponential backoff on 429 (rate limit)         [improvement 1]
      - Explicit 401 error message                              [improvement 4]
      - Structured network-error handling (timeout vs other)    [improvement 3]
      - DEV_MODE response logging                               [improvement 5]
    """
    if not APP_ID or not SECRET:
        raise HTTPException(
            500,
            "App ID and Secret unavailable. Configure .env or check connection to Qobuz.",
        )
    token = await get_token()
    params = {**params, "app_id": APP_ID, "user_auth_token": token}
    url = f"{QOBUZ_BASE}/{endpoint}"

    # [IMPROVEMENT 3] Wrap everything in a single network-error handler
    try:
        # [IMPROVEMENT 1] Retry loop with exponential backoff on 429
        for attempt in range(_RATE_LIMIT_MAX_RETRIES + 1):
            r = await http_client.get(url, params=params)
            _log_response("GET", url, r)   # [IMPROVEMENT 5]

            # [IMPROVEMENT 4] Explicit 401 handling
            if r.status_code == 401:
                raise HTTPException(
                    401,
                    "Token Qobuz not valid or expired. "
                    "Update QOBUZ_TOKEN in .env by fetching it from localStorage on play.qobuz.com.",
                )  

            # [IMPROVEMENT 1] Rate-limit backoff
            if r.status_code == 429 and attempt < _RATE_LIMIT_MAX_RETRIES:
                delay = min(_RATE_LIMIT_BASE_DELAY * (2 ** attempt), _RATE_LIMIT_MAX_DELAY)
                retry_after = r.headers.get("Retry-After")
                if retry_after:
                    try:
                        delay = min(float(retry_after), _RATE_LIMIT_MAX_DELAY)
                    except ValueError:
                        pass
                logger.warning(
                    "Qobuz rate-limited (429) on %s — retrying in %.1fs (attempt %d/%d)",
                    endpoint, delay, attempt + 1, _RATE_LIMIT_MAX_RETRIES,
                )
                await asyncio.sleep(delay)
                continue

            # All other non-2xx responses
            if r.status_code != 200:
                data = {}
                try:
                    data = r.json()
                except Exception:
                    pass
                raise HTTPException(r.status_code, data.get("message", "Qobuz API error"))

            return r.json()

        # Exhausted retries on 429
        raise HTTPException(429, "Qobuz rate limit exceeded — all retry attempts exhausted")

    # [IMPROVEMENT 3] Distinguish timeout from general network errors
    except HTTPException:
        raise
    except httpx.TimeoutException as e:
        logger.error("Timeout calling Qobuz endpoint %s: %s", endpoint, e)
        raise HTTPException(504, "Qobuz API timed out — try again shortly")
    except httpx.RequestError as e:
        logger.error("Network error calling Qobuz endpoint %s: %s", endpoint, e)
        raise HTTPException(503, f"Connection error to Qobuz: {e}")


# ─── Models ────────────────────────────────────────────────────────────────
class DownloadRequest(BaseModel):
    track_id:   str
    quality:    Literal["mp3", "flac", "hi24", "hi96"] = "flac"
    output_dir: str = "./downloads"


# ─── Endpoint: Health ──────────────────────────────────────────────────────
@app.get("/", tags=["info"])
async def root():
    return {
        "status":    "online",
        "version":   "1.0.0",
        "docs":      "http://localhost:8000/docs",
        "endpoints": [
            "/search", "/track/{id}", "/album/{id}",
            "/artist/{id}", "/download-url/{track_id}",
            "/stream/{track_id}", "/download", "/download-album/{album_id}",
            "/album-status/{album_id}",
        ],
    }


# ─── Endpoint: Search ──────────────────────────────────────────────────────
@app.get("/search", tags=["search"])
async def search(
    q:     str = Query(..., description="Text to search"),
    type:  str = Query("tracks", description="tracks | albums | artists"),
    limit: int = Query(10, ge=1, le=50),
):
    """Searches for tracks, albums, or artists on Qobuz."""
    return await qobuz_get("catalog/search", {"query": q, "type": type, "limit": limit})


# ─── Endpoint: Track Info ──────────────────────────────────────────────────
@app.get("/track/{track_id}", tags=["metadata"])
async def get_track(track_id: str):
    """Returns complete metadata for a track."""
    return await qobuz_get("track/get", {"track_id": track_id})


# ─── Endpoint: Album Info ──────────────────────────────────────────────────
@app.get("/album/{album_id}", tags=["metadata"])
async def get_album(album_id: str):
    """Returns album metadata and the tracklist."""
    return await qobuz_get("album/get", {"album_id": album_id})


# ─── Endpoint: Artist Info ─────────────────────────────────────────────────
@app.get("/artist/{artist_id}", tags=["metadata"])
async def get_artist(
    artist_id: str,
    limit:     int = Query(20, ge=1, le=100),
):
    """Returns artist data along with their albums."""
    return await qobuz_get("artist/get", {
        "artist_id": artist_id,
        "extra":     "albums",
        "limit":     limit,
    })


# ─── Endpoint: Download URL ────────────────────────────────────────────────
@app.get("/download-url/{track_id}", tags=["download"])
async def get_download_url(
    track_id: str,
    quality:  Literal["mp3", "flac", "hi24", "hi96"] = Query("flac"),
):
    """Returns the signed URL to download a track."""
    if not APP_ID or not SECRET:
        raise HTTPException(500, "App ID and Secret unavailable to generate the signature.")

    format_id = QUALITY_MAP[quality]
    sig, ts   = make_sig(track_id, format_id)

    data = await qobuz_get("track/getFileUrl", {
        "track_id":    track_id,
        "format_id":   format_id,
        "intent":      "stream",
        "request_ts":  ts,
        "request_sig": sig,
    })

    if "url" not in data:
        raise HTTPException(
            403,
            data.get("message", "URL not available (unsupported quality or insufficient subscription)"),
        )

    return {
        "track_id":      track_id,
        "quality":       quality,
        "format_id":     format_id,
        "url":           data["url"],
        "mime_type":     data.get("mime_type"),
        "bit_depth":     data.get("bit_depth"),
        "sampling_rate": data.get("sampling_rate"),
        "file_size":     data.get("file_size"),
    }


# ─── Endpoint: Direct Stream ───────────────────────────────────────────────
@app.get("/stream/{track_id}", tags=["download"])
async def stream_track(
    track_id: str,
    quality:  Literal["mp3", "flac", "hi24", "hi96"] = Query("flac"),
):
    """Streams the audio file directly through this API."""
    url_data = await get_download_url(track_id, quality)
    url      = url_data["url"]

    # [IMPROVEMENT 3] Network errors during streaming are handled explicitly
    async def generate():
        try:
            async with http_client.stream("GET", url, timeout=None) as r:
                async for chunk in r.aiter_bytes(chunk_size=65536):
                    yield chunk
        except httpx.TimeoutException:
            logger.error("Timeout while streaming track %s", track_id)
        except httpx.RequestError as e:
            logger.error("Network error while streaming track %s: %s", track_id, e)

    mime = url_data.get("mime_type") or (
        "audio/mpeg" if quality == "mp3" else "audio/flac"
    )
    return StreamingResponse(generate(), media_type=mime)


# ─── Endpoint: Download to Disk ────────────────────────────────────────────
@app.post("/download", tags=["download"])
async def download_track(req: DownloadRequest, background_tasks: BackgroundTasks):
    """Downloads a single track to disk in the specified path."""
    url_data   = await get_download_url(req.track_id, req.quality)   # ← fuori dal background
    track_info = await get_track(req.track_id)

    artist   = track_info.get("performer", {}).get("name", "Unknown Artist")
    title    = track_info.get("title", req.track_id)
    ext      = "mp3" if req.quality == "mp3" else "flac"
    fname    = sanitize_filename(f"{artist} - {title}") + f".{ext}"

    os.makedirs(req.output_dir, exist_ok=True)
    out_path = os.path.join(req.output_dir, fname)

    async def do_download():
        async with DOWNLOAD_SEM:
            logger.info(f"[{req.track_id}] Starting: {fname}")
            try:
                async with http_client.stream("GET", url_data["url"], timeout=None) as r:
                    with open(out_path, "wb") as f:
                        async for chunk in r.aiter_bytes(65536):
                            f.write(chunk)
                logger.info(f"[{req.track_id}] Done: {fname}")
            except httpx.TimeoutException:
                logger.error(f"[{req.track_id}] Timeout while downloading {fname}")
            except httpx.RequestError as e:
                logger.error(f"[{req.track_id}] Network error while downloading {fname}: {e}")
            except Exception as e:
                logger.error(f"[{req.track_id}] Failed: {e}")

    background_tasks.add_task(do_download)

    return {
        "status":   "downloading",
        "track_id": req.track_id,
        "filename": fname,
        "output":   out_path,
        "quality":  req.quality,
        "url":      url_data["url"],
    }


# ─── Endpoint: Full Album Download ─────────────────────────────────────────
@app.post("/download-album/{album_id}", tags=["download"])
async def download_album(
    album_id:         str,
    background_tasks: BackgroundTasks,
    quality:          Literal["mp3", "flac", "hi24", "hi96"] = Query("flac"),
    output_dir:       str = Query("./downloads"),
):
    """Downloads all tracks from an album to disk (max 3 concurrent)."""
    album  = await get_album(album_id)
    tracks = album.get("tracks", {}).get("items", [])
    if not tracks:
        raise HTTPException(404, "No tracks found in the album")

    artist_name = album.get("artist", {}).get("name", "Unknown")
    album_title = album.get("title", album_id)
    album_dir   = os.path.join(
        output_dir,
        sanitize_filename(f"{artist_name} - {album_title}"),
    )
    os.makedirs(album_dir, exist_ok=True)

    status = {
        str(t["id"]): {"title": t.get("title", ""), "status": "pending"}
        for t in tracks
    }
    _write_status(album_dir, status)
    logger.info(f"Album download queued: {artist_name} — {album_title} ({len(tracks)} tracks)")

    for track in tracks:
        background_tasks.add_task(
            _download_single,
            DownloadRequest(track_id=str(track["id"]), quality=quality, output_dir=album_dir),
            album_dir,
        )

    return {
        "status":     "downloading",
        "album":      album_title,
        "artist":     artist_name,
        "tracks":     len(tracks),
        "output_dir": album_dir,
        "quality":    quality,
    }


# ─── Endpoint: Album Download Status ──────────────────────────────────────
@app.get("/album-status/{album_id}", tags=["download"])
async def album_status(
    album_id:   str,
    output_dir: str = Query("./downloads"),
):
    """Returns the download progress for an album (reads status.json from disk)."""
    album     = await get_album(album_id)
    artist    = album.get("artist", {}).get("name", "Unknown")
    title     = album.get("title", album_id)
    album_dir = os.path.join(output_dir, sanitize_filename(f"{artist} - {title}"))

    status = _read_status(album_dir)
    if not status:
        raise HTTPException(404, "No status file found — has the download been started?")

    done    = sum(1 for t in status.values() if t["status"] == "done")
    errors  = sum(1 for t in status.values() if t["status"] == "error")
    pending = sum(1 for t in status.values() if t["status"] == "pending")

    return {
        "album":   title,
        "artist":  artist,
        "done":    done,
        "pending": pending,
        "errors":  errors,
        "tracks":  status,
    }


# ─── Internal: single-track background worker ─────────────────────────────
async def _download_single(req: DownloadRequest, album_dir: str | None = None) -> None:
    """
    Downloads one track, honouring the global DOWNLOAD_SEM concurrency limit.
    URL is fetched inside the semaphore so it is always fresh when the slot opens.
    If album_dir is provided, updates status.json on completion or failure.
    """
    async with DOWNLOAD_SEM:
        try:
            url_data   = await get_download_url(req.track_id, req.quality)
            track_info = await get_track(req.track_id)
            title  = track_info.get("title", req.track_id)
            ext    = "mp3" if req.quality == "mp3" else "flac"
            fname  = sanitize_filename(
                f"{track_info.get('track_number', 0):02d} - {title}"
            ) + f".{ext}"
            out = os.path.join(req.output_dir, fname)

            logger.info(f"[{req.track_id}] Starting: {fname}")

            # [IMPROVEMENT 3] Explicit network error handling
            try:
                async with http_client.stream("GET", url_data["url"], timeout=None) as r:
                    with open(out, "wb") as f:
                        async for chunk in r.aiter_bytes(65536):
                            f.write(chunk)
                logger.info(f"[{req.track_id}] Done: {fname}")
            except httpx.TimeoutException as e:
                raise Exception(f"Timeout during file download: {e}")
            except httpx.RequestError as e:
                raise Exception(f"Network error during file download: {e}")

            if album_dir:
                status = _read_status(album_dir)
                if req.track_id in status:
                    status[req.track_id].update({"status": "done", "file": fname})
                    _write_status(album_dir, status)

        except Exception as e:
            logger.error(f"[{req.track_id}] Failed: {e}")
            if album_dir:
                status = _read_status(album_dir)
                if req.track_id in status:
                    status[req.track_id].update({"status": "error", "error": str(e)})
                    _write_status(album_dir, status)


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
