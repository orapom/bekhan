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


@app.post('/api/items')
def import_item(body: dict, _: None = Depends(_check_secret)):
    """Import media from URL. Body: {url, collections?, tags?, title?}"""
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

    db.upsert_item({
        'id': item_id,
        'url_source': url,
        'source': src,
        'title': body.get('title') or '',
        'collections_json': collections,
        'tags_json': tags,
        'status': 'indexed',
    })

    from celery import chain as cchain
    from tasks import import_url, transcribe_item, correct_transcript, diarize_item, generate_paragraphs
    from tasks import summarize_item, extract_mentions_task, generate_infographic_task
    from tasks import mark_sacred_segments_task, mark_external_quotes_task, generate_artwork_task, translate_item

    cchain(
        import_url.si(item_id),
        transcribe_item.si(item_id),
        correct_transcript.si(item_id),
        diarize_item.si(item_id),
        generate_paragraphs.si(item_id),
        summarize_item.si(item_id),
        extract_mentions_task.si(item_id),
        generate_infographic_task.si(item_id),
        mark_sacred_segments_task.si(item_id),
        mark_external_quotes_task.si(item_id),
        generate_artwork_task.si(item_id),
        translate_item.si(item_id),
    ).apply_async()

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
    from tasks import import_upload, transcribe_item, correct_transcript, diarize_item, generate_paragraphs
    from tasks import summarize_item, extract_mentions_task, generate_infographic_task
    from tasks import mark_sacred_segments_task, mark_external_quotes_task, generate_artwork_task, translate_item

    cchain(
        import_upload.si(item_id),
        transcribe_item.si(item_id),
        correct_transcript.si(item_id),
        diarize_item.si(item_id),
        generate_paragraphs.si(item_id),
        summarize_item.si(item_id),
        extract_mentions_task.si(item_id),
        generate_infographic_task.si(item_id),
        mark_sacred_segments_task.si(item_id),
        mark_external_quotes_task.si(item_id),
        generate_artwork_task.si(item_id),
        translate_item.si(item_id),
    ).apply_async()

    return {'id': item_id, 'status': 'queued'}


@app.post('/api/items/{item_id}/reprocess')
def reprocess_item(item_id: str, _: None = Depends(_check_secret)):
    item = db.get_item(item_id)
    if not item:
        raise HTTPException(404, 'not found')

    from celery import chain as cchain
    from tasks import transcribe_item, correct_transcript, diarize_item, generate_paragraphs
    from tasks import summarize_item, extract_mentions_task, generate_infographic_task
    from tasks import mark_sacred_segments_task, mark_external_quotes_task, generate_artwork_task, translate_item

    db.set_status(item_id, 'indexed')
    cchain(
        transcribe_item.si(item_id),
        correct_transcript.si(item_id),
        diarize_item.si(item_id),
        generate_paragraphs.si(item_id),
        summarize_item.si(item_id),
        extract_mentions_task.si(item_id),
        generate_infographic_task.si(item_id),
        mark_sacred_segments_task.si(item_id),
        mark_external_quotes_task.si(item_id),
        generate_artwork_task.si(item_id),
        translate_item.si(item_id),
    ).apply_async()
    return {'ok': True}


@app.patch('/api/items/{item_id}')
def update_item(item_id: str, body: dict, _: None = Depends(_check_secret)):
    """Update title, collections, tags."""
    if not db.get_item(item_id):
        raise HTTPException(404, 'not found')
    allowed = ['title_fa', 'title', 'collections_json', 'tags_json']
    data = {'id': item_id}
    for k in allowed:
        if k in body:
            data[k] = body[k]
    if 'collections' in body:
        data['collections_json'] = json.dumps(body['collections'], ensure_ascii=False)
    if 'tags' in body:
        data['tags_json'] = json.dumps(body['tags'], ensure_ascii=False)
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
        stream_url = _get_url(url_source)
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
