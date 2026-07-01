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
- [x] `/data/` volume for DB + media + audio + model/prompt/chunking config
- [x] `.env` / `.apikey` mounted read-only at `/secrets/`
- [x] `BEKHAN_SECRET` env var — optional static key auth on write endpoints
- [x] WAL mode SQLite + `PRAGMA foreign_keys=ON`
- [x] `backup.py` — hot DB backup via `sqlite3.Connection.backup()` + optional media tar.gz, `--keep N` retention; `make backup` / `make backup-full`

### Database (`db.py`)
- [x] `items` table: id, type, source, external_id, title, title_fa, url_source, url_thumbnail, file_path, duration_sec, date_published, language, collections_json, tags_json, status, error_msg, **preferred_quality**
- [x] `transcript_segments`: item_id, language, seg_index, start_sec, end_sec, text, words_json, **speaker**
- [x] `ai_content`: item_id, content_type, language, content, model_id
- [x] `pipeline_state`: item_id, step, language, status, started_at, done_at, error_msg, model_used, **progress_pct**
- [x] Live migrations via try/except `ALTER TABLE` — safe on re-run
- [x] `get_pipeline_stats()` / `get_pipeline_progress(item_id)`

### AI Client (`ai_client.py`)
- [x] `_model_config()` — loads `/data/model_config.json` at runtime, falls back to defaults
- [x] LLM fallback chain: DeepSeek-V3.2 → DeepSeek-V3.1 → GLM-4.6 → Claude-Haiku-4.5 → Qwen3-30B-A3B → Gemini-Flash
- [x] ASR fallback chain (API-only, no local Whisper): GPT-4o-Transcribe → Whisper-1 → Xerxes-1 (configurable order)
- [x] Optional dual-ASR mode: run 2 models per chunk in parallel, LLM merges (`asr_dual` flag)
- [x] Audio chunking: split large files at silence boundaries before parallel transcription (see "Audio Chunking" below — reworked this session)
- [x] Configurable per-task prompts via `/data/prompts_config.json` (`_load_prompts()`)
- [x] LLM tasks: `correct_transcript_segments`, `generate_book_paragraphs`, `summarize_fa`, `extract_mentions`, `generate_infographic`, `mark_sacred_segments`, `mark_external_quotes`, `generate_artwork`, `diarize_speakers`
- [x] `raise_on_fail` diagnostic mode + `temperature` param on ASR calls, for the admin test lab
- [x] Per-model-shape multipart fallback in `_call_asr` (some gateway routes reject a redundant `model` form field — tries every param combo with and without it)

### Pipeline (`tasks.py`)
Order: `import → transcribe → correct → diarize → paragraphs → summarize → mentions → infographic → sacred → quotes → artwork → done` (English translation step removed per user request — see Session 6)

- [x] `import_url` / `import_upload`: yt-dlp or ffmpeg extraction, subtitle passthrough if available
- [x] `transcribe_item`: parallel chunked ASR, granular `progress_pct`
- [x] `correct_transcript`, `diarize_item`, `generate_paragraphs`, `summarize_item`, `extract_mentions_task`, `generate_infographic_task`, `mark_sacred_segments_task`, `mark_external_quotes_task`, `generate_artwork_task` — each stores `model_used` + progress callbacks
- [x] Single shared `_pipeline_chain(item_id)` used by import/upload/reprocess (previously 3 hand-duplicated chains)

### API (`api.py`)
- [x] Full CRUD + pipeline endpoints for items, transcripts, search, progress, content
- [x] `GET/POST /api/admin/model-config`, `/api/admin/prompt-config` — persisted JSON configs
- [x] `POST /api/admin/test-model` — generic LLM model test
- [x] `POST /api/items/{id}/subtitle` — manual subtitle upload
- [x] `GET /api/formats`, `quality`/`preferred_quality` fields — quality selection for import/playback
- [x] ASR test lab endpoints: `GET /api/admin/asr-models`, `POST /api/admin/test-asr-one`, `POST /api/admin/test-asr-merge`
- [x] Chunking test lab endpoints: `POST /api/admin/test-chunking`, `GET /api/admin/test-chunking-audio/{test_id}/{idx}`
- [x] Static file serving: `/` → `index.html`, `/media/`, `/audio/`

### Frontend (`frontend/index.html`)
Single file, RTL, Persian, dark/light theme, no build step.

**Views:** کتابخانه (Library), نمایش (Viewer — karaoke + book mode, native video player, YouTube/Aparat/Telewebion), جستجو (Search), مدیریت (Admin).

**Player:**
- [x] Karaoke segment sync; book-mode paragraph view with proportional read/unread highlighting
- [x] Sacred segments (green) / external quotes (orange) highlighting
- [x] **Script-style speaker display**: consecutive same-speaker segments grouped into `.script-turn` blocks, speaker name shown once at the start like a play script, color-coded per speaker (session 6)
- [x] Video player moved into the title box area, directly above the transcript, no gap (session 6)
- [x] Chapter strip, mini-player with current segment text + chapter title
- [x] Quality picker (`#pl-quality`) reading available formats from `/api/formats`
- [x] Manual subtitle upload button
- [x] AI Q&A sidebar panel

**Admin:**
- [x] Pipeline stats bars (done count, avg duration, last model)
- [x] Per-item expandable step detail table
- [x] Model config editor (LLM/ASR/Image ordered lists, add/remove/reorder)
- [x] LLM model test UI
- [x] AI prompt editors (summary, correct, paragraphs, sacred, quotes, mentions)
- [x] Collections/tags management, items table
- [x] **ASR test lab**: pick item + clip window + language + prompt/context-hint + temperature, choose any subset of configured ASR models, run in parallel, see latency/text/params per model, optional dual-merge preview; now also flags when two models return byte-identical output (session 6, see below)
- [x] **Audio chunking test lab**: pick item + chunk count, actually cuts the real audio file (same code path as production), shows every resulting piece in order with time range/duration and whether the cut landed on detected silence, each with an inline audio player (session 6)

---

## ✅ Session 6 (this session) — collaborative prompt work, ASR lab, chunking

### New todos captured and shipped
1. Video player repositioned into the title box, directly above transcript (no gap)
2. Script-style (نمایشنامه) speaker rendering: name once per turn + per-speaker color, instead of bare "A:"/"B:" labels
3. Telewebion support added (`media_import.py` platform pattern + quality options), alongside existing Aparat/YouTube
4. Highest-quality audio enforced for transcription across all sources (`download_audio()` already always used `bestaudio/best`; verified, no change needed)
5. English translation feature fully removed per request (`translate_item` task kept but unused, no longer in pipeline chain; UI toggle and FA/EN switch removed)
6. Design/senior review pass: consolidated 3 duplicated Celery chains into one `_pipeline_chain()`; fixed a latent bug in `media_import.get_stream_url()` where the quality/format selector was computed but never actually applied
7. Per-step-model DAG / full manual pipeline test mode — **explicitly deferred to backlog** per user (wants to first improve prompts/models before deciding this is needed)

### ASR testing lab (built from scratch this session)
- Admin lab to compare configured ASR models side-by-side on a real clip from any imported item, with language/prompt/temperature control and optional LLM dual-merge preview
- **Found & fixed: GPT-4o-Transcribe was completely broken** (400 "invalid content-type" on every call). Root-caused via direct curl to the ArvanCloud gateway (bypassing bekhan code): the presence of a redundant `model` field in the multipart body caused the upstream to reject the request. Fixed by trying every parameter shape both with and without `model`.
- Investigated report that Xerxes-1 wasn't appearing correctly / was appearing when it shouldn't — endpoint now strictly mirrors the admin's configured ASR list, nothing else.
- Xerxes-1 itself still 404s on the gateway — confirmed to be an ArvanCloud-side routing/subscription issue, not fixable from bekhan's code.

### Major finding: "both models return the same result"
User reported Whisper-1 and GPT-4o-Transcribe always returning identical transcripts. Verified by calling the ArvanCloud gateway directly for both model URLs on the same audio: **responses were byte-for-byte identical, including identical raw Whisper token IDs** (e.g. `50364`, Whisper's own special token) — proof that ArvanCloud's "GPT-4o-Transcribe" alias is actually served by the same Whisper backend, not real GPT-4o. This is a provider-side (ArvanCloud) issue, not a bug in bekhan. Practical effect: the "dual-model merge" feature currently gains nothing with these two models selected, since they're the same engine.
- Mitigation shipped: the ASR test lab now automatically detects when two tested models return identical text and shows a warning explaining the likely cause.

### Audio chunking — silence-based splitting hardened
- Previous logic snapped each chunk boundary to the *nearest* detected silence within 60s, even a marginal one, with no protection against two boundaries landing on/near the same point.
- Reworked to prefer the *widest* silence interval near each target (more confidently between words), enforce strictly increasing/non-overlapping boundaries, and support a forced chunk count for testing on any file regardless of size.
- Shipped an admin test lab (`✂ آزمایش برش صدا`) that runs the real production cutting code on a real item and shows every resulting piece, in order, with timing and silence-snap diagnostics, each playable — so cuts can be checked by ear.
- Original fixed-dB approach was flagged by the user as likely wrong across different recordings (phone vs. studio audio have different noise floors) — resolved in Session 7 below with a per-file adaptive threshold + waveform visualization.

---

## ✅ Session 7 (this session) — multi-page Settings/Admin panel

Replaced the single flat Admin page with a tabbed Settings area: نمای کلی (Overview), مدل‌ها (Models), برش صدا (Chunking), رونویسی ASR (ASR). Each tab is a real sub-page, not just a collapsed section.

### مدل‌ها (Models)
- LLM fallback order editor (moved here from the old flat layout)
- **New: model import/capability test.** `GET /api/admin/all-models` lists every model found in `.env`, regardless of whether it's configured anywhere yet. A "تست" button per model empirically probes 4 capabilities in parallel — text (`/chat/completions`), embeddings (`/embeddings`), image generation (`/images/generations`), audio transcription (`/audio/transcriptions`, using a synthetic tone so it doesn't depend on any item existing) — and shows pass/fail + latency + a sample for each, instead of guessing from the model's name.

### برش صدا (Chunking)
- Silence detection rewritten as `detect_silences_v2()`: decodes the audio to raw PCM via ffmpeg and computes an actual RMS-dBFS envelope in Python/numpy (`_db_curve`), rather than parsing ffmpeg's `silencedetect` log text. This is the same code path for production chunking and the test lab, so what's tuned in the UI is exactly what runs.
- Two selectable algorithms, persisted in `/data/chunking_config.json`: **adaptive** (threshold = the Nth percentile of the file's own dB curve — a phone recording and a studio recording get different, file-appropriate silence floors) and **fixed** (one dB value for every file, the old behavior, kept as an option).
- Settings page: algorithm choice, percentile / fixed-dB, min silence duration, max drift, overlap, max chunk size — all persisted via `GET/POST /api/admin/chunking-config`.
- **Waveform test lab**: pick an item, optionally override any setting without saving, run — actually cuts the real audio and renders an inline SVG chart of the dB-over-time curve with the computed threshold line and every cut point overlaid (green dashed = landed on real silence, red dashed = hard cut), followed by the ordered, playable resulting chunks. Verified live: 41-minute item, adaptive threshold at -56.3dB, 3/3 internal cuts landed on silence.

### رونویسی ASR (ASR)
- ASR fallback order + the persisted ASR-dual toggle moved to their own tab (previously mixed in with LLM order)
- Test lab fields given visible labels (language, clip start/duration, temperature, prompt/context-hint, per-model checkboxes) instead of relying on `title` tooltips/placeholders — addresses the "I don't know what these fields are" feedback
- **New configurable ASR prompt**: added a `transcribe` key to the existing prompt-config system (`_DEFAULT_PROMPTS`, `/api/admin/prompt-config`), combined with each item's title in `transcribe_item()`. Shipped with an explanation panel in the UI: unlike the LLM instruction prompts elsewhere in Admin, this text is *not* a command — Whisper-style ASR models condition on it as if it were preceding transcript text, so it works by biasing spelling/vocabulary toward whatever proper nouns or recurring terms appear in it, not by being obeyed. Default value ships with common recurring terms from this archive's content (روح، برزخ، قیامت، قرآن کریم، امام حسین، کربلا، …).

---

## 🔴 Not Done / Pending

### Backlog (explicitly deferred by user)
- Full per-step manual pipeline test mode + DAG view (choose model/options per stage, run manually, compare/merge results, see time per stage) — deferred until prompt/model quality work shows whether it's still needed
- Collaborative prompt review, task by task, for each AI step (in progress via the ASR prompt work; other steps — correct/paragraphs/summarize/mentions/etc. — not yet reviewed together)

### Other known pending items (carried over, not yet revisited this session)
- Benchmark / model comparison page: run a fixed sample video through each ASR model + param combo, grade quality
- Search: currently client-side filter + transcript cache; no FTS5/embedding-based backend search
- Dual-model transcription: auto-detect low-quality audio and force dual mode automatically
- YouTube import blocked by a network/TLS issue in this Docker environment — deferred, user will address networking/VPN
- Chrome extension (detect media on any page → send to bekhan) — deferred until core app is stable
- AI-generated title/description/thumbnail (currently uses yt-dlp metadata as-is)

---

## Known Issues

| Issue | Status |
|-------|--------|
| YouTube network blocked in container | Deferred — user will fix VPN |
| GPT-4o-Transcribe and Whisper-1 appear to be the same backend engine on ArvanCloud | Provider-side, flagged in ASR lab automatically |
| Xerxes-1 404s on the gateway | Provider-side (routing/subscription), not currently selected anyway |
| `file_path` set to audio even for uploaded video files | Minor — doesn't break playback |
| No retry UI for individual pipeline steps | Future |
| Flower dashboard has no auth | Local-only, port 5556 |

---

## File Map

```
bekhan/
├── backend/
│   ├── Dockerfile          # FROM jost-api:latest; pip install -r requirements.txt --upgrade yt-dlp
│   ├── config.py           # Env vars + MODEL_URLS loader from .env
│   ├── db.py               # SQLite schema, helpers, pipeline_state tracking
│   ├── ai_client.py        # LLM/ASR fallback chains, chunking, prompt/model config loaders
│   ├── tasks.py            # Celery pipeline tasks (import → ... → artwork)
│   ├── api.py              # FastAPI endpoints, admin routes, test labs
│   ├── media_import.py     # yt-dlp wrappers, subtitle parsing, detect_source(), quality selection
│   ├── backup.py           # DB + media backup script
│   └── requirements.txt
├── frontend/
│   └── index.html          # Single-file SPA, RTL Persian
├── data/                   # Volume: bekhan.db, media/, audio/, model_config.json, prompts_config.json, chunking_config.json
├── docker-compose.yml      # redis + api + worker + flower
├── Makefile                # make up / make logs / make shell / make rebuild / make backup
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

# Rebuild after code changes (uvicorn has no --reload)
make rebuild
```

---

## Model Config

Edit `/data/model_config.json` or use the Admin UI:

```json
{
  "llm": ["DeepSeek-V3.2", "DeepSeek-V3.1", "GLM-4.6", "Claude-Haiku-4.5"],
  "asr": ["GPT-4o-Transcribe", "Whisper-1", "Xerxes-1"],
  "asr_dual": false,
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
