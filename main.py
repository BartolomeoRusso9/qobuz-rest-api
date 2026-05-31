"""
Qobuz Local API Server
Exposes Qobuz functionalities on localhost:8000
Compatible with programs like Spotiflac
"""

import hashlib
import time
import os
import re
import httpx
from contextlib import asynccontextmanager
from typing import Literal
from fastapi import FastAPI, HTTPException, Query, BackgroundTasks
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv, set_key

load_dotenv()

# ─── Configuration ─────────────────────────────────────────────────────────
QOBUZ_BASE = "https://www.qobuz.com/api.json/0.2"
APP_ID     = os.getenv("QOBUZ_APP_ID", "")
SECRET     = os.getenv("QOBUZ_SECRET", "")
TOKEN      = os.getenv("QOBUZ_TOKEN", "")

# Quality IDs
QUALITY_MAP = {
    "mp3":    5,   # MP3 320kbps
    "flac":   6,   # FLAC 16-bit (CD quality)
    "hi24":   7,   # FLAC 24-bit ≤96kHz
    "hi96":  27,   # FLAC 24-bit >96kHz (Hi-Res)
}

# ─── Automatic Extraction ──────────────────────────────────────────────────
async def auto_extract_keys():
    """
    Integrates the Qobuz-AppID-Secret-Tool logic to automatically extract
    App ID and Secret from the web player if they are missing in the .env file.
    Extracted values are persisted to .env so they survive restarts.
    """
    global APP_ID, SECRET
    if APP_ID and SECRET:
        return

    print("[*] Missing App ID or Secret. Starting automatic extraction from play.qobuz.com...")
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get("https://play.qobuz.com/login")

            # Find all references to Web Player JS files
            scripts = re.findall(r'src=["\'](/resources/[^"\']+\.js)["\']', r.text)

            for script_path in scripts:
                js_url = f"https://play.qobuz.com{script_path}"
                r_js = await client.get(js_url)
                js = r_js.text

                # Extract APP_ID using regex
                if not APP_ID:
                    match_id = re.search(r'app_id\s*:\s*["\']([^"\']+)["\']', js)
                    if match_id:
                        APP_ID = match_id.group(1)
                        set_key(".env", "QOBUZ_APP_ID", APP_ID)
                        print(f"[+] App ID automatically extracted and saved: {APP_ID}")

                # Extract SECRET (usually a 32-character hex hash associated with the player)
                if not SECRET:
                    secrets = re.findall(r'["\']([a-f0-9]{32})["\']', js)
                    if secrets:
                        SECRET = secrets[0]
                        set_key(".env", "QOBUZ_SECRET", SECRET)
                        print(f"[+] App Secret automatically extracted and saved: {SECRET}")

                # If both are found, stop the loop
                if APP_ID and SECRET:
                    break

            if not APP_ID or not SECRET:
                print("[-] Unable to automatically extract keys from JS files.")
    except Exception as e:
        print(f"[-] Connection error during extraction: {e}")

# ─── Lifespan ──────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    await auto_extract_keys()
    yield

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
    """Generates the HMAC signature required by track/getFileUrl"""
    ts = str(int(time.time()))
    raw = f"trackgetFileUrlformat_id{format_id}intentstreamtrack_id{track_id}{ts}{SECRET}"
    sig = hashlib.md5(raw.encode()).hexdigest()
    return sig, ts

async def qobuz_get(endpoint: str, params: dict) -> dict:
    if not APP_ID or not SECRET:
        raise HTTPException(500, "App ID and Secret unavailable. Configure .env or check connection to Qobuz.")

    token = await get_token()
    params.update({"app_id": APP_ID, "user_auth_token": token})
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(f"{QOBUZ_BASE}/{endpoint}", params=params)
    data = r.json()
    if r.status_code != 200:
        raise HTTPException(r.status_code, data.get("message", "Qobuz Error"))
    return data

# ─── Models ────────────────────────────────────────────────────────────────
class DownloadRequest(BaseModel):
    track_id:   str
    quality:    Literal["mp3", "flac", "hi24", "hi96"] = "flac"
    output_dir: str = "./downloads"

# ─── Endpoint: Health ──────────────────────────────────────────────────────
@app.get("/", tags=["info"])
async def root():
    return {
        "status": "online",
        "version": "1.0.0",
        "docs": "http://localhost:8000/docs",
        "endpoints": [
            "/search", "/track/{id}", "/album/{id}",
            "/artist/{id}", "/download-url/{track_id}",
            "/stream/{track_id}", "/download", "/download-album/{album_id}",
        ]
    }

# ─── Endpoint: Search ──────────────────────────────────────────────────────
@app.get("/search", tags=["search"])
async def search(
    q:     str = Query(..., description="Text to search"),
    type:  str = Query("tracks", description="tracks | albums | artists"),
    limit: int = Query(10, ge=1, le=50),
):
    """Searches for tracks, albums, or artists on Qobuz."""
    return await qobuz_get("catalog/search", {
        "query": q,
        "type":  type,
        "limit": limit,
    })

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
    limit: int = Query(20, ge=1, le=100),
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
        raise HTTPException(403, data.get("message", "URL not available (unsupported quality or insufficient subscription)"))

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
    url = url_data["url"]

    async def generate():
        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream("GET", url) as r:
                async for chunk in r.aiter_bytes(chunk_size=65536):
                    yield chunk

    mime = url_data.get("mime_type") or (
        "audio/mpeg" if quality == "mp3" else "audio/flac"
    )
    return StreamingResponse(generate(), media_type=mime)

# ─── Endpoint: Download to Disk ────────────────────────────────────────────
@app.post("/download", tags=["download"])
async def download_track(req: DownloadRequest, background_tasks: BackgroundTasks):
    """Downloads a track to disk in the specified path."""
    url_data   = await get_download_url(req.track_id, req.quality)
    track_info = await get_track(req.track_id)

    artist = track_info.get("performer", {}).get("name", "Unknown Artist")
    title  = track_info.get("title", req.track_id)
    ext    = "mp3" if req.quality == "mp3" else "flac"
    fname  = f"{artist} - {title}.{ext}".replace("/", "-").replace(":", "-")

    os.makedirs(req.output_dir, exist_ok=True)
    out_path = os.path.join(req.output_dir, fname)

    async def do_download():
        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream("GET", url_data["url"]) as r:
                with open(out_path, "wb") as f:
                    async for chunk in r.aiter_bytes(65536):
                        f.write(chunk)

    background_tasks.add_task(do_download)

    return {
        "status":   "downloading",
        "track_id": req.track_id,
        "filename": fname,
        "output":   out_path,
        "quality":  req.quality,
    }

# ─── Endpoint: Full Album Download ─────────────────────────────────────────
@app.post("/download-album/{album_id}", tags=["download"])
async def download_album(
    album_id:         str,
    background_tasks: BackgroundTasks,
    quality:          Literal["mp3", "flac", "hi24", "hi96"] = Query("flac"),
    output_dir:       str = Query("./downloads"),
):
    """Downloads all tracks from an album to disk."""
    album  = await get_album(album_id)
    tracks = album.get("tracks", {}).get("items", [])
    if not tracks:
        raise HTTPException(404, "No tracks found in the album")

    artist_name = album.get("artist", {}).get("name", "Unknown")
    album_title = album.get("title", album_id)
    album_dir   = os.path.join(output_dir, f"{artist_name} - {album_title}".replace("/", "-"))
    os.makedirs(album_dir, exist_ok=True)

    for track in tracks:
        tid = str(track["id"])
        background_tasks.add_task(
            _download_single,
            DownloadRequest(track_id=tid, quality=quality, output_dir=album_dir)
        )

    return {
        "status":     "downloading",
        "album":      album_title,
        "artist":     artist_name,
        "tracks":     len(tracks),
        "output_dir": album_dir,
        "quality":    quality,
    }

async def _download_single(req: DownloadRequest):
    """Internal helper for background downloading."""
    try:
        url_data   = await get_download_url(req.track_id, req.quality)
        track_info = await get_track(req.track_id)
        title = track_info.get("title", req.track_id)
        ext   = "mp3" if req.quality == "mp3" else "flac"
        fname = f"{track_info.get('track_number', 0):02d} - {title}.{ext}".replace("/", "-")
        out   = os.path.join(req.output_dir, fname)
        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream("GET", url_data["url"]) as r:
                with open(out, "wb") as f:
                    async for chunk in r.aiter_bytes(65536):
                        f.write(chunk)
    except Exception as e:
        print(f"[ERROR] track {req.track_id}: {e}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)