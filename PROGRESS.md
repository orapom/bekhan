# بخوان — PROGRESS.md

Personal media archive. Single-user. RTL Persian UI. FastAPI + Celery + SQLite + Redis. No local Whisper.

---

## Stack

| Layer | Tech |
|-------|------|
| API | FastAPI (Python 3.11), port 8001 |
| Worker | Celery 5.x, Redis broker |
| DB | SQLite (WAL mode), `/data/bekhan.db` |
| Frontend | Single-file `/frontend/index.html`, no build step, RTL dark/light |
| AI | ArvanCloud OpenAI-compatible gateway |
| Docker | `FROM jost-api:latest` (DockerHub blocked — reuses existing image) |

---

## ✅ Done

### Infrastructure
- [x] Docker Compose: `redis`, `api`, `worker`, `flower` services
- [x] Port `8001:8000` (avoids conflict with jost-api on 8000)
- [x] `jost-api:latest` base image with ffmpeg, yt-dlp, python packages
- [x] `yt-dlp` upgraded to latest at build time (`pip install --upgrade yt-dlp`)
- [x] `/data/` volume for DB + media + audio + model config
- [x] `.env` / `.apikey` mounted read-only at `/secrets/`
- [x] `BEKHAN_SECRET` env var — optional static key auth on write endpoints
- [x] WAL mode SQLite + `PRAGMA foreign_keys=ON`

### Database (`db.py`)
- [x] `items` table: id, type, source, external_id, title, title_fa, url_source, url_thumbnail, file_path, duration_sec, date_published, language, collections_json, tags_json, status, error_msg
- [x] `transcript_segments`: item_id, language, seg_index, start_sec, end_sec, text, words_json
- [x] `ai_content`: item_id, content_type, language, content, model_id
- [x] `pipeline_state`: item_id, step, language, status, started_at, done_at, error_msg, **model_used**
- [x] Live migration: `ALTER TABLE pipeline_state ADD COLUMN model_used TEXT` (try/except — safe on re-run)
- [x] `get_pipeline_stats()`: per-step done count, avg_sec, last model used
- [x] `get_pipeline_progress(item_id)`: per-step detail with computed `duration_sec`

### AI Client (`ai_client.py`)
- [x] `_last_model: dict` — module-level, tracks last successful model
- [x] `get_last_model()` — returns name of model that last succeeded
- [x] `_model_config()` — loads `/data/model_config.json` at runtime, falls back to defaults
- [x] LLM fallback chain (order from config or default):
  - DeepSeek-V3.2 → DeepSeek-V3.1 → GLM-4.6 → Claude-Haiku-4.5 → Qwen3-30B-A3B → Gemini-Flash
- [x] ASR fallback chain (API-only, **no local Whisper**):
  - GPT-4o-Transcribe → Whisper-1 → Xerxes-1
- [x] Audio chunking: ffmpeg silence detection → split at silence boundaries, 19 MB max per chunk, 3s overlap
- [x] LLM tasks: `correct_transcript_segments`, `generate_book_paragraphs`, `summarize_fa`, `extract_mentions`, `generate_infographic`, `mark_sacred_segments`, `mark_external_quotes`, `generate_artwork`

### Pipeline (`tasks.py`)
Order: `import → transcribe → correct → paragraphs → summarize → mentions → infographic → sacred → quotes → artwork → done`

- [x] `import_url`: yt-dlp metadata + audio download + subtitle extraction (→ transcript if available)
- [x] `import_upload`: ffmpeg video→audio extraction, ffprobe duration
- [x] `transcribe_item`: chunked ASR via fallback chain, stores `asr_model` in pipeline_state
- [x] `correct_transcript`: LLM corrects segments in batches (Persian/Arabic text normalization)
- [x] `generate_paragraphs`: segments → logical book-mode paragraphs with timestamps
- [x] `summarize_item`: full-text summary + main_theme
- [x] `extract_mentions_task`: people, places, books, orgs mentioned
- [x] `generate_infographic_task`: key stats / visual data points for UI card
- [x] `mark_sacred_segments_task`: Quran + Hadith + sacred text detection (green highlight)
- [x] `mark_external_quotes_task`: external quotes detection (orange highlight)
- [x] `generate_artwork_task`: generate SVG/HTML artwork using thumbnail + summary
- [x] Each step stores `model_used` in pipeline_state

### API (`api.py`)
- [x] `POST /api/import` — import URL (YouTube/Aparat/direct)
- [x] `POST /api/upload` — upload file
- [x] `GET /api/items` — list items (filter: source, status, collection, tag)
- [x] `GET /api/items/{id}` — item detail
- [x] `DELETE /api/items/{id}` — delete item + files
- [x] `POST /api/items/{id}/retranscribe` — force re-transcribe
- [x] `GET /api/items/{id}/transcript` — segments with start/end/text
- [x] `GET /api/items/{id}/progress` — per-step pipeline detail (status, duration_sec, model_used)
- [x] `GET /api/items/{id}/content/{type}` — ai_content by type
- [x] `GET /api/search` — full-text search across segments + titles
- [x] `GET /api/admin/stats` — pipeline stats (counts, avg_sec per step, last model per step)
- [x] `GET /api/admin/model-config` — current model order + available model list
- [x] `POST /api/admin/model-config` — save new model order to `/data/model_config.json`
- [x] `POST /api/admin/test-model` — test models: `{category, models, prompt}` → `{results: [{model, latency_sec, snippet, ok}]}`
- [x] Static file serving: `/` → `index.html`, `/media/`, `/audio/`

### Frontend (`frontend/index.html`)
Single file, ~1900 lines, RTL, Persian, dark/light theme, no build step.

**Views:**
- [x] کتابخانه (Library) — card grid, search, filter by source/status/collection/tag
- [x] نمایش (Viewer) — karaoke sync + book mode; YouTube IFrame API; Aparat HLS via hls.js
- [x] جستجو (Search) — full-text, search history (DOM API, no XSS)
- [x] مدیریت (Admin) — pipeline stats, model config editor, model test UI

**Transcript display:**
- [x] Karaoke mode: highlight current segment by time
- [x] Book mode: paragraph view with click-to-seek
- [x] Sacred segments: green background + Arabic font
- [x] External quotes: orange background
- [x] YouTube karaoke via `setInterval` polling (IFrame API)
- [x] Aparat HLS via `hls.js` CDN; `timeupdate` event sync

**Admin — pipeline monitoring:**
- [x] Per-step bar showing: done count, avg duration (e.g. `~42s`), last model used
- [x] Expandable per-item step detail (≡ button) — table: step, status, duration_sec, model_used
- [x] Persian step labels via `STEP_NAMES` const

**Admin — model config:**
- [x] Per-category (asr/llm/image) ordered list of models
- [x] Drag-free reorder (▲▼ buttons), add, remove
- [x] Save to backend (`POST /api/admin/model-config`)

**Admin — model test:**
- [x] Select category + pick models from available list
- [x] Enter test prompt; run → results table: model, latency, snippet, pass/fail

**Bug fixes shipped:**
- [x] `source` defaulting to 'upload' for YouTube URLs — fixed by calling `detect_source(url)` before `upsert_item` in `import_item()`
- [x] `external_id` null before pipeline runs → `_ytVideoId()` fallback parses `url_source` URL params
- [x] XSS in search history `onclick` — rewritten using DOM API
- [x] DockerHub 403 — switched to `FROM jost-api:latest`
- [x] yt-dlp 2024.9.27 too old (YouTube player extraction broken) — upgraded at build time
- [x] Port 8000 conflict with jost-api — changed to 8001:8000

---

## ✅ Done (session 2 — this session)

### Parallel Transcription + Progress
- [x] `progress_pct INTEGER` column on `pipeline_state` (+ live migration)
- [x] `transcribe_audio()` processes chunks in parallel (`asyncio.gather` + `Semaphore(4)`)
- [x] `on_progress(done, total)` callback updates `progress_pct` in DB per chunk
- [x] Item title passed as `prompt` to ASR for better accuracy + `timestamp_granularities[]=word` attempted
- [x] Media page shows per-step progress bars (`#pl-proc`) while pipeline runs; 2s polling; hides when done

### Dual-Model Transcription (optional, off by default)
- [x] `transcribe_audio_dual()` — runs top-2 ASR models per chunk in parallel, LLM merges
- [x] Enable via `"asr_dual": true` in `/data/model_config.json`

### Better Summary
- [x] `summarize_fa()` prompt: half-page narrative (~150-300 words), conversational, no bullet points

### Configurable Prompts
- [x] `_load_prompts()` in `ai_client.py` — loads `/data/prompts_config.json`
- [x] `correct`, `paragraphs`, `summarize` all check for custom instruction overrides
- [x] `GET /api/admin/prompt-config` + `POST /api/admin/prompt-config`
- [x] Admin UI: textarea per task (summary, correct, paragraphs, sacred, quotes, mentions)

### Chapter Markers on Player
- [x] `#ch-strip` — clickable chapter markers at correct timeline positions (RTL-aware %)
- [x] Doubles as progress indicator (gold overlay updates with `_onTick`)
- [x] Current chapter highlighted in sidebar list while playing

### Topic % Normalization
- [x] Weights normalized to actual % (sum → 100%) instead of N/5

---

---

## ✅ Done (session 3)

### ✅ Speaker Diarization

Neither GPT-4o-Transcribe nor Whisper-1 returns speaker labels. LLM post-processing step after `correct`.

- [x] DB column `speaker TEXT` in `transcript_segments` (migration from session 2)
- [x] `ai_client.py`: `diarize_speakers(segments, title)` — batches of 80 segs to LLM, returns `{is_multi_speaker, assignments, names}`
- [x] `tasks.py`: `diarize_item` task after `correct_transcript`, before `generate_paragraphs`; saves speaker assignments to DB + `speakers` ai_content
- [x] `api.py`: transcript endpoint returns `speaker` field; `GET /api/items/{id}/speakers` endpoint; `diarize_item` in all 3 pipeline chains; `diarize` in pipeline stats
- [x] Frontend: speaker chips (A=blue/gold, B=teal, C=orange, D=purple); speaker legend at top of karaoke when multi-speaker; chip shows only on speaker change

### YouTube Network Block
- YouTube SSL `UNEXPECTED_EOF_WHILE_READING` from Docker container — likely network/VPN issue on this machine
- yt-dlp itself is up to date (2026.06.x); issue is network-level
- Workaround: manually import YouTube audio by providing direct audio URL, or use VPN
- **Deferred** by user

### Chrome Extension
- Original plan: detect media on any page → send to bekhan
- Deferred until core bekhan is stable
- Planned: browser extension that POSTs URL to `/api/import` with BEKHAN_SECRET header

### Multi-language Transcript
- DB supports `language` column on `transcript_segments` — can store `fa` + `en` separately
- UI only shows `fa` currently
- No translation task in pipeline yet

### Playlist / Bulk Import
- No bulk import from YouTube channel or Aparat profile
- Would use yt-dlp `--flat-playlist` to enumerate, then queue each URL

---

## 🔴 Not Done / Pending

### ✅ Collections & Tags Management UI (session 3 — done)
- [x] Viewer: editable chips row — collections (green) + tags (blue); × to remove, input + Enter to add; ✎ toggle edit mode
- [x] `PATCH /api/items/{id}` used for all changes (already existed)
- [x] Admin: مجموعه‌ها and برچسب‌ها grids with per-item counts; click → filter library
- [x] `STEP_NAMES` now includes `diarize → گویندگان`

### Search — Semantic / AI Q&A
- Current search: SQLite `LIKE` fulltext on segments + titles
- Planned: embedding-based vector search + "ask a question about this media" feature

### Aparat Subtitles
- yt-dlp fetches Aparat subtitles if available
- No manual subtitle upload UI

---

## Known Issues

| Issue | Status |
|-------|--------|
| YouTube network blocked in container | Deferred — user will fix VPN |
| `file_path` set to audio even for uploaded video files | Minor — doesn't break playback |
| Artwork task silently sets status=done even if artwork fails | Acceptable — final step |
| No retry UI for individual pipeline steps (only full retranscribe) | Future |
| Flower dashboard has no auth | Local-only, port 5556 |

---

## File Map

```
bekhan/
├── backend/
│   ├── Dockerfile          # FROM jost-api:latest; pip install -r requirements.txt --upgrade yt-dlp
│   ├── config.py           # Env vars: DB_PATH, REDIS_URL, AUDIO_DIR, MEDIA_DIR, API_URLS_FILE, etc.
│   ├── db.py               # SQLite schema, helpers, pipeline_state tracking
│   ├── ai_client.py        # LLM/ASR fallback chains, model config loader, last-model tracker
│   ├── tasks.py            # Celery pipeline tasks (import → ... → artwork)
│   ├── api.py              # FastAPI endpoints, admin routes, model config/test
│   ├── media_import.py     # yt-dlp wrappers, subtitle parsing, detect_source()
│   └── requirements.txt
├── frontend/
│   └── index.html          # Single-file SPA, ~1900 lines, RTL Persian
├── data/                   # Volume: bekhan.db, media/, audio/, model_config.json
├── docker-compose.yml      # redis + api + worker + flower
├── Makefile                # make up / make logs / make shell / make rebuild
├── .env                    # API URLs (ArvanCloud gateway endpoints per model)
├── .apikey                 # Single-line API key
└── .gitignore              # Excludes .env, .apikey, data/
```

---

## Run

```bash
# Start (first time builds image)
docker compose up -d --build

# Logs
docker compose logs -f api worker

# Access
# App:    http://localhost:8001
# Flower: http://localhost:5556/flower

# Rebuild after code changes
docker compose up -d --build api worker
```

---

## Model Config

Edit `/data/model_config.json` or use the Admin UI:

```json
{
  "llm": ["DeepSeek-V3.2", "DeepSeek-V3.1", "GLM-4.6", "Claude-Haiku-4.5"],
  "asr": ["GPT-4o-Transcribe", "Whisper-1", "Xerxes-1"],
  "image": ["Gemini-2.0-Flash-Image"]
}
```

Models tried in order; first success wins. `model_used` stored per pipeline step in DB.

---

## Environment (.env format)

```
LLM_URL=https://...arvancloud.../v1
LLM_MODEL=DeepSeek-V3.2
EMBED_URL=...
EMBED_MODEL=text-embedding-3-small
IMAGE_URL=...
IMAGE_MODEL=gemini-2.0-flash-image
MODEL_URLS={"DeepSeek-V3.2": "...", "Whisper-1": "...", ...}
```

API key goes in `.apikey` (single line).


added extra todos:

for some videos i want to use both transcribe models and then merge the result with ai to get a better result, for medias with lower quality or when a person isnt promouncing everytging very good.

for testing models, i want you to create a comperhensive test, run a sample 10 minute video from aparat like this:
https://www.aparat.com/v/fgk23zg
and compare transcribe with each model, or both models, then other parts like summary and stuff, everything 
i want to see comparison in speed and quality, use best model you have for evaluating quality

i think maybe event different configurations for models should be tested, for transcribe, there are alot of parameters i think, i want to use best params for best result in transcribe, even use both models if needed. i want to know what are downsides of using better params


i would like to see progress for different tasks in processing, percentage per task if possible, i would like transcribtions to be in batches and in parallel to be faster. small chunks, parallel, and show progress in both admin page and the media page. crop on smart places if possible. like in silences. i want to see progress of everything and the models used in both media page and admin page very clearly

i would also like sectioning of video and audios like in youtube. its already present in سرفصل ها but i want to see the same thing on player also.

i also want to see what percentage of the media is about each topic


summary should be really summaty not an intro. like a half pager like someone is telling me what is in the media


prompts shpuld be configurable in admin panel too