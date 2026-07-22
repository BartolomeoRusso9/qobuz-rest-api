# qobuz-rest-api

> [!IMPORTANT]
> Music piracy is illegal in most countries. This project is intended for use with a valid Qobuz subscription for personal/educational purposes (e.g. in your homelab). Use responsibly.

---

## Setup

Run `pip install -r requirements.txt`, then configure your `.env` file:

```
cp .env.example .env
```

### Environment variables

| Variable              | Description                                                                                                                      |
| --------------------- | -------------------------------------------------------------------------------------------------------------------------------- |
| `QOBUZ_APP_ID`        | Qobuz App ID — see [Finding APP\_ID and SECRET](#finding-app_id-and-secret)                                                      |
| `QOBUZ_SECRET`        | Qobuz Secret — see [Finding APP\_ID and SECRET](#finding-app_id-and-secret)                                                      |
| `QOBUZ_TOKEN`         | User token → localStorage of [play.qobuz.com](https://play.qobuz.com), key `localuser.token`                                    |
| `QOBUZ_AUTH_TOKENS`   | Pool of multiple tokens as a JSON array or comma-separated string — one is picked at random per request, skipping tokens that recently returned `401` |
| `QOBUZ_API_BASE`      | Alternative Qobuz API base URL (default: `https://www.qobuz.com/api.json/0.2`)                                                   |
| `QOBUZ_PROXY`         | Residential proxy for cloud hosting — `http://user:pass@host:port` or `socks5://user:pass@host:port`. Leave empty for direct.   |
| `API_KEY`             | If set, every request except `/`, `/docs`, `/openapi.json`, `/redoc` must include header `X-API-Key: <value>`. **Strongly recommended** if the port is reachable from outside localhost. Generate with `openssl rand -hex 32`. |
| `CORS_ORIGINS`        | Comma-separated list of allowed browser origins. Empty (default) disables cross-origin browser access entirely.                 |
| `DOWNLOAD_ROOT`       | Root directory all `output_dir` values are confined to (default: `./downloads`). Requests attempting to escape it (`../`, absolute paths) are rejected with `400`. |
| `LOG_LEVEL`           | Log verbosity: `DEBUG`, `INFO`, `WARNING`, `ERROR` (default: `INFO`)                                                            |
| `DEV_MODE`            | Set to `true` to log full upstream responses at DEBUG level                                                                      |
| `CACHE_TTL_SECONDS`   | TTL in seconds for the in-memory metadata cache (default: `600`). Set to `0` to disable.                                        |

> [!IMPORTANT]
> `QOBUZ_APP_ID` and `QOBUZ_SECRET` must be set manually. APP_ID and SECRET are a matched pair — using mismatched values will cause `400 Invalid Request Signature` errors on all download endpoints.

> [!WARNING]
> This server has no authentication by default. If you expose port 8000 beyond `127.0.0.1` (e.g. on a homelab reachable from your LAN, or a cloud host), set `API_KEY` in `.env` and pass it as the `X-API-Key` header on every request. The bundled `docker-compose.yml` binds to `127.0.0.1:8000` for this reason — change it deliberately if you need broader access.

Start the server with:

```
python main.py
```

Or directly with uvicorn:

```
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

Interactive docs available at **<http://localhost:8000/docs>**

### Docker

A pre-built image is published automatically to the GitHub Container Registry on every push to `main`.

```
docker pull ghcr.io/bartolomeorusso9/qobuz-rest-api:main
```

Run with your token:

```
docker run -d \
  -p 127.0.0.1:8000:8000 \
  -e QOBUZ_TOKEN=your_token_here \
  -e API_KEY=your_generated_api_key \
  -e LOG_LEVEL=INFO \
  ghcr.io/bartolomeorusso9/qobuz-api:main
```

Or with a `.env` file:

```
docker run -d \
  -p 127.0.0.1:8000:8000 \
  --env-file .env \
  ghcr.io/bartolomeorusso9/qobuz-api:main
```

---

## Finding APP_ID and SECRET

APP_ID and SECRET must be extracted manually from the Qobuz web player. They are a matched pair — always use them together.

**1. Get the APP_ID**

Open [play.qobuz.com](https://play.qobuz.com), open DevTools (`F12` / `Cmd+Option+I`), go to the **Network** tab and play any track. Click on a `track/getFileUrl` request — the `app_id` is visible in the query parameters.

**2. Get the SECRET**

In the DevTools **Console**, run:

```js
fetch('https://play.qobuz.com/resources/8.1.0-b019/bundle.js')
  .then(r => r.text())
  .then(js => {
    const secrets = [...new Set([...js.matchAll(/[^a-f0-9]([a-f0-9]{32})[^a-f0-9]/g)].map(m => m[1]))]
    alert('SECRETs:\n' + secrets.slice(0, 5).join('\n'))
  })
```

> [!NOTE]
> The bundle path (`8.1.0-b019`) changes with each Qobuz release. Find the current one by running in the console:
>
> ```js
> performance.getEntriesByType('resource').filter(r => r.name.endsWith('.js') && r.name.includes('/resources/')).map(r => r.name)
> ```

**3. Get the QOBUZ_TOKEN**

In DevTools go to **Application → Local Storage → play.qobuz.com**, or run in the console:

```js
JSON.parse(localStorage.getItem('localuser')).token
```

**4. Test the pair**

Try each APP_ID candidate with the first SECRET until `/download-url/{track_id}` returns `200 OK` instead of `400 Invalid Request Signature`.

---

## API Schema

> [!NOTE]
> If `API_KEY` is set in `.env`, every request below (except `GET /`, `/docs`, `/openapi.json`, `/redoc`) must include header `X-API-Key: <your key>`. Omitted from the examples below for brevity.

### `GET /`

Returns server status, auth state, proxy config and the API base in use. Always public, even when `API_KEY` is set.

#### Response

`200 OK`

```json
{
  "status": "online",
  "version": "2.2.0",
  "docs": "http://localhost:8000/docs",
  "auth": "token active",
  "api_key_required": true,
  "proxy": "direct (no proxy)",
  "qobuz_api_base": "https://www.qobuz.com/api.json/0.2"
}
```

---

### `POST /set-token`

Updates the active token at runtime without restarting the server. The new token takes precedence over `QOBUZ_TOKEN` and `QOBUZ_AUTH_TOKENS` until the server is restarted.

#### Body

```json
{ "token": "your_fresh_token_here" }
```

#### Response

`200 OK`

```json
{ "status": "ok", "token_set": true }
```

---

### `POST /cache/clear`

Clears the in-memory metadata cache (tracks, albums, artists, playlists) immediately, without waiting for TTL expiry.

#### Response

`200 OK`

```json
{ "status": "ok", "entries_removed": 12 }
```

---

### `GET /me`

Returns information about the authenticated user.

#### Response

`200 OK`

```json
{
  "login": "user@example.com",
  "display": "Francesco",
  "email": "user@example.com",
  "plan": "Studio Premier",
  "country": "IT"
}
```

---

### `GET /search`

#### Params

- `q`: `str` (required) — search query
- `type`: `str` (optional, default `tracks`) — `tracks`, `albums`, `artists`, `playlists`
- `limit`: `int` (optional, default `10`, min `1`, max `50`) — number of results

#### Response

`200 OK`

```json
{
  "query": "Francesco Cavestri",
  "albums": {
    "limit": 10,
    "offset": 0,
    "total": 11,
    "items": [
      {
        "id": "em5pzj2fxalfl",
        "title": "Noè",
        "release_date_original": "2026-05-29",
        "duration": 2330,
        "tracks_count": 10,
        "hires_streamable": true,
        "maximum_bit_depth": 24,
        "maximum_sampling_rate": 48,
        "artist": {
          "id": 12951852,
          "name": "Francesco Cavestri"
        }
      }
    ]
  }
}
```

---

### `GET /track/{track_id}`

Returns the full metadata of a track.

```
curl -X 'GET' \
  'http://localhost:8000/track/420232043' \
  -H 'accept: application/json'
```

#### Params

- `track_id`: `str` (required) — Qobuz track ID

#### Response

`200 OK`

```json
{
  "id": 420232043,
  "title": "Omen Of A Sea",
  "isrc": "ITUM72600479",
  "duration": 147,
  "track_number": 1,
  "media_number": 1,
  "version": null,
  "parental_warning": false,
  "hires": true,
  "hires_streamable": true,
  "maximum_bit_depth": 24,
  "maximum_sampling_rate": 48,
  "maximum_channel_count": 2,
  "streamable": true,
  "downloadable": true,
  "purchasable": true,
  "release_date_original": "2026-05-29",
  "copyright": "℗ 2026 Universal Music Italia Srl",
  "performers": "Francesco Cavestri, MainArtist, Composer - ...",
  "audio_info": {
    "replaygain_track_gain": -5.43,
    "replaygain_track_peak": 0.922699
  },
  "performer": { "id": 12951852, "name": "Francesco Cavestri" },
  "composer":  { "id": 12951852, "name": "Francesco Cavestri" },
  "album": {
    "id": "em5pzj2fxalfl",
    "title": "Noè",
    "qobuz_id": 420232042,
    "maximum_bit_depth": 24,
    "maximum_sampling_rate": 48,
    "hires_streamable": true,
    "image": {
      "small": "https://static.qobuz.com/images/covers/fl/al/em5pzj2fxalfl_230.jpg",
      "thumbnail": "https://static.qobuz.com/images/covers/fl/al/em5pzj2fxalfl_50.jpg",
      "large": "https://static.qobuz.com/images/covers/fl/al/em5pzj2fxalfl_600.jpg",
      "back": null
    },
    "artist": { "id": 12951852, "name": "Francesco Cavestri" },
    "label":  { "id": 190126,   "name": "Universal Music Italia srL." },
    "genre":  { "id": 80, "name": "Jazz", "slug": "jazz" }
  }
}
```

---

### `GET /album/{album_id}`

Returns album metadata and full track listing.

```
curl -X 'GET' \
  'http://localhost:8000/album/em5pzj2fxalfl' \
  -H 'accept: application/json'
```

#### Params

- `album_id`: `str` (required) — Qobuz album ID

#### Response

`200 OK`

```json
{
  "id": "em5pzj2fxalfl",
  "title": "Noè",
  "qobuz_id": 420232042,
  "upc": "0600574182104",
  "duration": 2330,
  "tracks_count": 10,
  "media_count": 1,
  "release_date_original": "2026-05-29",
  "hires": true,
  "hires_streamable": true,
  "maximum_bit_depth": 24,
  "maximum_sampling_rate": 48,
  "maximum_channel_count": 2,
  "maximum_technical_specifications": "24 bits / 48.0 kHz - Stereo",
  "streamable": true,
  "downloadable": true,
  "purchasable": true,
  "parental_warning": false,
  "copyright": "© 2026 Universal Music Italia Srl ℗ 2026 Universal Music Italia Srl",
  "image": {
    "small": "https://static.qobuz.com/images/covers/fl/al/em5pzj2fxalfl_230.jpg",
    "thumbnail": "https://static.qobuz.com/images/covers/fl/al/em5pzj2fxalfl_50.jpg",
    "large": "https://static.qobuz.com/images/covers/fl/al/em5pzj2fxalfl_600.jpg",
    "back": null
  },
  "artist": { "id": 12951852, "name": "Francesco Cavestri", "slug": "francesco-cavestri" },
  "label":  { "id": 190126, "name": "Universal Music Italia srL.", "slug": "universalmusicitaliasrl" },
  "genre":  { "id": 80, "name": "Jazz", "slug": "jazz", "color": "#0070ef" },
  "awards": [
    { "name": "Album della settimana Qobuz", "publication_name": "Qobuz", "awarded_at": 1780005600 }
  ],
  "tracks": {
    "offset": 0,
    "limit": 500,
    "total": 10,
    "items": [
      {
        "id": 420232043,
        "title": "Omen Of A Sea",
        "track_number": 1,
        "duration": 147,
        "isrc": "ITUM72600479",
        "hires": true,
        "hires_streamable": true,
        "maximum_bit_depth": 24,
        "maximum_sampling_rate": 48,
        "audio_info": {
          "replaygain_track_gain": -5.43,
          "replaygain_track_peak": 0.922699
        }
      }
    ]
  }
}
```

> [!NOTE]
> `tracks.items` is truncated above. The full response includes all tracks in the album.

---

### `GET /artist/{artist_id}`

Returns artist data and their album catalogue.

```
curl -X 'GET' \
  'http://localhost:8000/artist/12951852?limit=20' \
  -H 'accept: application/json'
```

#### Params

- `artist_id`: `str` (required) — Qobuz artist ID
- `limit`: `int` (optional, default `20`, min `1`, max `100`) — number of albums to return

#### Response

`200 OK`

```json
{
  "id": 12951852,
  "name": "Francesco Cavestri",
  "slug": "francesco-cavestri",
  "albums_count": 11,
  "albums_as_primary_artist_count": 19,
  "albums_as_primary_composer_count": 17,
  "picture": null,
  "image": {
    "small":  "https://static.qobuz.com/images/artists/covers/small/1f60a55e0ed77d46294e4fbc270f36ba.jpg",
    "medium": "https://static.qobuz.com/images/artists/covers/medium/1f60a55e0ed77d46294e4fbc270f36ba.jpg",
    "large":  "https://static.qobuz.com/images/artists/covers/large/1f60a55e0ed77d46294e4fbc270f36ba.jpg"
  },
  "biography": {
    "source": "Qobuz",
    "language": "it",
    "summary": "<p>Nato nel 2003, <strong>Francesco Cavestri</strong> è un pianista...</p>"
  },
  "albums": {
    "total": 11,
    "offset": 0,
    "limit": 20,
    "items": [ "..." ]
  }
}
```

> [!NOTE]
> `albums.items` is truncated above. The full response includes all albums up to the requested `limit`.

---

### `GET /playlist/{playlist_id}`

Returns playlist metadata and its full track listing (up to 500 tracks).

```
curl -X 'GET' \
  'http://localhost:8000/playlist/12345678' \
  -H 'accept: application/json'
```

#### Params

- `playlist_id`: `str` (required) — Qobuz playlist ID

#### Response

`200 OK` — Qobuz playlist object with a `tracks.items` array.

---

### `GET /download-url/{track_id}`

Returns a signed URL ready for download. Used by Spotiflac and similar tools.

#### Params

- `track_id`: `str` (required) — Qobuz track ID
- `quality`: `str` (optional, default `flac`) — see table below

| Value  | Format                       |
| ------ | ---------------------------- |
| `mp3`  | MP3 320 kbps                 |
| `flac` | FLAC 16-bit (CD)             |
| `hi24` | FLAC 24-bit ≤96 kHz          |
| `hi96` | FLAC 24-bit >96 kHz (Hi-Res) |

#### Response

`200 OK`

```json
{
  "track_id": "420232043",
  "quality": "hi24",
  "format_id": 7,
  "mime_type": "audio/flac",
  "bit_depth": 24,
  "sampling_rate": 48.0,
  "file_size": 42831737,
  "url": "https://streaming.qobuz.com/file?uid=...&secret=...&sig=...&expire=1780005600"
}
```

> [!NOTE]
> The signed URL is time-limited. Pass it directly to `ffmpeg`, `httpx`, or any HTTP client — do not store it for later use.

---

### `GET /stream/{track_id}`

Proxies the audio stream directly. Useful for media players that support HTTP sources.

#### Params

- `track_id`: `str` (required) — Qobuz track ID
- `quality`: `str` (optional, default `flac`) — same values as `/download-url/`

#### Response

Binary audio stream with appropriate `Content-Type` header.

---

### `POST /download`

Downloads a track to disk in the background. Returns immediately.

#### Body

```json
{
  "track_id": "420232043",
  "quality": "hi24",
  "target_format": "flac",
  "output_dir": "./downloads",
  "filename_format": "{track:02d} - {title}"
}
```

| Field             | Type   | Default                 | Description                                                   |
| ----------------- | ------ | ------------------------ | --------------------------------------------------------------|
| `track_id`        | `str`  | required                | Qobuz track ID                                                |
| `quality`         | `str`  | `flac`                  | Source quality: `mp3`, `flac`, `hi24`, `hi96`                 |
| `target_format`   | `str`  | same as `quality`       | Output format: `mp3`, `flac`, `alac`, `wav`, `opus` — requires `ffmpeg` for conversion |
| `output_dir`      | `str`  | `./downloads`           | Directory where the file will be saved, resolved relative to `DOWNLOAD_ROOT`. Paths attempting to escape it are rejected. |
| `filename_format` | `str`  | `{track:02d} - {title}` | Filename template — see [Filename format](#filename-format)   |

#### Response

`200 OK`

```json
{
  "status": "downloading",
  "track_id": "420232043",
  "filename": "01 - Omen Of A Sea.flac",
  "output": "./downloads/01 - Omen Of A Sea.flac",
  "quality": "hi24",
  "format": "flac"
}
```

> [!NOTE]
> The download runs in the background. The response is returned immediately — check the output path to verify completion.

---

### `POST /download-album/{album_id}`

Downloads all tracks from an album to disk in the background. Returns immediately. At most **3 tracks** are downloaded concurrently; remaining tracks are queued automatically. The album cover is fetched once and reused across all tracks.

#### Params

- `album_id`: `str` (required) — Qobuz album ID
- `quality`: `str` (optional, default `flac`)
- `output_dir`: `str` (optional, default `./downloads`)
- `filename_format`: `str` (optional, default `{track:02d} - {title}`) — see [Filename format](#filename-format)

#### Response

`200 OK`

```json
{
  "status": "downloading",
  "album": "Noè",
  "artist": "Francesco Cavestri",
  "tracks": 10,
  "output_dir": "./downloads/Francesco Cavestri - Noè",
  "quality": "hi24"
}
```

> [!NOTE]
> All tracks download in the background with a concurrency limit of 3. Progress is tracked in `status.json` inside `output_dir`. Each track entry transitions from `"pending"` → `"done"` (with a `"file"` field) or `"error"` (with an `"error"` field). Check that file to monitor progress or detect failures after a server restart.
>
> ```json
> {
>   "420232043": { "title": "Omen Of A Sea", "status": "done",    "file": "01 - Omen Of A Sea.flac" },
>   "420232044": { "title": "Noè",           "status": "pending"                                    },
>   "420232045": { "title": "...",            "status": "error",   "error": "403 Forbidden"          }
> }
> ```

---

### `GET /album-status/{album_id}`

Returns the download progress for an album by reading `status.json` from disk.

```
curl "http://localhost:8000/album-status/em5pzj2fxalfl?output_dir=./music"
```

#### Params

- `album_id`: `str` (required) — Qobuz album ID
- `output_dir`: `str` (optional, default `./downloads`) — must match the path used in `/download-album`

#### Response

`200 OK`

```json
{
  "album":   "Noè",
  "artist":  "Francesco Cavestri",
  "done":    7,
  "pending": 2,
  "errors":  1,
  "tracks": {
    "420232043": { "title": "Omen Of A Sea", "status": "done",  "file": "01 - Omen Of A Sea.flac" },
    "420232044": { "title": "Noè",           "status": "error", "error": "403 Forbidden"           }
  }
}
```

`404 Not Found` if no `status.json` exists for that album (download not yet started).

---

### `GET /downloads`

Lists every download tracked under `output_dir` (albums and playlists) along with a quick progress summary, without needing to know exact artist/album/playlist names.

```
curl "http://localhost:8000/downloads?output_dir=./music"
```

#### Params

- `output_dir`: `str` (optional, default `./downloads`)

#### Response

`200 OK`

```json
{
  "output_dir": "/app/downloads",
  "items": [
    { "name": "Francesco Cavestri - Noè", "path": "/app/downloads/Francesco Cavestri - Noè", "done": 10, "pending": 0, "errors": 0, "total": 10 },
    { "name": "My Playlist", "path": "/app/downloads/My Playlist", "done": 20, "pending": 4, "errors": 1, "total": 25 }
  ]
}
```

---

### `POST /download-playlist/{playlist_id}`

Downloads all tracks from a Qobuz playlist to disk in the background. Returns immediately. Respects the same 3-track concurrency limit as album downloads, and reuses a single fetch of each track's cover art where possible.

#### Params

- `playlist_id`: `str` (required) — Qobuz playlist ID
- `quality`: `str` (optional, default `flac`)
- `output_dir`: `str` (optional, default `./downloads`)
- `filename_format`: `str` (optional, default `{track:02d} - {title}`) — see [Filename format](#filename-format)

#### Response

`200 OK`

```json
{
  "status": "downloading",
  "playlist": "My Playlist",
  "tracks": 25,
  "output_dir": "./downloads/My Playlist",
  "quality": "flac"
}
```

Progress is tracked in `status.json` inside `output_dir`, same format as album downloads.

---

### `GET /playlist-status/{playlist_id}`

Returns the download progress for a playlist by reading `status.json` from disk.

```
curl "http://localhost:8000/playlist-status/12345678?output_dir=./music"
```

#### Params

- `playlist_id`: `str` (required) — Qobuz playlist ID
- `output_dir`: `str` (optional, default `./downloads`) — must match the path used in `/download-playlist`

#### Response

`200 OK`

```json
{
  "playlist": "My Playlist",
  "done":    20,
  "pending":  4,
  "errors":   1,
  "tracks": { "...": "..." }
}
```

`404 Not Found` if no `status.json` exists (download not yet started).

---

### `GET /export-album/{album_id}`

Packages an already-downloaded album folder into a `.zip` file and returns it for direct download. The album must have been fully downloaded first — the endpoint returns `400` if any tracks are still pending.

```
curl "http://localhost:8000/export-album/em5pzj2fxalfl?output_dir=./downloads" \
  --output "Francesco Cavestri - Noè.zip"
```

#### Params

- `album_id`: `str` (required) — Qobuz album ID
- `output_dir`: `str` (optional, default `./downloads`) — must match the path used in `/download-album`

#### Response

Binary `application/zip` file on `200 OK`.

`400 Bad Request` if downloads are still in progress.
`404 Not Found` if no `status.json` is found for the album.

---

## Filename format

The `filename_format` parameter accepts a Python format string with the following placeholders:

| Placeholder   | Value                          |
| ------------- | ------------------------------ |
| `{track}`     | Track number (e.g. `1`)        |
| `{title}`     | Track title                    |
| `{artist}`    | Performer name                 |
| `{album}`     | Album title                    |
| `{year}`      | Release year (e.g. `2026`)     |
| `{date}`      | Full release date (e.g. `2026-05-29`) |
| `{disc}`      | Disc number                    |
| `{isrc}`      | ISRC code                      |
| `{genre}`     | Genre name                     |

Examples:

```
{track:02d} - {title}            →  01 - Omen Of A Sea.flac
{artist} - {title}               →  Francesco Cavestri - Omen Of A Sea.flac
{disc}-{track:02d} {title}       →  1-01 Omen Of A Sea.flac
{year} - {album}/{track:02d} {title}  →  2026 - Noè/01 Omen Of A Sea.flac
```

---

## Usage Examples

### Python

```python
import requests

BASE = "http://localhost:8000"
HEADERS = {"X-API-Key": "your_generated_api_key"}  # omit if API_KEY isn't set

# Update token at runtime
requests.post(f"{BASE}/set-token", json={"token": "your_fresh_token"}, headers=HEADERS)

# Check authenticated user
me = requests.get(f"{BASE}/me", headers=HEADERS).json()

# Search for an album
results = requests.get(f"{BASE}/search", params={"q": "Francesco Cavestri", "type": "albums"}, headers=HEADERS).json()

# Get a signed download URL
url_info = requests.get(f"{BASE}/download-url/420232043", params={"quality": "hi24"}, headers=HEADERS).json()
print(url_info["url"])  # pass directly to ffmpeg or httpx

# Download a single track with format conversion
requests.post(f"{BASE}/download", json={
    "track_id": "420232043",
    "quality": "hi24",
    "target_format": "alac",
    "output_dir": "./music",
    "filename_format": "{track:02d} - {title}",
}, headers=HEADERS)

# Download a full album
requests.post(f"{BASE}/download-album/em5pzj2fxalfl", params={
    "quality": "hi24",
    "output_dir": "./music",
}, headers=HEADERS)

# Poll album download status
status = requests.get(f"{BASE}/album-status/em5pzj2fxalfl", params={"output_dir": "./music"}, headers=HEADERS).json()
print(f"{status['done']}/{status['done'] + status['pending']} tracks done")
```

### curl

```bash
# Check status and auth (always public)
curl "http://localhost:8000/"

# All other requests need -H "X-API-Key: your_generated_api_key" if API_KEY is set

# Set token at runtime
curl -X POST http://localhost:8000/set-token \
  -H "Content-Type: application/json" \
  -H "X-API-Key: your_generated_api_key" \
  -d '{"token":"your_fresh_token"}'

# Authenticated user info
curl "http://localhost:8000/me" -H "X-API-Key: your_generated_api_key"

# Search
curl "http://localhost:8000/search?q=Francesco+Cavestri&type=albums" -H "X-API-Key: your_generated_api_key"

# Track metadata
curl "http://localhost:8000/track/420232043" -H "X-API-Key: your_generated_api_key"

# Album metadata
curl "http://localhost:8000/album/em5pzj2fxalfl" -H "X-API-Key: your_generated_api_key"

# Artist with albums
curl "http://localhost:8000/artist/12951852?limit=20" -H "X-API-Key: your_generated_api_key"

# Playlist metadata
curl "http://localhost:8000/playlist/12345678" -H "X-API-Key: your_generated_api_key"

# Download URL
curl "http://localhost:8000/download-url/420232043?quality=hi24" -H "X-API-Key: your_generated_api_key"

# Download track to disk (with custom filename template and format conversion)
curl -X POST http://localhost:8000/download \
  -H "Content-Type: application/json" \
  -H "X-API-Key: your_generated_api_key" \
  -d '{
    "track_id": "420232043",
    "quality": "hi24",
    "target_format": "alac",
    "output_dir": "./music",
    "filename_format": "{track:02d} - {title}"
  }'

# Download full album to disk
curl -X POST "http://localhost:8000/download-album/em5pzj2fxalfl?quality=hi24&output_dir=./music" \
  -H "X-API-Key: your_generated_api_key"

# Check album download progress
curl "http://localhost:8000/album-status/em5pzj2fxalfl?output_dir=./music" -H "X-API-Key: your_generated_api_key"

# List all tracked downloads
curl "http://localhost:8000/downloads?output_dir=./music" -H "X-API-Key: your_generated_api_key"

# Download full playlist to disk
curl -X POST "http://localhost:8000/download-playlist/12345678?quality=flac&output_dir=./music" \
  -H "X-API-Key: your_generated_api_key"

# Check playlist download progress
curl "http://localhost:8000/playlist-status/12345678?output_dir=./music" -H "X-API-Key: your_generated_api_key"

# Clear the metadata cache
curl -X POST "http://localhost:8000/cache/clear" -H "X-API-Key: your_generated_api_key"

# Export downloaded album as ZIP
curl "http://localhost:8000/export-album/em5pzj2fxalfl?output_dir=./music" \
  -H "X-API-Key: your_generated_api_key" \
  --output "Francesco Cavestri - Noè.zip"
```
