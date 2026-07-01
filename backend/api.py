"""FastAPI application — بخوان media archive."""
import os
import json
import threading
import logging
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, Response, UploadFile, File, Form, Depends, Header
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, PlainTextResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from celery import Celery
from config import REDIS_URL, MEDIA_DIR, AUDIO_DIR, BEKHAN_SECRET
import db

log = logging.getLogger(__name__)

celery = Celery('bekhan', broker=REDIS_URL, backend=REDIS_URL)


def _check_secret(authorization: Optional[str] = Header(default=None)):
    if not BEKHAN_SECRET:
        return
    if not authorization or authorization != f"Bearer {BEKHAN_SECRET}":
        raise HTTPException(status_code=401, detail="Unauthorized")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    os.makedirs(MEDIA_DIR, exist_ok=True)
    os.makedirs(AUDIO_DIR, exist_ok=True)
    yield


app = FastAPI(title='Bekhan API', version='0.1.0', lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=['*'],
    allow_methods=['*'],
    allow_headers=['*'],
)


# ── health / stats ─────────────────────────────────────────────────────────────

@app.get('/api/health')
def health():
    return {'status': 'ok'}


@app.get('/api/stats')
def stats():
    conn = db.get_conn()
    try:
        total = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        done  = conn.execute("SELECT COUNT(*) FROM items WHERE status='done'").fetchone()[0]
        proc  = conn.execute("SELECT COUNT(*) FROM items WHERE status NOT IN ('done','error','indexed')").fetchone()[0]
        err   = conn.execute("SELECT COUNT(*) FROM items WHERE status='error'").fetchone()[0]
        yt    = conn.execute("SELECT COUNT(*) FROM items WHERE source='youtube'").fetchone()[0]
        ap    = conn.execute("SELECT COUNT(*) FROM items WHERE source='aparat'").fetchone()[0]
        return {'total': total, 'done': done, 'processing': proc, 'error': err,
                'youtube': yt, 'aparat': ap}
    finally:
        conn.close()


# ── items ─────────────────────────────────────────────────────────────────────

@app.get('/api/items')
def list_items(limit: int = 100, source: str = None, status: str = None,
               collection: str = None, tag: str = None):
    items = db.list_items(limit=limit, source=source, status=status,
                          collection=collection, tag=tag)
    return {'items': items}


@app.get('/api/items/{item_id}')
def get_item(item_id: str):
    item = db.get_item(item_id)
    if not item:
        raise HTTPException(404, 'not found')
    return item


@app.get('/api/formats')
def get_formats(url: str):
    """List available video quality tiers for a URL, for the import quality picker."""
    from media_import import list_available_qualities
    try:
        qualities = list_available_qualities(url)
        return {'qualities': qualities}
    except Exception as exc:
        log.warning("list_available_qualities failed for %s: %s", url[:80], exc)
        return {'qualities': [{'value': 'best', 'label': 'بهترین کیفیت موجود'}]}


@app.post('/api/items')
def import_item(body: dict, _: None = Depends(_check_secret)):
    """Import media from URL. Body: {url, collections?, tags?, title?, quality?}"""
    url = (body.get('url') or '').strip()
    if not url:
        raise HTTPException(400, 'url required')

    conn = db.get_conn()
    existing = conn.execute("SELECT id FROM items WHERE url_source=?", (url,)).fetchone()
    conn.close()
    if existing:
        return {'id': existing['id'], 'status': 'existing'}

    import uuid as _uuid
    from media_import import detect_source
    item_id = str(_uuid.uuid4())
    collections = json.dumps(body.get('collections') or [], ensure_ascii=False)
    tags = json.dumps(body.get('tags') or [], ensure_ascii=False)
    src = detect_source(url)
    quality = (body.get('quality') or 'best').strip() or 'best'

    db.upsert_item({
        'id': item_id,
        'url_source': url,
        'source': src,
        'title': body.get('title') or '',
        'collections_json': collections,
        'tags_json': tags,
        'preferred_quality': quality,
        'status': 'indexed',
    })

    from celery import chain as cchain
    from tasks import import_url, _pipeline_chain

    cchain(import_url.si(item_id), _pipeline_chain(item_id)).apply_async()

    return {'id': item_id, 'status': 'queued'}


@app.post('/api/items/upload')
async def upload_item(
    file: UploadFile = File(...),
    title: str = Form(default=''),
    collections: str = Form(default='[]'),
    tags: str = Form(default='[]'),
    _: None = Depends(_check_secret),
):
    """Upload a media file directly."""
    import uuid as _uuid, aiofiles

    item_id = str(_uuid.uuid4())
    os.makedirs(MEDIA_DIR, exist_ok=True)

    ext = (file.filename or '').rsplit('.', 1)[-1].lower() or 'mp4'
    safe_ext = ext if ext in {'mp4','webm','mkv','avi','mov','flv','mp3','wav','ogg','m4a','aac','flac'} else 'mp4'
    dest = os.path.join(MEDIA_DIR, f"{item_id}.{safe_ext}")

    async with aiofiles.open(dest, 'wb') as f:
        while chunk := await file.read(1024 * 1024):
            await f.write(chunk)

    try:
        cols = json.loads(collections) if collections else []
    except Exception:
        cols = []
    try:
        tgs = json.loads(tags) if tags else []
    except Exception:
        tgs = []

    db.upsert_item({
        'id': item_id,
        'title': title or file.filename or '',
        'file_path': dest,
        'source': 'upload',
        'collections_json': json.dumps(cols, ensure_ascii=False),
        'tags_json': json.dumps(tgs, ensure_ascii=False),
        'status': 'indexed',
    })

    from celery import chain as cchain
    from tasks import import_upload, _pipeline_chain

    cchain(import_upload.si(item_id), _pipeline_chain(item_id)).apply_async()

    return {'id': item_id, 'status': 'queued'}


def _clear_downstream(item_id: str):
    """Drop cached AI content + pipeline state for everything after transcription,
    so the pipeline regenerates cleanly against a freshly-provided transcript."""
    conn = db.get_conn()
    try:
        conn.execute(
            "DELETE FROM ai_content WHERE item_id=? AND content_type IN "
            "('paragraphs','summary','main_theme','mentions','infographic',"
            "'sacred_segs','ext_quotes','speakers','artwork')",
            (item_id,)
        )
        conn.execute(
            "DELETE FROM pipeline_state WHERE item_id=? AND step IN "
            "('correct','translate','diarize','paragraphs','summarize','mentions',"
            "'infographic','sacred','quotes','artwork')",
            (item_id,)
        )
        conn.execute("DELETE FROM transcript_segments WHERE item_id=? AND language='en'", (item_id,))
        conn.execute("UPDATE transcript_segments SET speaker=NULL WHERE item_id=?", (item_id,))
        conn.commit()
    finally:
        conn.close()


@app.post('/api/items/{item_id}/subtitle')
async def upload_subtitle(item_id: str, file: UploadFile = File(...),
                          _: None = Depends(_check_secret)):
    """Manually attach a subtitle file (.srt/.vtt) as the item's transcript,
    skipping ASR entirely. Re-runs the rest of the pipeline against it."""
    item = db.get_item(item_id)
    if not item:
        raise HTTPException(404, 'not found')

    ext = (file.filename or '').rsplit('.', 1)[-1].lower()
    if ext not in ('srt', 'vtt'):
        raise HTTPException(400, 'only .srt or .vtt files are supported')

    import tempfile
    from media_import import parse_subtitle_file

    raw = await file.read()
    fd, tmp_path = tempfile.mkstemp(suffix=f'.{ext}')
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(raw)
        segments = parse_subtitle_file(tmp_path)
    finally:
        os.remove(tmp_path)

    if not segments:
        raise HTTPException(400, 'could not parse any segments from this file')

    db.save_segments(item_id, 'fa', segments)
    db.set_pipeline_state(item_id, 'transcribe', 'done', model_used='manual-subtitle')
    _clear_downstream(item_id)
    db.set_status(item_id, 'transcribed')

    from tasks import _pipeline_chain
    _pipeline_chain(item_id).apply_async()

    return {'ok': True, 'segments': len(segments)}


@app.post('/api/items/{item_id}/reprocess')
def reprocess_item(item_id: str, _: None = Depends(_check_secret)):
    item = db.get_item(item_id)
    if not item:
        raise HTTPException(404, 'not found')

    from tasks import _pipeline_chain

    db.set_status(item_id, 'indexed')
    _pipeline_chain(item_id).apply_async()
    return {'ok': True}


@app.patch('/api/items/{item_id}')
def update_item(item_id: str, body: dict, _: None = Depends(_check_secret)):
    """Update title, collections, tags, preferred_quality."""
    if not db.get_item(item_id):
        raise HTTPException(404, 'not found')
    allowed = ['title_fa', 'title', 'collections_json', 'tags_json', 'preferred_quality']
    data = {'id': item_id}
    for k in allowed:
        if k in body:
            data[k] = body[k]
    if 'collections' in body:
        data['collections_json'] = json.dumps(body['collections'], ensure_ascii=False)
    if 'tags' in body:
        data['tags_json'] = json.dumps(body['tags'], ensure_ascii=False)
    if 'quality' in body:
        data['preferred_quality'] = body['quality']
    db.upsert_item(data)
    return {'ok': True}


@app.delete('/api/items/{item_id}')
def delete_item(item_id: str, _: None = Depends(_check_secret)):
    item = db.get_item(item_id)
    if not item:
        raise HTTPException(404, 'not found')
    conn = db.get_conn()
    try:
        conn.execute("DELETE FROM transcript_segments WHERE item_id=?", (item_id,))
        conn.execute("DELETE FROM ai_content WHERE item_id=?", (item_id,))
        conn.execute("DELETE FROM pipeline_state WHERE item_id=?", (item_id,))
        conn.execute("DELETE FROM items WHERE id=?", (item_id,))
        conn.commit()
    finally:
        conn.close()
    # Remove local audio file if it's in AUDIO_DIR (not MEDIA_DIR uploads)
    fp = item.get('file_path') or ''
    if fp and fp.startswith(AUDIO_DIR) and os.path.exists(fp):
        try:
            os.remove(fp)
        except Exception:
            pass
    return {'ok': True}


# ── transcript ────────────────────────────────────────────────────────────────

@app.get('/api/items/{item_id}/transcript')
def get_transcript(item_id: str, lang: str = 'fa'):
    conn = db.get_conn()
    try:
        rows = conn.execute(
            "SELECT seg_index, start_sec, end_sec, text, words_json, speaker "
            "FROM transcript_segments WHERE item_id=? AND language=? ORDER BY seg_index",
            (item_id, lang)
        ).fetchall()
        segments = [{
            'seg_index': r['seg_index'],
            'start': r['start_sec'],
            'end': r['end_sec'],
            'text': r['text'],
            'speaker': r['speaker'],
        } for r in rows]
        return {'segments': segments, 'language': lang}
    finally:
        conn.close()


@app.get('/api/items/{item_id}/speakers')
def get_speakers(item_id: str):
    conn = db.get_conn()
    try:
        row = conn.execute(
            "SELECT content FROM ai_content WHERE item_id=? AND content_type='speakers' AND language='fa'",
            (item_id,)
        ).fetchone()
    finally:
        conn.close()
    if not row or not row['content']:
        return {'is_multi_speaker': False, 'names': {}}
    try:
        return json.loads(row['content'])
    except Exception:
        return {'is_multi_speaker': False, 'names': {}}


@app.get('/api/items/{item_id}/transcript.srt')
def get_transcript_srt(item_id: str, lang: str = 'fa'):
    conn = db.get_conn()
    try:
        rows = conn.execute(
            "SELECT seg_index, start_sec, end_sec, text FROM transcript_segments "
            "WHERE item_id=? AND language=? ORDER BY seg_index",
            (item_id, lang)
        ).fetchall()
    finally:
        conn.close()

    def fmt(t):
        t = t or 0
        h, rem = divmod(int(t), 3600)
        m, s = divmod(rem, 60)
        ms = int((t - int(t)) * 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    lines = []
    for i, r in enumerate(rows, 1):
        lines.append(f"{i}")
        lines.append(f"{fmt(r['start_sec'])} --> {fmt(r['end_sec'])}")
        lines.append(r['text'] or '')
        lines.append('')
    return PlainTextResponse('\n'.join(lines), media_type='text/plain; charset=utf-8')


# ── AI content ────────────────────────────────────────────────────────────────

@app.get('/api/items/{item_id}/ai')
def get_ai(item_id: str):
    conn = db.get_conn()
    try:
        rows = conn.execute(
            "SELECT content_type, language, content, model_id FROM ai_content WHERE item_id=?",
            (item_id,)
        ).fetchall()
    finally:
        conn.close()

    result = {}
    for r in rows:
        key = f"{r['content_type']}_{r['language']}"
        val = r['content'] or ''
        try:
            parsed = json.loads(val)
        except Exception:
            parsed = val
        result[key] = {'content': parsed, 'model': r['model_id']}
    return result


@app.get('/api/items/{item_id}/artwork')
def get_artwork(item_id: str):
    conn = db.get_conn()
    try:
        row = conn.execute(
            "SELECT content FROM ai_content WHERE item_id=? AND content_type='artwork' AND language='fa'",
            (item_id,)
        ).fetchone()
    finally:
        conn.close()

    if not row or not row['content']:
        raise HTTPException(404, 'artwork not ready')

    content = row['content']
    if content.startswith('data:image/png'):
        # Gemini-generated PNG
        b64 = content.split(',', 1)[1]
        import base64 as b64mod
        img_bytes = b64mod.b64decode(b64)
        return Response(content=img_bytes, media_type='image/png')
    return Response(content=content, media_type='image/svg+xml')


@app.get('/api/items/{item_id}/stream-url')
def get_stream_url(item_id: str):
    """Get fresh stream URL for Aparat/HLS sources (called by frontend player)."""
    item = db.get_item(item_id)
    if not item:
        raise HTTPException(404, 'not found')

    url_source = item.get('url_source') or ''
    if not url_source:
        raise HTTPException(400, 'no source URL')

    source = item.get('source') or ''
    if source == 'youtube':
        return {'url': url_source, 'type': 'youtube'}

    if source in ('upload',):
        file_path = item.get('file_path') or ''
        if file_path and os.path.exists(file_path):
            return {'url': f"/api/items/{item_id}/file", 'type': 'file'}
        raise HTTPException(404, 'file not found')

    try:
        from media_import import get_stream_url as _get_url
        quality = item.get('preferred_quality') or 'best'
        stream_url = _get_url(url_source, quality)
        if not stream_url:
            raise HTTPException(502, 'could not get stream URL')
        stream_type = 'hls' if 'm3u8' in stream_url else 'mp4'
        return {'url': stream_url, 'type': stream_type}
    except HTTPException:
        raise
    except Exception as exc:
        log.error("stream_url %s: %s", item_id, exc)
        raise HTTPException(502, str(exc))


@app.get('/api/items/{item_id}/file')
def serve_file(item_id: str):
    """Serve uploaded media file."""
    item = db.get_item(item_id)
    if not item or not item.get('file_path'):
        raise HTTPException(404, 'not found')
    fp = item['file_path']
    if not os.path.exists(fp):
        raise HTTPException(404, 'file missing')
    return FileResponse(fp)


@app.get('/api/items/{item_id}/progress')
def get_progress(item_id: str):
    return {'steps': db.get_pipeline_progress(item_id)}


@app.post('/api/items/{item_id}/ask')
async def ask_about_item(item_id: str, body: dict):
    """Per-item AI Q&A — answers in Persian using transcript as context."""
    question = (body.get('question') or '').strip()
    if not question:
        raise HTTPException(400, 'question required')

    item = db.get_item(item_id)
    if not item:
        raise HTTPException(404, 'not found')

    transcript = db.get_transcript_text(item_id, 'fa')
    if not transcript:
        raise HTTPException(400, 'no transcript yet')

    conn = db.get_conn()
    row = conn.execute(
        "SELECT content FROM ai_content WHERE item_id=? AND content_type='summary' AND language='fa'",
        (item_id,)
    ).fetchone()
    conn.close()
    summary = row['content'] if row else ''

    title = item.get('title') or item.get('title_fa') or ''
    from ai_client import chat
    prompt = (
        f"عنوان محتوا: {title}\n"
        f"خلاصه: {summary[:500] if summary else ''}\n\n"
        f"متن رونویسی (۶۰۰۰ کاراکتر اول):\n{transcript[:6000]}\n\n"
        f"سوال: {question}\n\n"
        "پاسخ جامع و دقیق به فارسی بده. اگر پاسخ در متن نیست، صادقانه بگو."
    )
    try:
        answer = await chat([{"role": "user", "content": prompt}], temperature=0.5, max_tokens=2048)
        return {'answer': answer}
    except Exception as exc:
        raise HTTPException(502, str(exc))


# ── collections ───────────────────────────────────────────────────────────────

@app.get('/api/collections')
def list_collections():
    return {'collections': db.get_all_collections()}


@app.get('/api/tags')
def list_tags():
    return {'tags': db.get_all_tags()}


# ── model config ──────────────────────────────────────────────────────────────

_MODEL_CONFIG_PATH = os.path.join(os.path.dirname(os.getenv('DB_PATH', '/data/bekhan.db')), 'model_config.json')

_DEFAULT_CONFIG = {
    "asr":       ["GPT-4o-Transcribe", "Whisper-1", "Xerxes-1"],
    "llm":       ["DeepSeek-V3.2", "DeepSeek-V3.1", "GLM-4.6",
                  "Claude-Haiku-4.5", "Qwen3-30B-A3B", "Gemini-3.1-Flash-Lite-Preview"],
    "image":     [],
    "translate": False,   # set true to enable auto-translation to English
    "asr_dual":  False,   # set true to run dual ASR+LLM merge
}


def _load_model_config() -> dict:
    try:
        with open(_MODEL_CONFIG_PATH) as f:
            return json.load(f)
    except Exception:
        return dict(_DEFAULT_CONFIG)


def _save_model_config(cfg: dict):
    os.makedirs(os.path.dirname(_MODEL_CONFIG_PATH), exist_ok=True)
    with open(_MODEL_CONFIG_PATH, 'w') as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


@app.get('/api/admin/model-config')
def get_model_config():
    from config import MODEL_URLS
    cfg = _load_model_config()
    # add available_models so UI knows what keys exist
    cfg['available'] = list(MODEL_URLS.keys())
    return cfg


@app.post('/api/admin/model-config')
def save_model_config(body: dict, _: None = Depends(_check_secret)):
    list_keys = {'asr', 'llm', 'image'}
    bool_keys = {'translate', 'asr_dual'}
    new_cfg = {k: v for k, v in body.items() if k in list_keys and isinstance(v, list)}
    new_cfg.update({k: bool(v) for k, v in body.items() if k in bool_keys})
    _save_model_config(new_cfg)
    return {'ok': True}


_PROMPT_CONFIG_PATH = os.path.join(os.path.dirname(os.getenv('DB_PATH', '/data/bekhan.db')), 'prompts_config.json')

_DEFAULT_PROMPTS = {
    "summary": (
        "متن رونویسی این محتوا را برایم خلاصه کن.\n"
        "یک خلاصه روایی نیم‌صفحه‌ای به فارسی روان بنویس — انگار دوستت برایت تعریف می‌کند چه چیزی در این ویدیو/صدا بود.\n"
        "نه عنوان، نه لیست، نه بولت‌پوینت — فقط چند پاراگراف پیوسته که بگوید چه موضوعاتی مطرح شد، "
        "چه نکات مهمی گفته شد، و پیام اصلی محتوا چیست.\n"
        "حداقل ۱۵۰ کلمه و حداکثر ۳۰۰ کلمه."
    ),
    "correct": (
        "You are correcting ASR errors in a transcript. Fix homophones, run-together words, misheard terms.\n"
        "Return ONLY a JSON array of corrected strings, same count as input, same order.\n"
        "Fix only clear errors. If uncertain, keep original. No explanations."
    ),
    "paragraphs": (
        "متن زیر رونویسی خام یک محتوای رسانه‌ای است.\n"
        "این متن را به یک بخش کتاب تبدیل کن:\n"
        "۱. یک عنوان معنادار فارسی (۲ تا ۵ کلمه) برای این بخش بساز\n"
        "۲. متن را به نثر روان و خوانا تبدیل کن\n"
        "۳. پاراگراف‌بندی طبیعی با خط خالی بین پاراگراف‌ها\n"
        "۴. همه محتوا را حفظ کن — فقط پاکسازی، نه حذف"
    ),
    "sacred": (
        "بخش‌هایی از رونویسی که مستقیماً شامل متن عربی دینی هستند را شناسایی کن:\n"
        "- آیات قرآنی (عربی)\n- احادیث یا روایات (عربی)\n- دعاها یا اذکار عربی\n"
        "فقط اگر متن عربی دینی است شامل کن. متن فارسی یا توضیح دینی را شامل نکن."
    ),
    "quotes": (
        "بخش‌هایی از رونویسی را شناسایی کن که در آن‌ها گوینده مستقیماً از شخص دیگر، کتاب، "
        "مقاله یا منبع خارجی نقل‌قول می‌کند.\n"
        "نقل‌قول مستقیم = گوینده کلام شخص دیگری را می‌خواند یا می‌آورد."
    ),
    "mentions": (
        "اشخاص، کتاب‌ها، مکان‌ها و سازمان‌های ذکر شده در این محتوا را استخراج کن."
    ),
    "transcribe": (
        "روح، برزخ، قیامت، آخرالزمان، قرآن کریم، حدیث، امام حسین، کربلا، اهل بیت، "
        "ان‌شاءالله، إن‌شاءالله، استغفرالله"
    ),
}


def _load_prompt_config() -> dict:
    try:
        with open(_PROMPT_CONFIG_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_prompt_config(cfg: dict):
    os.makedirs(os.path.dirname(_PROMPT_CONFIG_PATH), exist_ok=True)
    with open(_PROMPT_CONFIG_PATH, 'w') as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


@app.get('/api/admin/prompt-config')
def get_prompt_config():
    current = _load_prompt_config()
    return {'prompts': current, 'defaults': _DEFAULT_PROMPTS}


@app.post('/api/admin/prompt-config')
def save_prompt_config(body: dict, _: None = Depends(_check_secret)):
    allowed = set(_DEFAULT_PROMPTS.keys())
    new_cfg = {k: v for k, v in body.items() if k in allowed and isinstance(v, str)}
    _save_prompt_config(new_cfg)
    return {'ok': True}


@app.post('/api/admin/test-model')
async def test_model(body: dict):
    """Run a quick test prompt against specified models and return latency + snippet."""
    import time
    import httpx as _httpx
    from config import MODEL_URLS, API_KEY

    category = body.get('category', 'llm')  # 'llm' | 'asr'
    models = body.get('models') or []
    prompt = body.get('prompt') or 'سلام، یک جمله کوتاه به فارسی بنویس.'

    results = []
    if category == 'llm':
        for model_key in models:
            if model_key not in MODEL_URLS:
                results.append({'model': model_key, 'error': 'not configured'})
                continue
            url = MODEL_URLS[model_key]
            t0 = time.time()
            try:
                import httpx as _httpx
                async with _httpx.AsyncClient(timeout=30) as client:
                    r = await client.post(
                        url.rstrip('/') + '/chat/completions',
                        json={'model': model_key, 'messages': [{'role': 'user', 'content': prompt}],
                              'max_tokens': 100, 'temperature': 0.3},
                        headers={'Authorization': f'Bearer {API_KEY}', 'Content-Type': 'application/json'}
                    )
                    r.raise_for_status()
                    text = r.json()['choices'][0]['message']['content']
                elapsed = round(time.time() - t0, 2)
                results.append({'model': model_key, 'latency_sec': elapsed,
                                 'snippet': text[:200], 'ok': True})
            except Exception as exc:
                results.append({'model': model_key, 'latency_sec': round(time.time()-t0, 2),
                                 'error': str(exc)[:120], 'ok': False})
    return {'results': results}


@app.get('/api/admin/asr-models')
def list_asr_models():
    """ASR models to offer in the test lab: exactly the ones chosen in the
    admin's ASR config (پیکربندی مدل‌ها). Models not selected there don't
    show up here either — the lab tests what's actually in use."""
    cfg = _load_model_config()
    return {'models': cfg.get('asr') or []}


@app.get('/api/admin/all-models')
def list_all_models():
    """Every model found in .env — not filtered to what's already chosen
    anywhere. Used by the مدل‌ها (Models) settings page to discover and test
    what's actually available on the gateway before deciding to use it."""
    from config import MODEL_URLS
    return {'models': sorted(MODEL_URLS.keys())}


def _synthetic_test_clip() -> bytes:
    """A tiny self-contained audio clip for probing whether an ASR endpoint
    is reachable at all — doesn't depend on any item existing yet."""
    import subprocess, tempfile
    fd, path = tempfile.mkstemp(suffix='.mp3')
    os.close(fd)
    try:
        subprocess.run([
            'ffmpeg', '-y', '-f', 'lavfi', '-i', 'sine=frequency=440:duration=2',
            '-acodec', 'libmp3lame', '-q:a', '4', path,
        ], capture_output=True, timeout=30)
        with open(path, 'rb') as f:
            return f.read()
    finally:
        try:
            os.remove(path)
        except Exception:
            pass


@app.post('/api/admin/test-model-capabilities')
async def test_model_capabilities(body: dict):
    """Empirically probe what a model can actually do, rather than guessing
    from its name: try chat/completions (text), embeddings, image generation,
    and audio transcription against its gateway URL, in parallel, and report
    which ones responded successfully with latency + a short sample."""
    import time, asyncio, httpx
    from config import MODEL_URLS, API_KEY

    model = body.get('model')
    if not model or model not in MODEL_URLS:
        raise HTTPException(400, 'unknown model')
    base = MODEL_URLS[model].rstrip('/')
    auth = {'Authorization': f'Bearer {API_KEY}'}

    async def _probe_text():
        t0 = time.time()
        try:
            async with httpx.AsyncClient(timeout=40) as client:
                r = await client.post(f'{base}/chat/completions', json={
                    'model': model,
                    'messages': [{'role': 'user', 'content': 'یک جمله کوتاه به فارسی بگو.'}],
                    'max_tokens': 60,
                }, headers={**auth, 'Content-Type': 'application/json'})
            if r.status_code < 400:
                content = r.json()['choices'][0]['message']['content']
                return {'ok': True, 'latency_sec': round(time.time() - t0, 2), 'sample': content[:200]}
            return {'ok': False, 'latency_sec': round(time.time() - t0, 2), 'error': r.text[:200]}
        except Exception as exc:
            return {'ok': False, 'latency_sec': round(time.time() - t0, 2), 'error': str(exc)[:200]}

    async def _probe_embedding():
        t0 = time.time()
        try:
            async with httpx.AsyncClient(timeout=40) as client:
                r = await client.post(f'{base}/embeddings', json={'model': model, 'input': 'تست'},
                                       headers={**auth, 'Content-Type': 'application/json'})
            if r.status_code < 400:
                vec = r.json()['data'][0]['embedding']
                return {'ok': True, 'latency_sec': round(time.time() - t0, 2), 'sample': f'{len(vec)} بعد'}
            return {'ok': False, 'latency_sec': round(time.time() - t0, 2), 'error': r.text[:200]}
        except Exception as exc:
            return {'ok': False, 'latency_sec': round(time.time() - t0, 2), 'error': str(exc)[:200]}

    async def _probe_image():
        t0 = time.time()
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                r = await client.post(f'{base}/images/generations', json={
                    'model': model, 'prompt': 'a simple red circle on white background', 'n': 1, 'size': '256x256',
                }, headers={**auth, 'Content-Type': 'application/json'})
            if r.status_code < 400:
                return {'ok': True, 'latency_sec': round(time.time() - t0, 2), 'sample': 'تصویر تولید شد'}
            return {'ok': False, 'latency_sec': round(time.time() - t0, 2), 'error': r.text[:200]}
        except Exception as exc:
            return {'ok': False, 'latency_sec': round(time.time() - t0, 2), 'error': str(exc)[:200]}

    async def _probe_audio():
        t0 = time.time()
        try:
            clip = _synthetic_test_clip()
            async with httpx.AsyncClient(timeout=60) as client:
                r = await client.post(f'{base}/audio/transcriptions',
                                       files={'file': ('a.mp3', clip, 'audio/mpeg')},
                                       data={'language': 'fa', 'response_format': 'json'}, headers=auth)
            if r.status_code < 400:
                text = r.json().get('text', '')
                return {'ok': True, 'latency_sec': round(time.time() - t0, 2),
                        'sample': text[:150] or '(صدای تست بی‌کلام بود — اتصال موفق است)'}
            return {'ok': False, 'latency_sec': round(time.time() - t0, 2), 'error': r.text[:200]}
        except Exception as exc:
            return {'ok': False, 'latency_sec': round(time.time() - t0, 2), 'error': str(exc)[:200]}

    text, embedding, image, audio = await asyncio.gather(
        _probe_text(), _probe_embedding(), _probe_image(), _probe_audio(),
    )
    return {'model': model, 'results': {'text': text, 'embedding': embedding, 'image': image, 'audio': audio}}


def _extract_test_clip(item: dict, start_sec: float, duration_sec: float) -> bytes:
    import subprocess, tempfile
    duration_sec = max(5.0, min(duration_sec, 180.0))  # keep test clips short & cheap
    fd, clip_path = tempfile.mkstemp(suffix='.mp3')
    os.close(fd)
    try:
        subprocess.run([
            'ffmpeg', '-y', '-ss', str(start_sec), '-t', str(duration_sec),
            '-i', item['file_path'], '-acodec', 'libmp3lame', '-q:a', '4', clip_path,
        ], capture_output=True, timeout=60)
        if not os.path.exists(clip_path) or os.path.getsize(clip_path) == 0:
            raise HTTPException(500, 'could not extract audio clip (ffmpeg failed)')
        with open(clip_path, 'rb') as f:
            return f.read()
    finally:
        try:
            os.remove(clip_path)
        except Exception:
            pass


@app.post('/api/admin/test-asr-one')
async def test_asr_one(body: dict):
    """
    Run a short clip from an already-imported item's audio through exactly ONE
    ASR model with full control over its params, and return latency + text.
    Split out from a combined endpoint so the frontend can call one per model
    in parallel and show real per-model progress as each resolves.
    Body: {item_id, model, start_sec?, duration_sec?, temperature?, context_hint?, language?}
    """
    import time
    from ai_client import _transcribe_bytes_single

    item_id = body.get('item_id')
    item = db.get_item(item_id) if item_id else None
    if not item or not item.get('file_path') or not os.path.exists(item['file_path']):
        raise HTTPException(400, 'item has no usable audio file')

    model = body.get('model')
    if not model:
        raise HTTPException(400, 'model required')

    start_sec = float(body.get('start_sec') or 0)
    duration_sec = float(body.get('duration_sec') or 60)
    language = body.get('language')
    if language is None:
        language = item.get('language') or 'fa'
    context_hint = body.get('context_hint')
    if context_hint is None:
        context_hint = item.get('title') or item.get('title_fa') or ''
    temperature = body.get('temperature')
    if temperature is not None:
        try:
            temperature = float(temperature)
        except (TypeError, ValueError):
            temperature = None

    clip_bytes = _extract_test_clip(item, start_sec, duration_sec)

    t0 = time.time()
    try:
        segs = await _transcribe_bytes_single(clip_bytes, language, model,
                                              context_hint, temperature, raise_on_fail=True)
        elapsed = round(time.time() - t0, 2)
        text = ' '.join(s.get('text', '') for s in segs).strip()
        return {'model': model, 'latency_sec': elapsed, 'text': text,
                'segment_count': len(segs), 'ok': bool(text),
                'params': {'language': language, 'context_hint': context_hint, 'temperature': temperature}}
    except Exception as exc:
        return {'model': model, 'latency_sec': round(time.time() - t0, 2),
                'error': str(exc)[:300], 'ok': False}


_CHUNK_TEST_CACHE: dict = {}  # test_id -> {'dir': str, 'created': float}


def _cleanup_chunk_tests(keep: int = 3):
    import shutil
    items = sorted(_CHUNK_TEST_CACHE.items(), key=lambda kv: kv[1]['created'], reverse=True)
    for tid, info in items[keep:]:
        shutil.rmtree(info['dir'], ignore_errors=True)
        _CHUNK_TEST_CACHE.pop(tid, None)


@app.get('/api/admin/chunking-config')
def get_chunking_config():
    from ai_client import chunking_config
    return chunking_config()


@app.post('/api/admin/chunking-config')
def post_chunking_config(body: dict):
    from ai_client import save_chunking_config
    return save_chunking_config(body)


@app.post('/api/admin/test-chunking')
def test_chunking(body: dict):
    """Admin lab: actually run the silence-aware chunk-splitting used before
    parallel transcription, on a real item's audio, and return each resulting
    piece (in order) with its timing + whether the cut landed on detected
    silence, plus the full dB-over-time curve (for a waveform chart) and the
    threshold that was used, so cutting can be tuned visually and every join
    point can be listened to.
    Body: {item_id, n_chunks?, max_mb?, overlap_sec?,
           algorithm?('fixed'|'adaptive'), fixed_db?, percentile?,
           min_silence_dur?, max_drift_sec?}
    Any algorithm/threshold field left out falls back to the saved config."""
    import uuid, time
    from ai_client import analyze_and_split_for_test

    item_id = body.get('item_id')
    item = db.get_item(item_id) if item_id else None
    if not item or not item.get('file_path') or not os.path.exists(item['file_path']):
        raise HTTPException(400, 'item has no usable audio file')

    max_mb = float(body.get('max_mb') or 19.0)
    overlap_sec = float(body.get('overlap_sec') or 3.0)
    n_chunks = body.get('n_chunks')
    n_chunks = int(n_chunks) if n_chunks else None

    def _f(key):
        v = body.get(key)
        return float(v) if v is not None and v != '' else None

    result = analyze_and_split_for_test(
        item['file_path'], max_mb=max_mb, overlap_sec=overlap_sec, force_chunks=n_chunks,
        algorithm=body.get('algorithm'), fixed_db=_f('fixed_db'), percentile=_f('percentile'),
        min_silence_dur=_f('min_silence_dur'), max_drift_sec=_f('max_drift_sec'),
    )

    test_id = uuid.uuid4().hex[:12]
    chunk_dir = os.path.dirname(result['chunks'][0]['path'])
    _CHUNK_TEST_CACHE[test_id] = {'dir': chunk_dir, 'created': time.time()}
    _cleanup_chunk_tests()

    for c in result['chunks']:
        c['audio_url'] = f"/api/admin/test-chunking-audio/{test_id}/{c['idx']}"
        del c['path']
    result['test_id'] = test_id
    return result


@app.get('/api/admin/test-chunking-audio/{test_id}/{idx}')
def test_chunking_audio(test_id: str, idx: int):
    info = _CHUNK_TEST_CACHE.get(test_id)
    if not info:
        raise HTTPException(404, 'test expired, run it again')
    path = os.path.join(info['dir'], f'chunk_{idx:03d}.mp3')
    if not os.path.exists(path):
        raise HTTPException(404, 'chunk not found')
    return FileResponse(path, media_type='audio/mpeg')


@app.post('/api/admin/test-asr-merge')
async def test_asr_merge(body: dict):
    """Merge two already-obtained transcripts with the same LLM logic used by
    production dual-ASR mode. Body: {model_a, text_a, model_b, text_b}"""
    import time
    from ai_client import _merge_dual_transcripts

    model_a, model_b = body.get('model_a', 'A'), body.get('model_b', 'B')
    text_a, text_b = body.get('text_a') or '', body.get('text_b') or ''
    segs_a = [{'start': None, 'end': None, 'text': text_a}] if text_a else []
    segs_b = [{'start': None, 'end': None, 'text': text_b}] if text_b else []

    t0 = time.time()
    try:
        merged_segs = await _merge_dual_transcripts(segs_a, segs_b, model_a, model_b)
        return {'text': ' '.join(s.get('text', '') for s in merged_segs).strip(),
                'latency_sec': round(time.time() - t0, 2)}
    except Exception as exc:
        return {'error': str(exc)[:300]}


# ── admin ─────────────────────────────────────────────────────────────────────

@app.get('/api/admin/pipeline-stats')
def pipeline_stats():
    return db.get_pipeline_stats()


@app.get('/api/admin/workers')
def admin_workers():
    try:
        i = celery.control.inspect(timeout=1.5)
        active = i.active() or {}
        workers = []
        for name, tasks in active.items():
            workers.append({'name': name, 'active_tasks': len(tasks), 'tasks': [t.get('name') for t in tasks]})
        return {'workers': workers, 'count': len(workers)}
    except Exception as exc:
        return {'workers': [], 'count': 0, 'error': str(exc)}


# ── static frontend ───────────────────────────────────────────────────────────

FRONTEND_DIR = os.getenv('FRONTEND_DIR', os.path.join(os.path.dirname(__file__), '../frontend'))

if os.path.isdir(FRONTEND_DIR):
    app.mount('/static', StaticFiles(directory=FRONTEND_DIR), name='static')

    @app.get('/{full_path:path}')
    async def serve_frontend(full_path: str):
        index = os.path.join(FRONTEND_DIR, 'index.html')
        if os.path.exists(index):
            return FileResponse(index)
        raise HTTPException(404, 'frontend not found')
