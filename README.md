# qobuz-rest-api

> [!IMPORTANT]
> Music piracy is illegal in most countries. This project is intended for use with a valid Qobuz subscription for personal/educational purposes (e.g. in your homelab). Use responsibly.

---

## Setup

Run `pip install -r requirements.txt`, then configure your `.env` file:

```bash
cp .env.example .env
```

| Variable | Description |
|---|---|
| `QOBUZ_APP_ID` | Qobuz App ID — see [Finding APP_ID and SECRET](#finding-app_id-and-secret) |
| `QOBUZ_SECRET` | Qobuz Secret — see [Finding APP_ID and SECRET](#finding-app_id-and-secret) |
| `QOBUZ_TOKEN` | User token → localStorage of [play.qobuz.com](https://play.qobuz.com), key `localuser.token` |
| `LOG_LEVEL` | Log verbosity: `DEBUG`, `INFO`, `WARNING`, `ERROR` (default: `INFO`) |

> [!IMPORTANT]
> `QOBUZ_APP_ID` and `QOBUZ_SECRET` must be set manually. APP_ID and SECRET are a matched pair — using mismatched values will cause `400 Invalid Request Signature` errors on all download endpoints.

Start the server with:

```bash
python main.py
```

Or directly with uvicorn:

```bash
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

Interactive docs available at **http://localhost:8000/docs**

### Docker

A pre-built image is published automatically to the GitHub Container Registry on every push to `main`.

```bash
docker pull ghcr.io/bartolomeorusso9/qobuz-api:main
```

Run with your token:

```bash
docker run -d \
  -p 8000:8000 \
  -e QOBUZ_TOKEN=your_token_here \
  -e LOG_LEVEL=INFO \
  ghcr.io/bartolomeorusso9/qobuz-api:main
```

Or with a `.env` file:

```bash
docker run -d \
  -p 8000:8000 \
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

```javascript
fetch('https://play.qobuz.com/resources/8.1.0-b019/bundle.js')
  .then(r => r.text())
  .then(js => {
    const secrets = [...new Set([...js.matchAll(/[^a-f0-9]([a-f0-9]{32})[^a-f0-9]/g)].map(m => m[1]))]
    alert('SECRETs:\n' + secrets.slice(0, 5).join('\n'))
  })
```

> [!NOTE]
> The bundle path (`8.1.0-b019`) changes with each Qobuz release. Find the current one by running in the console:
> ```javascript
> performance.getEntriesByType('resource').filter(r => r.name.endsWith('.js') && r.name.includes('/resources/')).map(r => r.name)
> ```

**3. Get the QOBUZ_TOKEN**

In DevTools go to **Application → Local Storage → play.qobuz.com**, or run in the console:

```javascript
JSON.parse(localStorage.getItem('localuser')).token
```

**4. Test the pair**

Try each APP_ID candidate with the first SECRET until `/download-url/{track_id}` returns `200 OK` instead of `400 Invalid Request Signature`.

---

## API Schema

### `GET /`

Returns server status and available endpoints.

#### Response

`200 OK`

```json
{
  "status": "online",
  "version": "2.1.0",
  "docs": "http://localhost:8000/docs",
  "endpoints": [
    "/search", "/track/{id}", "/album/{id}",
    "/artist/{id}", "/download-url/{track_id}",
    "/stream/{track_id}", "/download", "/download-album/{album_id}"
  ]
}
```

---

### `GET /search`

#### Params

- `q`: `str` (required) — search query
- `type`: `str` (optional, default `tracks`) — `tracks`, `albums`, `artists`
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

```bash
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
  "performers": "Francesco Cavestri, MainArtist, Composer - Luca Mattioni, ComputermusicProgrammer, Producer - Giuseppe Salvadori, ComputermusicProgrammer, MixingEngineer, MasteringEngineer",
  "audio_info": {
    "replaygain_track_gain": -5.43,
    "replaygain_track_peak": 0.922699
  },
  "performer": {
    "id": 12951852,
    "name": "Francesco Cavestri"
  },
  "composer": {
    "id": 12951852,
    "name": "Francesco Cavestri"
  },
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
    "artist": {
      "id": 12951852,
      "name": "Francesco Cavestri"
    },
    "label": {
      "id": 190126,
      "name": "Universal Music Italia srL."
    },
    "genre": {
      "id": 80,
      "name": "Jazz",
      "slug": "jazz"
    }
  }
}
```

---

### `GET /album/{album_id}`

Returns album metadata and full track listing.

```bash
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
  "artist": {
    "id": 12951852,
    "name": "Francesco Cavestri",
    "slug": "francesco-cavestri"
  },
  "artists": [
    { "id": 12951852, "name": "Francesco Cavestri", "roles": ["main-artist"] }
  ],
  "label": {
    "id": 190126,
    "name": "Universal Music Italia srL.",
    "slug": "universalmusicitaliasrl"
  },
  "genre": {
    "id": 80,
    "name": "Jazz",
    "slug": "jazz",
    "color": "#0070ef"
  },
  "awards": [
    {
      "name": "Album della settimana Qobuz",
      "publication_name": "Qobuz",
      "awarded_at": 1780005600
    }
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
      },
      {
        "id": 420232044,
        "title": "Noè",
        "track_number": 2,
        "duration": 337,
        "isrc": "ITUM72600480",
        "hires": true,
        "hires_streamable": true,
        "maximum_bit_depth": 24,
        "maximum_sampling_rate": 48,
        "audio_info": {
          "replaygain_track_gain": -4.14,
          "replaygain_track_peak": 0.942474
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

```bash
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
    "small": "https://static.qobuz.com/images/artists/covers/small/1f60a55e0ed77d46294e4fbc270f36ba.jpg",
    "medium": "https://static.qobuz.com/images/artists/covers/medium/1f60a55e0ed77d46294e4fbc270f36ba.jpg",
    "large": "https://static.qobuz.com/images/artists/covers/large/1f60a55e0ed77d46294e4fbc270f36ba.jpg"
  },
  "biography": {
    "source": "Qobuz",
    "language": "it",
    "summary": "<p>Nato nel 2003, <strong>Francesco Cavestri</strong> è un pianista, compositore..."
  },
  "albums": {
    "total": 11,
    "offset": 0,
    "limit": 20,
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
        "genre": { "id": 80, "name": "Jazz" },
        "label": { "id": 190126, "name": "Universal Music Italia srL." },
        "image": {
          "thumbnail": "https://static.qobuz.com/images/covers/fl/al/em5pzj2fxalfl_50.jpg",
          "large": "https://static.qobuz.com/images/covers/fl/al/em5pzj2fxalfl_600.jpg"
        }
      },
      {
        "id": "bp2sbqf7r7jnb",
        "title": "IKI - Bellezza Ispiratrice",
        "release_date_original": "2024-01-19",
        "duration": 1859,
        "tracks_count": 6,
        "hires_streamable": true,
        "maximum_bit_depth": 24,
        "maximum_sampling_rate": 44.1,
        "genre": { "id": 80, "name": "Jazz" },
        "label": { "id": 190126, "name": "Universal Music Italia srL." }
      }
    ]
  }
}
```

> [!NOTE]
> `albums.items` is truncated above. The full response includes all albums up to the requested `limit`.

---

### `GET /download-url/{track_id}`

Returns a signed URL ready for download. Used by Spotiflac and similar tools.

#### Params

- `track_id`: `str` (required) — Qobuz track ID
- `quality`: `str` (optional, default `flac`) — see table below

| Value | Format |
|---|---|
| `mp3` | MP3 320kbps |
| `flac` | FLAC 16-bit (CD) |
| `hi24` | FLAC 24-bit ≤96kHz |
| `hi96` | FLAC 24-bit >96kHz (Hi-Res) |

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
  "output_dir": "./downloads"
}
```

#### Response

`200 OK`

```json
{
  "status": "downloading",
  "track_id": "420232043",
  "filename": "Francesco Cavestri - Omen Of A Sea.flac",
  "output": "./downloads/Francesco Cavestri - Omen Of A Sea.flac",
  "quality": "hi24"
}
```

> [!NOTE]
> The download runs in the background. The response is returned immediately — check the output path to verify completion.

---

### `POST /download-album/{album_id}`

Downloads all tracks from an album to disk in the background. Returns immediately. At most **3 tracks** are downloaded concurrently; remaining tracks are queued automatically.

#### Params

- `album_id`: `str` (required) — Qobuz album ID
- `quality`: `str` (optional, default `flac`)
- `output_dir`: `str` (optional, default `./downloads`)

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

```json
{
  "420232043": { "title": "Omen Of A Sea", "status": "done",    "file": "01 - Omen Of A Sea.flac" },
  "420232044": { "title": "Noè",           "status": "pending"                                    },
  "420232045": { "title": "...",            "status": "error",   "error": "403 Forbidden"          }
}
```

---

### `GET /album-status/{album_id}`

Returns the download progress for an album by reading `status.json` from disk.

```bash
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

## Usage Examples

### Python

```python
import requests

BASE = "http://localhost:8000"

# Search for an album
results = requests.get(f"{BASE}/search", params={"q": "Francesco Cavestri", "type": "albums"}).json()

# Get a signed download URL
url_info = requests.get(f"{BASE}/download-url/420232043", params={"quality": "hi24"}).json()
print(url_info["url"])  # pass directly to ffmpeg or httpx
```

### curl

```bash
# Search
curl "http://localhost:8000/search?q=Francesco+Cavestri&type=albums"

# Track metadata
curl "http://localhost:8000/track/420232043"

# Album metadata
curl "http://localhost:8000/album/em5pzj2fxalfl"

# Artist with albums
curl "http://localhost:8000/artist/12951852?limit=20"

# Download URL
curl "http://localhost:8000/download-url/420232043?quality=hi24"

# Download track to disk
curl -X POST http://localhost:8000/download \
  -H "Content-Type: application/json" \
  -d '{"track_id":"420232043","quality":"hi24","output_dir":"./music"}'

# Download full album to disk
curl -X POST "http://localhost:8000/download-album/em5pzj2fxalfl?quality=hi24&output_dir=./music"

# Check album download progress
curl "http://localhost:8000/album-status/em5pzj2fxalfl?output_dir=./music"
```