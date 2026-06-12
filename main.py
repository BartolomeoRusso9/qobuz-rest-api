import asyncio
import hashlib
import json
import logging
import os
import re
import time
import uvicorn
import subprocess
import shutil
from fastapi.responses import FileResponse
from mutagen.flac import FLAC, Picture
from mutagen.id3 import (
    ID3, TIT2, TPE1, TALB, TRCK, APIC, ID3NoHeaderError,
    TCON, TDRC, TCOP, TSRC, TCOM, TPUB, TPOS, TXXX, COMM, TLEN, WXXX, USLT,
)
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
APP_ID     = os.getenv("QOBUZ_APP_ID", "").strip("'\"")
SECRET     = os.getenv("QOBUZ_SECRET",  "").strip("'\"")
_TOKEN     = os.getenv("QOBUZ_TOKEN",   "").strip("'\"")   # overridable via /login

DEV_MODE = os.getenv("DEV_MODE", "False").lower() in ("true", "1", "yes")

QUALITY_MAP = {
    "mp3":  5,
    "flac": 6,
    "hi24": 7,
    "hi96": 27,
}

_RATE_LIMIT_MAX_RETRIES = 3
_RATE_LIMIT_BASE_DELAY  = 1.0
_RATE_LIMIT_MAX_DELAY   = 15.0

DOWNLOAD_SEM: asyncio.Semaphore   # initialised in lifespan
http_client: httpx.AsyncClient    # initialised in lifespan

# ─── [NEW] In-memory metadata cache with TTL ──────────────────────────────
# Avoids redundant Qobuz API calls for the same track/album/artist within
# the same session.  Default TTL: 10 minutes.  Set CACHE_TTL_SECONDS=0 to disable.

_CACHE_TTL   = int(os.getenv("CACHE_TTL_SECONDS", "600"))
_cache: dict[str, tuple[dict, float]] = {}   # key → (data, expiry_timestamp)

def _cache_get(key: str) -> dict | None:
    if _CACHE_TTL <= 0:
        return None
    entry = _cache.get(key)
    if entry and time.time() < entry[1]:
        return entry[0]
    _cache.pop(key, None)
    return None

def _cache_set(key: str, data: dict) -> None:
    if _CACHE_TTL > 0:
        _cache[key] = (data, time.time() + _CACHE_TTL)

# ─── [NEW] Runtime token store (set by /login, falls back to .env) ─────────

_runtime_token: str = ""

def _get_token() -> str:
    """Returns the active token: /login takes precedence over .env."""
    tok = _runtime_token or _TOKEN
    if not tok:
        raise HTTPException(
            401,
            "No token available.  Call POST /set-token or set QOBUZ_TOKEN in .env.",
        )
    return tok

# ─── Helpers ───────────────────────────────────────────────────────────────

def sanitize_filename(name: str) -> str:
    name = re.sub(r'[\\/*?:"<>|]', "-", name)
    name = re.sub(r"-{2,}", "-", name)
    name = re.sub(r" {2,}", " ", name)
    return name.strip(" -")


def _format_filename(template: str, track_info: dict, album_info: dict) -> str:
    """
    [NEW] Resolves a filename template with track/album placeholders.
    Supported tokens: {track}, {title}, {artist}, {album}, {year}, {date},
                      {disc}, {isrc}, {genre}
    Example: "{track:02d} - {title}" → "03 - Some Song"
    """
    artist = (
        track_info.get("performer", {}).get("name")
        or album_info.get("artist", {}).get("name", "Unknown")
    )
    release_date = (
        track_info.get("release_date_original")
        or album_info.get("release_date_original", "")
    )
    values = {
        "track":  track_info.get("track_number", 0),
        "title":  track_info.get("title", "Unknown"),
        "artist": artist,
        "album":  album_info.get("title", "Unknown"),
        "year":   release_date[:4] if release_date else "",
        "date":   release_date,
        "disc":   track_info.get("media_number", 1),
        "isrc":   track_info.get("isrc", ""),
        "genre":  album_info.get("genre", {}).get("name", ""),
    }
    try:
        return sanitize_filename(template.format_map(values))
    except (KeyError, ValueError):
        logger.warning("Invalid filename_format '%s', falling back to default.", template)
        return sanitize_filename(f"{values['track']:02d} - {values['title']}")


def _apply_metadata(
    file_path: str,
    track_info: dict,
    album_info: dict,
    cover_bytes: bytes | None,
    lyrics_text: str = "",
) -> None:
    """Injects full Qobuz metadata (including lyrics) into FLAC or MP3."""
    ext = file_path.rsplit(".", 1)[-1].lower()

    title        = track_info.get("title", "Unknown")
    version      = track_info.get("version")
    if version:
        title = f"{title} ({version})"
    artist       = (
        track_info.get("performer", {}).get("name")
        or album_info.get("artist", {}).get("name", "Unknown")
    )
    album        = album_info.get("title", "Unknown")
    track_num    = str(track_info.get("track_number", 1))
    total_tracks = str(album_info.get("tracks_count", track_num))
    disc_num     = str(track_info.get("media_number", 1))
    total_discs  = str(album_info.get("media_count", 1))
    genre          = album_info.get("genre", {}).get("name", "")
    release_date   = track_info.get("release_date_original") or album_info.get("release_date_original", "")
    copyright_text = track_info.get("copyright") or album_info.get("copyright", "")
    isrc           = track_info.get("isrc", "")
    composer       = track_info.get("composer", {}).get("name", "")
    label          = album_info.get("label", {}).get("name", "")
    upc            = album_info.get("upc", "")
    duration_sec   = track_info.get("duration")
    replay_gain    = track_info.get("audio_info", {}).get("replaygain_track_gain")
    replay_peak    = track_info.get("audio_info", {}).get("replaygain_track_peak")
    explicit       = track_info.get("parental_warning", False)
    credits_text   = track_info.get("performers", "")
    tech_specs     = album_info.get("maximum_technical_specifications", "")
    qobuz_track_id = str(track_info.get("id", ""))
    qobuz_album_id = str(album_info.get("qobuz_id", ""))
    album_url      = album_info.get("url", "")
    awards         = album_info.get("awards", [])
    awards_str     = ", ".join(a.get("name", "") for a in awards)

    if ext == "flac":
        audio = FLAC(file_path)
        audio["title"]          = [title]
        audio["artist"]         = [artist]
        audio["album"]          = [album]
        audio["tracknumber"]    = [track_num]
        audio["totaltracks"]    = [total_tracks]
        audio["discnumber"]     = [disc_num]
        audio["totaldiscs"]     = [total_discs]
        if genre:            audio["genre"]          = [genre]
        if release_date:     audio["date"]           = [release_date]
        if copyright_text:   audio["copyright"]      = [copyright_text]
        if isrc:             audio["isrc"]           = [isrc]
        if composer:         audio["composer"]       = [composer]
        if label:            audio["organization"]   = [label]
        if upc:              audio["barcode"]        = [upc]
        if duration_sec:     audio["length"]         = [str(duration_sec * 1000)]
        if replay_gain is not None: audio["replaygain_track_gain"] = [f"{replay_gain} dB"]
        if replay_peak is not None: audio["replaygain_track_peak"] = [str(replay_peak)]
        if explicit:         audio["itunesadvisory"] = ["1"]
        if credits_text:     audio["comment"]        = [credits_text]
        if lyrics_text:      audio["lyrics"]         = [lyrics_text]
        if tech_specs:       audio["technical_specifications"] = [tech_specs]
        if qobuz_track_id:   audio["qobuz_track_id"] = [qobuz_track_id]
        if qobuz_album_id:   audio["qobuz_album_id"] = [qobuz_album_id]
        if album_url:        audio["url"]            = [album_url]
        if awards_str:       audio["awards"]         = [awards_str]
        if cover_bytes:
            pic = Picture()
            pic.type = 3
            pic.mime = "image/jpeg"
            pic.desc = "Cover"
            pic.data = cover_bytes
            audio.clear_pictures()
            audio.add_picture(pic)
        audio.save()

    elif ext == "mp3":
        try:
            audio = ID3(file_path)
        except ID3NoHeaderError:
            audio = ID3()
        audio.add(TIT2(encoding=3, text=[title]))
        audio.add(TPE1(encoding=3, text=[artist]))
        audio.add(TALB(encoding=3, text=[album]))
        audio.add(TRCK(encoding=3, text=[f"{track_num}/{total_tracks}"]))
        audio.add(TPOS(encoding=3, text=[f"{disc_num}/{total_discs}"]))
        if genre:          audio.add(TCON(encoding=3, text=[genre]))
        if release_date:   audio.add(TDRC(encoding=3, text=[release_date]))
        if copyright_text: audio.add(TCOP(encoding=3, text=[copyright_text]))
        if isrc:           audio.add(TSRC(encoding=3, text=[isrc]))
        if composer:       audio.add(TCOM(encoding=3, text=[composer]))
        if label:          audio.add(TPUB(encoding=3, text=[label]))
        if duration_sec:   audio.add(TLEN(encoding=3, text=[str(duration_sec * 1000)]))
        if lyrics_text:    audio.add(USLT(encoding=3, lang="und", desc="", text=lyrics_text))
        if replay_gain is not None: audio.add(TXXX(encoding=3, desc="REPLAYGAIN_TRACK_GAIN", text=[f"{replay_gain} dB"]))
        if replay_peak is not None: audio.add(TXXX(encoding=3, desc="REPLAYGAIN_TRACK_PEAK", text=[str(replay_peak)]))
        if tech_specs:     audio.add(TXXX(encoding=3, desc="TECHNICAL_SPECIFICATIONS", text=[tech_specs]))
        if qobuz_track_id: audio.add(TXXX(encoding=3, desc="QOBUZ_TRACK_ID",           text=[qobuz_track_id]))
        if qobuz_album_id: audio.add(TXXX(encoding=3, desc="QOBUZ_ALBUM_ID",           text=[qobuz_album_id]))
        if upc:            audio.add(TXXX(encoding=3, desc="BARCODE",                  text=[upc]))
        if awards_str:     audio.add(TXXX(encoding=3, desc="AWARDS",                  text=[awards_str]))
        if credits_text:   audio.add(COMM(encoding=3, lang="eng", desc="Credits",      text=[credits_text]))
        if album_url:      audio.add(WXXX(encoding=3, desc="Qobuz URL",               url=album_url))
        if cover_bytes:
            audio.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=cover_bytes))
        audio.save(file_path, v2_version=3)

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
    path = os.path.join(album_dir, _STATUS_FILE)
    tmp  = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(status, f, indent=2)
    os.replace(tmp, path)

# ─── DEV_MODE logger ──────────────────────────────────────────────────────

def _log_response(method: str, url: str, resp: httpx.Response) -> None:
    if not DEV_MODE:
        return
    logger.debug(
        "[DEV] %s %s → %s\n headers: %s\n body: %s",
        method, url, resp.status_code, dict(resp.headers), resp.text[:2000],
    )

# ─── Lifespan ──────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global http_client, DOWNLOAD_SEM
    if DEV_MODE:
        logger.warning("DEV_MODE enabled — upstream responses will be logged at DEBUG level")
    DOWNLOAD_SEM = asyncio.Semaphore(3)
    http_client  = httpx.AsyncClient(
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
    description=(
        "Local REST API for Qobuz — search, metadata, stream, download and playlist support.\n\n"
        "**Auth:** set `QOBUZ_TOKEN` in `.env`, or call `POST /set-token` at runtime to update "
        "the token without restarting the server.\n\n"
        "To get your token: log in at [play.qobuz.com](https://play.qobuz.com), open DevTools → "
        "Application → Local Storage → copy the value of `localuser.token`."
    ),
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── Core request helper ───────────────────────────────────────────────────

async def qobuz_get(endpoint: str, params: dict) -> dict:
    """Authenticated GET with retry/backoff on 429, explicit 401 message and cache support."""
    if not APP_ID or not SECRET:
        raise HTTPException(500, "APP_ID and SECRET not configured in .env.")

    params = {**params, "app_id": APP_ID, "user_auth_token": _get_token()}
    url    = f"{QOBUZ_BASE}/{endpoint}"

    try:
        for attempt in range(_RATE_LIMIT_MAX_RETRIES + 1):
            r = await http_client.get(url, params=params)
            _log_response("GET", url, r)

            if r.status_code == 401:
                raise HTTPException(
                    401,
                    "Token invalid or expired.  "
                    "Call POST /set-token with a fresh token from play.qobuz.com localStorage, "
                    "or update QOBUZ_TOKEN in .env.",
                )
            if r.status_code == 429 and attempt < _RATE_LIMIT_MAX_RETRIES:
                delay = min(_RATE_LIMIT_BASE_DELAY * (2 ** attempt), _RATE_LIMIT_MAX_DELAY)
                try:
                    delay = min(float(r.headers.get("Retry-After", delay)), _RATE_LIMIT_MAX_DELAY)
                except ValueError:
                    pass
                logger.warning("Rate-limited (429) on %s — retry in %.1fs (%d/%d)",
                               endpoint, delay, attempt + 1, _RATE_LIMIT_MAX_RETRIES)
                await asyncio.sleep(delay)
                continue
            if r.status_code != 200:
                data = {}
                try:
                    data = r.json()
                except Exception:
                    pass
                raise HTTPException(r.status_code, data.get("message", "Qobuz API error"))
            return r.json()
        raise HTTPException(429, "Qobuz rate limit exceeded — all retries exhausted.")
    except HTTPException:
        raise
    except httpx.TimeoutException as e:
        logger.error("Timeout on %s: %s", endpoint, e)
        raise HTTPException(504, "Qobuz API timed out — try again shortly.")
    except httpx.RequestError as e:
        logger.error("Network error on %s: %s", endpoint, e)
        raise HTTPException(503, f"Connection error: {e}")

# Cached wrappers for the most-called endpoints

async def _get_track_cached(track_id: str) -> dict:
    key  = f"track:{track_id}"
    data = _cache_get(key)
    if data is None:
        data = await qobuz_get("track/get", {"track_id": track_id})
        _cache_set(key, data)
    return data

async def _get_album_cached(album_id: str) -> dict:
    key  = f"album:{album_id}"
    data = _cache_get(key)
    if data is None:
        data = await qobuz_get("album/get", {"album_id": album_id})
        _cache_set(key, data)
    return data

# ─── Auth helpers ──────────────────────────────────────────────────────────

def make_sig(track_id: str, format_id: int) -> tuple[str, str]:
    ts  = str(int(time.time()))
    raw = f"trackgetFileUrlformat_id{format_id}intentstreamtrack_id{track_id}{ts}{SECRET}"
    sig = hashlib.md5(raw.encode()).hexdigest()
    return sig, ts

# ─── Models ────────────────────────────────────────────────────────────────

class SetTokenRequest(BaseModel):
    token: str

class DownloadRequest(BaseModel):
    track_id: str
    quality:         Literal["mp3", "flac", "hi24", "hi96"] = "flac"
    target_format:   Literal["mp3", "flac", "alac", "wav", "opus"] | None = None
    output_dir:      str = "./downloads"
    filename_format: str = "{track:02d} - {title}"   # [NEW] customisable template

# ─── Endpoint: Health ──────────────────────────────────────────────────────

@app.get("/", tags=["info"])
async def root():
    return {
        "status":  "online",
        "version": "2.0.0",
        "docs":    "http://localhost:8000/docs",
        "auth":    "token active" if (_runtime_token or _TOKEN) else "no token — call POST /set-token or set QOBUZ_TOKEN in .env",
    }

# ─── Endpoint: Set Token ───────────────────────────────────────────────────

@app.post("/set-token", tags=["auth"])
async def set_token(req: SetTokenRequest):
    """
    Updates the active Qobuz user token at runtime without restarting the server
    or editing .env.

    How to get the token:
    1. Open https://play.qobuz.com and log in.
    2. Open DevTools → Application → Local Storage → https://play.qobuz.com.
    3. Copy the value of the key `localuser.token`.
    4. POST it here.

    The token is stored in memory for the current session only.
    To make it permanent, copy it into QOBUZ_TOKEN in your .env.
    """
    global _runtime_token

    token = req.token.strip().strip("'\"")
    if not token:
        raise HTTPException(400, "Token cannot be empty.")

    _runtime_token = token
    logger.info("Token updated via POST /set-token")

    return {"status": "ok", "token_set": True}

# ─── [NEW] Endpoint: Me ────────────────────────────────────────────────────

@app.get("/me", tags=["auth"])
async def me():
    """Returns the authenticated user's profile and subscription plan."""
    data = await qobuz_get("user/get", {"user_id": ""})
    user = data.get("user", data)
    return {
        "login":   user.get("login"),
        "display": user.get("display_name"),
        "email":   user.get("email"),
        "plan":    user.get("credential", {}).get("label"),
        "country": user.get("country_code"),
    }

# ─── Endpoint: Search ──────────────────────────────────────────────────────

@app.get("/search", tags=["search"])
async def search(
    q:     str = Query(..., description="Text to search"),
    type:  str = Query("tracks", description="tracks | albums | artists | playlists"),
    limit: int = Query(10, ge=1, le=50),
):
    return await qobuz_get("catalog/search", {"query": q, "type": type, "limit": limit})

# ─── Endpoint: Track ───────────────────────────────────────────────────────

@app.get("/track/{track_id}", tags=["metadata"])
async def get_track(track_id: str):
    return await _get_track_cached(track_id)

# ─── Endpoint: Album ───────────────────────────────────────────────────────

@app.get("/album/{album_id}", tags=["metadata"])
async def get_album(album_id: str):
    return await _get_album_cached(album_id)

# ─── Endpoint: Artist ──────────────────────────────────────────────────────

@app.get("/artist/{artist_id}", tags=["metadata"])
async def get_artist(
    artist_id: str,
    limit: int = Query(20, ge=1, le=100),
):
    key  = f"artist:{artist_id}:{limit}"
    data = _cache_get(key)
    if data is None:
        data = await qobuz_get("artist/get", {
            "artist_id": artist_id,
            "extra":     "albums",
            "limit":     limit,
        })
        _cache_set(key, data)
    return data

# ─── [NEW] Endpoint: Playlist ──────────────────────────────────────────────

@app.get("/playlist/{playlist_id}", tags=["metadata"])
async def get_playlist(playlist_id: str):
    """Returns playlist metadata and the full track listing."""
    key  = f"playlist:{playlist_id}"
    data = _cache_get(key)
    if data is None:
        data = await qobuz_get("playlist/get", {
            "playlist_id": playlist_id,
            "extra":       "tracks",
            "limit":       500,
            "offset":      0,
        })
        _cache_set(key, data)
    return data

# ─── [NEW] Endpoint: Download Playlist ─────────────────────────────────────

@app.post("/download-playlist/{playlist_id}", tags=["download"])
async def download_playlist(
    playlist_id:     str,
    background_tasks: BackgroundTasks,
    quality:         Literal["mp3", "flac", "hi24", "hi96"] = Query("flac"),
    output_dir:      str  = Query("./downloads"),
    filename_format: str  = Query("{track:02d} - {title}"),
):
    """
    Downloads all tracks from a Qobuz playlist.
    Tracks are organised in a subfolder named after the playlist.
    Progress is tracked in status.json (same as /download-album).
    """
    playlist = await get_playlist(playlist_id)
    tracks   = playlist.get("tracks", {}).get("items", [])

    if not tracks:
        raise HTTPException(404, "No tracks found in the playlist.")

    pl_name    = playlist.get("name", playlist_id)
    pl_dir     = os.path.join(output_dir, sanitize_filename(pl_name))
    os.makedirs(pl_dir, exist_ok=True)

    status = {
        str(t["id"]): {"title": t.get("title", ""), "status": "pending"}
        for t in tracks
    }
    _write_status(pl_dir, status)

    logger.info("Playlist download queued: %s (%d tracks)", pl_name, len(tracks))

    for track in tracks:
        background_tasks.add_task(
            _download_single,
            DownloadRequest(
                track_id=str(track["id"]),
                quality=quality,
                output_dir=pl_dir,
                filename_format=filename_format,
            ),
            pl_dir,
        )

    return {
        "status":     "downloading",
        "playlist":   pl_name,
        "tracks":     len(tracks),
        "output_dir": pl_dir,
        "quality":    quality,
    }

# ─── [NEW] Endpoint: Playlist Status ───────────────────────────────────────

@app.get("/playlist-status/{playlist_id}", tags=["download"])
async def playlist_status(
    playlist_id: str,
    output_dir:  str = Query("./downloads"),
):
    """Returns the download progress for a playlist (reads status.json)."""
    playlist = await get_playlist(playlist_id)
    pl_name  = playlist.get("name", playlist_id)
    pl_dir   = os.path.join(output_dir, sanitize_filename(pl_name))
    status   = _read_status(pl_dir)
    if not status:
        raise HTTPException(404, "No status file found — has the download been started?")
    done    = sum(1 for t in status.values() if t["status"] == "done")
    errors  = sum(1 for t in status.values() if t["status"] == "error")
    pending = sum(1 for t in status.values() if t["status"] == "pending")
    return {
        "playlist": pl_name,
        "done":     done,
        "pending":  pending,
        "errors":   errors,
        "tracks":   status,
    }

# ─── Endpoint: Download URL ────────────────────────────────────────────────

@app.get("/download-url/{track_id}", tags=["download"])
async def get_download_url(
    track_id: str,
    quality:  Literal["mp3", "flac", "hi24", "hi96"] = Query("flac"),
):
    if not APP_ID or not SECRET:
        raise HTTPException(500, "APP_ID and SECRET not configured.")
    format_id  = QUALITY_MAP[quality]
    sig, ts    = make_sig(track_id, format_id)
    data       = await qobuz_get("track/getFileUrl", {
        "track_id":    track_id,
        "format_id":   format_id,
        "intent":      "stream",
        "request_ts":  ts,
        "request_sig": sig,
    })
    if "url" not in data:
        raise HTTPException(
            403,
            data.get("message", "URL unavailable — check quality tier or subscription."),
        )
    return {
        "track_id":     track_id,
        "quality":      quality,
        "format_id":    format_id,
        "url":          data["url"],
        "mime_type":    data.get("mime_type"),
        "bit_depth":    data.get("bit_depth"),
        "sampling_rate":data.get("sampling_rate"),
        "file_size":    data.get("file_size"),
    }

# ─── Endpoint: Stream ──────────────────────────────────────────────────────

@app.get("/stream/{track_id}", tags=["download"])
async def stream_track(
    track_id: str,
    quality:  Literal["mp3", "flac", "hi24", "hi96"] = Query("flac"),
):
    url_data = await get_download_url(track_id, quality)

    async def generate():
        try:
            async with http_client.stream("GET", url_data["url"], timeout=None) as r:
                async for chunk in r.aiter_bytes(65536):
                    yield chunk
        except httpx.TimeoutException:
            logger.error("Timeout streaming track %s", track_id)
        except httpx.RequestError as e:
            logger.error("Network error streaming track %s: %s", track_id, e)

    mime = url_data.get("mime_type") or ("audio/mpeg" if quality == "mp3" else "audio/flac")
    return StreamingResponse(generate(), media_type=mime)

# ─── Endpoint: Download single track ──────────────────────────────────────

@app.post("/download", tags=["download"])
async def download_track(req: DownloadRequest, background_tasks: BackgroundTasks):
    url_data   = await get_download_url(req.track_id, req.quality)
    track_info = await _get_track_cached(req.track_id)
    album_info = track_info.get("album", {})
    ext        = "mp3" if req.quality == "mp3" else "flac"
    fname      = _format_filename(req.filename_format, track_info, album_info) + f".{ext}"
    os.makedirs(req.output_dir, exist_ok=True)
    out_path   = os.path.join(req.output_dir, fname)

    async def do_download():
        async with DOWNLOAD_SEM:
            logger.info("[%s] Starting: %s", req.track_id, fname)
            try:
                async with http_client.stream("GET", url_data["url"], timeout=None) as r:
                    with open(out_path, "wb") as f:
                        async for chunk in r.aiter_bytes(65536):
                            f.write(chunk)
                logger.info("[%s] Done: %s", req.track_id, fname)
            except httpx.TimeoutException:
                logger.error("[%s] Timeout", req.track_id)
            except httpx.RequestError as e:
                logger.error("[%s] Network error: %s", req.track_id, e)
            except Exception as e:
                logger.error("[%s] Failed: %s", req.track_id, e)

    background_tasks.add_task(do_download)
    return {
        "status":   "downloading",
        "track_id": req.track_id,
        "filename": fname,
        "output":   out_path,
        "quality":  req.quality,
    }

# ─── Endpoint: Download album ──────────────────────────────────────────────

@app.post("/download-album/{album_id}", tags=["download"])
async def download_album(
    album_id:        str,
    background_tasks: BackgroundTasks,
    quality:         Literal["mp3", "flac", "hi24", "hi96"] = Query("flac"),
    output_dir:      str  = Query("./downloads"),
    filename_format: str  = Query("{track:02d} - {title}"),
):
    album  = await _get_album_cached(album_id)
    tracks = album.get("tracks", {}).get("items", [])
    if not tracks:
        raise HTTPException(404, "No tracks found in the album.")

    artist_name = album.get("artist", {}).get("name", "Unknown")
    album_title = album.get("title", album_id)
    album_dir   = os.path.join(output_dir, sanitize_filename(f"{artist_name} - {album_title}"))
    os.makedirs(album_dir, exist_ok=True)

    status = {
        str(t["id"]): {"title": t.get("title", ""), "status": "pending"}
        for t in tracks
    }
    _write_status(album_dir, status)
    logger.info("Album download queued: %s — %s (%d tracks)", artist_name, album_title, len(tracks))

    for track in tracks:
        background_tasks.add_task(
            _download_single,
            DownloadRequest(
                track_id=str(track["id"]),
                quality=quality,
                output_dir=album_dir,
                filename_format=filename_format,
            ),
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

# ─── Endpoint: Album status ────────────────────────────────────────────────

@app.get("/album-status/{album_id}", tags=["download"])
async def album_status(album_id: str, output_dir: str = Query("./downloads")):
    album  = await _get_album_cached(album_id)
    artist = album.get("artist", {}).get("name", "Unknown")
    title  = album.get("title", album_id)
    status = _read_status(os.path.join(output_dir, sanitize_filename(f"{artist} - {title}")))
    if not status:
        raise HTTPException(404, "Status file not found — has the download been started?")
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

# ─── Internal: single-track worker ─────────────────────────────────────────

async def _download_single(req: DownloadRequest, album_dir: str | None = None) -> None:
    async with DOWNLOAD_SEM:
        try:
            url_data   = await get_download_url(req.track_id, req.quality)
            track_info = await _get_track_cached(req.track_id)
            album_info = track_info.get("album", {})

            original_ext = "mp3" if req.quality == "mp3" else "flac"
            final_ext    = req.target_format or original_ext
            base_fname   = _format_filename(req.filename_format, track_info, album_info)
            temp_out     = os.path.join(req.output_dir, f"{base_fname}.{original_ext}")
            final_out    = os.path.join(req.output_dir, f"{base_fname}.{final_ext}")

            logger.info("[%s] Starting: %s.%s", req.track_id, base_fname, final_ext)

            # 1. Download
            try:
                async with http_client.stream("GET", url_data["url"], timeout=None) as r:
                    with open(temp_out, "wb") as f:
                        async for chunk in r.aiter_bytes(65536):
                            f.write(chunk)
            except httpx.TimeoutException as e:
                raise Exception(f"Timeout during download: {e}")
            except httpx.RequestError as e:
                raise Exception(f"Network error during download: {e}")

            # 2. Transcode (if needed)
            if original_ext != final_ext:
                ffmpeg_cmd = ["ffmpeg", "-y", "-i", temp_out]
                if final_ext == "alac":
                    ffmpeg_cmd.extend(["-c:a", "alac"])
                elif final_ext == "wav":
                    ffmpeg_cmd.extend(["-c:a", "pcm_s16le"])
                elif final_ext == "opus":
                    ffmpeg_cmd.extend(["-c:a", "libopus", "-b:a", "192k"])
                ffmpeg_cmd.append(final_out)
                subprocess.run(ffmpeg_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
                os.remove(temp_out)
            else:
                final_out = temp_out

            # 3. Cover art
            cover_bytes = None
            cover_url   = album_info.get("image", {}).get("large")
            if cover_url:
                cr = await http_client.get(cover_url)
                if cr.status_code == 200:
                    cover_bytes = cr.content

            # 4. Lyrics via LRCLIB
            plain_lyrics = ""
            try:
                artist_name = (
                    track_info.get("performer", {}).get("name")
                    or album_info.get("artist", {}).get("name", "")
                )
                clean_title = track_info.get("title", "").split(" (")[0]

                lr = await http_client.get("https://lrclib.net/api/get", params={
                    "track_name":  clean_title,
                    "artist_name": artist_name,
                    "album_name":  album_info.get("title", ""),
                    "duration":    track_info.get("duration", 0),
                }, timeout=5.0)
                if lr.status_code == 200:
                    plain_lyrics = lr.json().get("plainLyrics") or ""
                if not plain_lyrics:
                    sr = await http_client.get("https://lrclib.net/api/search", params={
                        "track_name":  clean_title,
                        "artist_name": artist_name,
                    }, timeout=5.0)
                    if sr.status_code == 200:
                        for res in sr.json() or []:
                            if res.get("plainLyrics"):
                                plain_lyrics = res["plainLyrics"]
                                break
            except Exception as e:
                logger.warning("[%s] Lyrics fetch failed: %s", req.track_id, e)

            # 5. Metadata
            _apply_metadata(final_out, track_info, album_info, cover_bytes, plain_lyrics)
            logger.info("[%s] Done: %s.%s", req.track_id, base_fname, final_ext)

            if album_dir:
                s = _read_status(album_dir)
                if req.track_id in s:
                    s[req.track_id].update({"status": "done", "file": f"{base_fname}.{final_ext}"})
                    _write_status(album_dir, s)

        except Exception as e:
            logger.error("[%s] Failed: %s", req.track_id, e)
            if album_dir:
                s = _read_status(album_dir)
                if req.track_id in s:
                    s[req.track_id].update({"status": "error", "error": str(e)})
                    _write_status(album_dir, s)

# ─── Endpoint: Export album as ZIP ────────────────────────────────────────

@app.get("/export-album/{album_id}", tags=["download"])
async def export_album(album_id: str, output_dir: str = Query("./downloads")):
    """Zips a completed album download and returns it as a file."""
    album  = await _get_album_cached(album_id)
    artist = album.get("artist", {}).get("name", "Unknown")
    title  = album.get("title", album_id)
    album_dir   = os.path.join(output_dir, sanitize_filename(f"{artist} - {title}"))
    status      = _read_status(album_dir)
    if not status:
        raise HTTPException(404, "Status file not found — download not started?")
    pending = sum(1 for t in status.values() if t["status"] == "pending")
    if pending > 0:
        raise HTTPException(400, f"Download in progress: {pending} track(s) remaining.")
    zip_base = os.path.join(output_dir, sanitize_filename(f"{artist} - {title}"))
    shutil.make_archive(zip_base, "zip", album_dir)
    return FileResponse(
        path=f"{zip_base}.zip",
        media_type="application/zip",
        filename=f"{sanitize_filename(f'{artist} - {title}')}.zip",
    )

# ─── Entry point ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)