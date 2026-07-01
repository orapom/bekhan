"""
Celery pipeline tasks for bekhan.
Pipeline: import → transcribe → correct → paragraphs → summarize →
          mentions → infographic → sacred → quotes → artwork → done
"""
import os
import json
import asyncio
import logging
from celery import Celery, chain
from config import REDIS_URL, AUDIO_DIR, MEDIA_DIR, LLM_MODEL

log = logging.getLogger(__name__)

app = Celery('bekhan', broker=REDIS_URL, backend=REDIS_URL)
app.conf.update(
    task_serializer='json',
    accept_content=['json'],
    result_serializer='json',
    timezone='Asia/Tehran',
    enable_utc=True,
    worker_prefetch_multiplier=1,
    task_acks_late=True,
    task_track_started=True,
)

import db


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _ps(item_id: str, step: str, status: str, lang: str = '_',
        err: str | None = None, model: str | None = None,
        progress_pct: int | None = None):
    try:
        db.set_pipeline_state(item_id, step, status, language=lang,
                              error_msg=err, model_used=model,
                              progress_pct=progress_pct)
    except Exception:
        pass


def _m():
    """Return the last model used by ai_client."""
    try:
        from ai_client import get_last_model
        return get_last_model()
    except Exception:
        return None


def _pipeline_chain(item_id: str):
    # Note: EN translation step removed from the pipeline per user preference.
    # translate_item/translate_to_english are left in place (unused) in case
    # this is wanted again later — nothing currently calls them.
    return chain(
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
    )


# ── import ────────────────────────────────────────────────────────────────────

@app.task(name='import_url', bind=True, max_retries=2)
def import_url(self, item_id: str):
    """Import media from URL: extract info via yt-dlp, download audio, save metadata."""
    from media_import import extract_info, download_audio, download_subtitles, build_item_meta
    import tempfile, shutil

    item = db.get_item(item_id)
    if not item:
        raise ValueError(f"item {item_id} not found")

    url = item.get('url_source') or ''
    if not url:
        raise ValueError(f"no URL for {item_id}")

    _ps(item_id, 'import', 'running')
    db.set_status(item_id, 'importing')

    try:
        info = extract_info(url)
        meta = build_item_meta(url, info)
        meta['id'] = item_id
        db.upsert_item(meta)

        os.makedirs(AUDIO_DIR, exist_ok=True)
        audio_out = os.path.join(AUDIO_DIR, item_id)
        audio_path = download_audio(url, audio_out)

        subs_dir = tempfile.mkdtemp()
        try:
            subs = download_subtitles(url, item_id, subs_dir)
        except Exception as e:
            log.warning("subtitle download failed for %s: %s", item_id, e)
            subs = []

        db.upsert_item({'id': item_id, 'file_path': audio_path, 'status': 'imported'})
        _ps(item_id, 'import', 'done')

        # Store subtitles as transcript if available
        if subs:
            _load_subtitles(item_id, subs)
            shutil.rmtree(subs_dir, ignore_errors=True)

        log.info("imported %s (%s) → %s", item_id, url[:60], audio_path)
        return item_id

    except Exception as exc:
        _ps(item_id, 'import', 'error', err=str(exc))
        db.set_status(item_id, 'error', str(exc))
        raise self.retry(exc=exc, countdown=30)


def _load_subtitles(item_id: str, subs: list):
    """Load subtitle files as transcript segments into DB."""
    from media_import import parse_subtitle_file

    lang_priority = ['fa', 'en', 'ar']
    for lang in lang_priority:
        for sub in subs:
            if sub.get('language', '') == lang or lang in sub.get('language', ''):
                try:
                    segments = parse_subtitle_file(sub['path'])
                    if segments:
                        db.save_segments(item_id, 'fa', segments)
                        log.info("loaded %d subtitle segments (%s) for %s", len(segments), lang, item_id)
                        return
                except Exception as e:
                    log.warning("subtitle parse failed %s: %s", sub['path'], e)


@app.task(name='import_upload', bind=True)
def import_upload(self, item_id: str):
    """Process an uploaded file: extract audio if video, start pipeline."""
    import subprocess, shutil

    item = db.get_item(item_id)
    if not item:
        raise ValueError(f"item {item_id} not found")

    file_path = item.get('file_path') or ''
    if not file_path or not os.path.exists(file_path):
        raise ValueError(f"file not found for {item_id}")

    _ps(item_id, 'import', 'running')
    db.set_status(item_id, 'importing')

    try:
        ext = file_path.rsplit('.', 1)[-1].lower()
        if ext in ('mp4', 'webm', 'mkv', 'avi', 'mov', 'flv'):
            os.makedirs(AUDIO_DIR, exist_ok=True)
            audio_path = os.path.join(AUDIO_DIR, f"{item_id}.mp3")
            subprocess.run([
                'ffmpeg', '-y', '-i', file_path, '-vn',
                '-acodec', 'libmp3lame', '-q:a', '4', audio_path
            ], capture_output=True, timeout=600)
            if not os.path.exists(audio_path):
                raise RuntimeError("ffmpeg audio extraction failed")
            db.upsert_item({'id': item_id, 'file_path': audio_path, 'type': 'video'})
        else:
            db.upsert_item({'id': item_id, 'type': 'audio'})

        result = subprocess.run(
            ['ffprobe', '-v', 'quiet', '-show_entries', 'format=duration',
             '-of', 'csv=p=0', file_path],
            capture_output=True, text=True, timeout=30
        )
        duration = float(result.stdout.strip() or '0') or None
        if duration:
            db.upsert_item({'id': item_id, 'duration_sec': duration})

        db.set_status(item_id, 'imported')
        _ps(item_id, 'import', 'done')
        log.info("import_upload %s done", item_id)
        return item_id

    except Exception as exc:
        _ps(item_id, 'import', 'error', err=str(exc))
        db.set_status(item_id, 'error', str(exc))
        raise


# ── transcribe ────────────────────────────────────────────────────────────────

@app.task(name='transcribe_item', bind=True)
def transcribe_item(self, item_id: str):
    item = db.get_item(item_id)
    if not item or not item.get('file_path'):
        raise ValueError(f"no audio file for {item_id}")

    audio_path = item['file_path']
    if not os.path.exists(audio_path):
        raise FileNotFoundError(f"audio file missing: {audio_path}")

    if db.transcript_segment_count(item_id, 'fa') > 0:
        _ps(item_id, 'transcribe', 'done')
        return item_id

    _ps(item_id, 'transcribe', 'running')
    db.set_status(item_id, 'transcribing')
    try:
        from ai_client import transcribe_audio, transcribe_audio_dual, _model_config, _load_prompts
        lang = item.get('language') or 'fa'
        title_hint = ((item.get('title') or item.get('title_fa') or '')[:150])
        asr_prompt = (_load_prompts().get('transcribe') or '').strip()
        # ASR 'prompt' is priming context, not an instruction — Whisper-style
        # models condition on it as if it were preceding transcript text, so
        # we append it after the title to bias spelling/vocabulary of names
        # and terms that recur across the archive.
        title_hint = f"{title_hint}. {asr_prompt}"[:200] if asr_prompt else title_hint

        def _on_progress(done: int, total: int):
            pct = int(done / total * 100) if total > 0 else 0
            _ps(item_id, 'transcribe', 'running', progress_pct=pct)

        dual = _model_config().get('asr_dual', False)
        if dual:
            seg_list = _run(transcribe_audio_dual(audio_path, language=lang,
                                                   context_hint=title_hint,
                                                   on_progress=_on_progress))
        else:
            seg_list = _run(transcribe_audio(audio_path, language=lang,
                                             context_hint=title_hint,
                                             on_progress=_on_progress))
        asr_model = _m()
        db.save_segments(item_id, 'fa', seg_list)
        dur = None
        if seg_list and seg_list[-1].get('end'):
            dur = seg_list[-1]['end']
        db.upsert_item({'id': item_id, 'duration_sec': dur, 'status': 'transcribed'})
        _ps(item_id, 'transcribe', 'done', model=asr_model)
        log.info("transcribed %d segments for %s (dual=%s)", len(seg_list), item_id, dual)
        return item_id
    except Exception as exc:
        _ps(item_id, 'transcribe', 'error', err=str(exc))
        db.set_status(item_id, 'error', str(exc))
        raise


# ── correct ───────────────────────────────────────────────────────────────────

@app.task(name='correct_transcript', bind=True)
def correct_transcript(self, item_id: str):
    from ai_client import correct_transcript_segments

    item = db.get_item(item_id)
    if not item:
        return item_id

    conn = db.get_conn()
    segs_raw = conn.execute(
        "SELECT id, seg_index, start_sec, end_sec, text, words_json "
        "FROM transcript_segments WHERE item_id=? AND language='fa' ORDER BY seg_index",
        (item_id,)
    ).fetchall()
    conn.close()

    if not segs_raw:
        return item_id

    # Skip if already corrected (check pipeline_state)
    conn_chk = db.get_conn()
    already_done = conn_chk.execute(
        "SELECT 1 FROM pipeline_state WHERE item_id=? AND step='correct' AND status='done'",
        (item_id,)
    ).fetchone()
    conn_chk.close()
    if already_done:
        _ps(item_id, 'correct', 'done')
        return item_id

    seg_dicts = [{'start': r['start_sec'], 'end': r['end_sec'], 'text': r['text'],
                  'words': json.loads(r['words_json']) if r['words_json'] else None}
                 for r in segs_raw]

    _ps(item_id, 'correct', 'running')

    def _on_progress(done: int, total: int):
        pct = int(done / total * 100) if total > 0 else 0
        _ps(item_id, 'correct', 'running', progress_pct=pct)

    try:
        corrected = _run(correct_transcript_segments(
            seg_dicts,
            title=item.get('title') or item.get('title_fa') or '',
            language=item.get('language') or 'fa',
            on_progress=_on_progress,
        ))
        llm_model = _m()
        conn2 = db.get_conn()
        for orig, fix in zip(segs_raw, corrected):
            if fix['text'] != orig['text']:
                conn2.execute("UPDATE transcript_segments SET text=? WHERE id=?",
                              (fix['text'], orig['id']))
        conn2.commit()
        conn2.close()
        _ps(item_id, 'correct', 'done', model=llm_model)
    except Exception as exc:
        _ps(item_id, 'correct', 'error', err=str(exc))
        log.error("correct_transcript %s: %s", item_id, exc)
    return item_id


# ── translate ─────────────────────────────────────────────────────────────────

@app.task(name='translate_item', bind=True)
def translate_item(self, item_id: str):
    from ai_client import translate_to_english, _model_config

    if not _model_config().get('translate'):
        return item_id

    if db.transcript_segment_count(item_id, 'en') > 0:
        _ps(item_id, 'translate', 'done')
        return item_id

    conn = db.get_conn()
    segs_raw = conn.execute(
        "SELECT seg_index, start_sec, end_sec, text FROM transcript_segments "
        "WHERE item_id=? AND language='fa' ORDER BY seg_index",
        (item_id,)
    ).fetchall()
    conn.close()
    if not segs_raw:
        return item_id

    seg_dicts = [{'seg_index': r['seg_index'], 'start': r['start_sec'],
                  'end': r['end_sec'], 'text': r['text']}
                 for r in segs_raw]

    _ps(item_id, 'translate', 'running')

    def _on_progress(done: int, total: int):
        pct = int(done / total * 100) if total > 0 else 0
        _ps(item_id, 'translate', 'running', progress_pct=pct)

    try:
        translated = _run(translate_to_english(seg_dicts, on_progress=_on_progress))
        used: str = _m() or LLM_MODEL or ''
        db.save_segments(item_id, 'en', translated)
        _ps(item_id, 'translate', 'done', model=used)
        log.info("translated %d segments for %s", len(translated), item_id)
    except Exception as exc:
        _ps(item_id, 'translate', 'error', err=str(exc))
        log.error("translate %s: %s", item_id, exc)
    return item_id


# ── diarize ───────────────────────────────────────────────────────────────────

@app.task(name='diarize_item', bind=True)
def diarize_item(self, item_id: str):
    from ai_client import diarize_speakers

    conn = db.get_conn()
    segs_raw = conn.execute(
        "SELECT id, seg_index, start_sec, end_sec, text FROM transcript_segments "
        "WHERE item_id=? AND language='fa' ORDER BY seg_index",
        (item_id,)
    ).fetchall()
    conn.close()

    if not segs_raw:
        return item_id

    item = db.get_item(item_id) or {}
    seg_dicts = [{'seg_index': r['seg_index'], 'start': r['start_sec'],
                  'end': r['end_sec'], 'text': r['text']}
                 for r in segs_raw]

    _ps(item_id, 'diarize', 'running')

    def _on_progress(done: int, total: int):
        pct = int(done / total * 100) if total > 0 else 0
        _ps(item_id, 'diarize', 'running', progress_pct=pct)

    try:
        result = _run(diarize_speakers(
            seg_dicts, title=item.get('title') or item.get('title_fa') or '',
            on_progress=_on_progress,
        ))
        used: str = _m() or LLM_MODEL or ''
        db.save_ai_content(item_id, 'speakers', 'fa',
                           json.dumps({'is_multi_speaker': result['is_multi_speaker'],
                                       'names': result.get('names', {})},
                                      ensure_ascii=False), used)
        if result['is_multi_speaker'] and result.get('assignments'):
            assign_map = {a['seg_index']: a['speaker'] for a in result['assignments']}
            conn2 = db.get_conn()
            for row in segs_raw:
                sp = assign_map.get(row['seg_index'])
                if sp:
                    conn2.execute("UPDATE transcript_segments SET speaker=? WHERE id=?",
                                  (sp, row['id']))
            conn2.commit()
            conn2.close()
        _ps(item_id, 'diarize', 'done', model=used)
        log.info("diarized %s (multi=%s)", item_id, result['is_multi_speaker'])
    except Exception as exc:
        _ps(item_id, 'diarize', 'error', err=str(exc))
        log.error("diarize %s: %s", item_id, exc)
    return item_id


# ── paragraphs ────────────────────────────────────────────────────────────────

@app.task(name='generate_paragraphs', bind=True)
def generate_paragraphs(self, item_id: str):
    from ai_client import generate_book_paragraphs

    if db.ai_content_exists(item_id, 'paragraphs', 'fa'):
        _ps(item_id, 'paragraphs', 'done')
        return item_id

    conn = db.get_conn()
    segs_raw = conn.execute(
        "SELECT seg_index, start_sec, end_sec, text FROM transcript_segments "
        "WHERE item_id=? AND language='fa' ORDER BY seg_index",
        (item_id,)
    ).fetchall()
    conn.close()
    if not segs_raw:
        return item_id

    item = db.get_item(item_id) or {}
    seg_dicts = [{'start': r['start_sec'], 'end': r['end_sec'],
                  'text': r['text'], 'seg_index': r['seg_index']}
                 for r in segs_raw]

    _ps(item_id, 'paragraphs', 'running')

    def _on_progress(done: int, total: int):
        pct = int(done / total * 100) if total > 0 else 0
        _ps(item_id, 'paragraphs', 'running', progress_pct=pct)

    try:
        paras = _run(generate_book_paragraphs(
            seg_dicts, title=item.get('title') or item.get('title_fa') or '',
            on_progress=_on_progress,
        ))
        used: str = _m() or LLM_MODEL or ''
        db.save_ai_content(item_id, 'paragraphs', 'fa',
                           json.dumps(paras, ensure_ascii=False), used)
        _ps(item_id, 'paragraphs', 'done', model=used)
        log.info("generated %d paragraphs for %s", len(paras), item_id)
    except Exception as exc:
        _ps(item_id, 'paragraphs', 'error', err=str(exc))
        log.error("generate_paragraphs %s: %s", item_id, exc)
    return item_id


# ── summarize ─────────────────────────────────────────────────────────────────

@app.task(name='summarize_item', bind=True)
def summarize_item(self, item_id: str):
    from ai_client import summarize_fa

    if db.ai_content_exists(item_id, 'summary', 'fa'):
        _ps(item_id, 'summarize', 'done')
        return item_id

    full_text = db.get_transcript_text(item_id, 'fa')
    if not full_text:
        return item_id

    item = db.get_item(item_id)
    title = (item or {}).get('title') or (item or {}).get('title_fa') or ''

    _ps(item_id, 'summarize', 'running')
    try:
        result = _run(summarize_fa(full_text, title=title))
        used: str = _m() or LLM_MODEL or ''
        db.save_ai_content(item_id, 'summary', 'fa', result.get('summary_fa', ''), used)
        if result.get('main_theme'):
            db.save_ai_content(item_id, 'main_theme', 'fa', result['main_theme'], used)
        db.set_status(item_id, 'summarized')
        _ps(item_id, 'summarize', 'done', model=used)
        log.info("summarized %s", item_id)
    except Exception as exc:
        _ps(item_id, 'summarize', 'error', err=str(exc))
        log.error("summarize %s: %s", item_id, exc)
    return item_id


# ── mentions ──────────────────────────────────────────────────────────────────

@app.task(name='extract_mentions_task', bind=True)
def extract_mentions_task(self, item_id: str):
    from ai_client import extract_mentions

    if db.ai_content_exists(item_id, 'mentions', 'fa'):
        return item_id

    full_text = db.get_transcript_text(item_id, 'fa')
    if not full_text:
        return item_id

    _ps(item_id, 'mentions', 'running')
    try:
        data = _run(extract_mentions(full_text))
        used: str = _m() or LLM_MODEL or ''
        db.save_ai_content(item_id, 'mentions', 'fa',
                           json.dumps(data, ensure_ascii=False), used)
        _ps(item_id, 'mentions', 'done', model=used)
    except Exception as exc:
        _ps(item_id, 'mentions', 'error', err=str(exc))
        log.error("mentions %s: %s", item_id, exc)
    return item_id


# ── infographic ───────────────────────────────────────────────────────────────

@app.task(name='generate_infographic_task', bind=True)
def generate_infographic_task(self, item_id: str):
    from ai_client import generate_infographic

    if db.ai_content_exists(item_id, 'infographic', 'fa'):
        return item_id

    full_text = db.get_transcript_text(item_id, 'fa')
    if not full_text:
        return item_id

    conn = db.get_conn()
    row = conn.execute(
        "SELECT content FROM ai_content WHERE item_id=? AND content_type='summary' AND language='fa'",
        (item_id,)
    ).fetchone()
    conn.close()
    summary_fa = row['content'] if row else ''

    item = db.get_item(item_id)
    title = (item or {}).get('title') or (item or {}).get('title_fa') or ''

    _ps(item_id, 'infographic', 'running')
    try:
        data = _run(generate_infographic(full_text, summary_fa=summary_fa, title=title))
        used: str = _m() or LLM_MODEL or ''
        db.save_ai_content(item_id, 'infographic', 'fa',
                           json.dumps(data, ensure_ascii=False), used)
        _ps(item_id, 'infographic', 'done', model=used)
    except Exception as exc:
        _ps(item_id, 'infographic', 'error', err=str(exc))
        log.error("infographic %s: %s", item_id, exc)
    return item_id


# ── sacred segments ───────────────────────────────────────────────────────────

@app.task(name='mark_sacred_segments_task', bind=True)
def mark_sacred_segments_task(self, item_id: str):
    from ai_client import mark_sacred_segments

    if db.ai_content_exists(item_id, 'sacred_segs', 'fa'):
        return item_id

    conn = db.get_conn()
    segs_raw = conn.execute(
        "SELECT seg_index, start_sec, end_sec, text FROM transcript_segments "
        "WHERE item_id=? AND language='fa' ORDER BY seg_index",
        (item_id,)
    ).fetchall()
    conn.close()
    if not segs_raw:
        return item_id

    seg_dicts = [{'seg_index': r['seg_index'], 'start': r['start_sec'],
                  'end': r['end_sec'], 'text': r['text']}
                 for r in segs_raw]

    _ps(item_id, 'sacred', 'running')
    try:
        indices = _run(mark_sacred_segments(seg_dicts))
        used: str = _m() or LLM_MODEL or ''
        db.save_ai_content(item_id, 'sacred_segs', 'fa',
                           json.dumps(indices, ensure_ascii=False), used)
        _ps(item_id, 'sacred', 'done', model=used)
    except Exception as exc:
        _ps(item_id, 'sacred', 'error', err=str(exc))
        log.error("sacred_segs %s: %s", item_id, exc)
    return item_id


# ── external quotes ───────────────────────────────────────────────────────────

@app.task(name='mark_external_quotes_task', bind=True)
def mark_external_quotes_task(self, item_id: str):
    from ai_client import mark_external_quotes

    if db.ai_content_exists(item_id, 'ext_quotes', 'fa'):
        return item_id

    conn = db.get_conn()
    segs_raw = conn.execute(
        "SELECT seg_index, start_sec, end_sec, text FROM transcript_segments "
        "WHERE item_id=? AND language='fa' ORDER BY seg_index",
        (item_id,)
    ).fetchall()
    conn.close()
    if not segs_raw:
        return item_id

    seg_dicts = [{'seg_index': r['seg_index'], 'start': r['start_sec'],
                  'end': r['end_sec'], 'text': r['text']}
                 for r in segs_raw]

    _ps(item_id, 'quotes', 'running')
    try:
        indices = _run(mark_external_quotes(seg_dicts))
        used: str = _m() or LLM_MODEL or ''
        db.save_ai_content(item_id, 'ext_quotes', 'fa',
                           json.dumps(indices, ensure_ascii=False), used)
        _ps(item_id, 'quotes', 'done', model=used)
    except Exception as exc:
        _ps(item_id, 'quotes', 'error', err=str(exc))
        log.error("ext_quotes %s: %s", item_id, exc)
    return item_id


# ── artwork ───────────────────────────────────────────────────────────────────

@app.task(name='generate_artwork_task', bind=True)
def generate_artwork_task(self, item_id: str):
    import httpx, base64
    from ai_client import generate_artwork

    if db.ai_content_exists(item_id, 'artwork', 'fa'):
        db.set_status(item_id, 'done')
        _ps(item_id, 'artwork', 'done')
        return item_id

    item = db.get_item(item_id)
    if not item:
        return item_id

    conn = db.get_conn()
    row = conn.execute(
        "SELECT content FROM ai_content WHERE item_id=? AND content_type='summary' AND language='fa'",
        (item_id,)
    ).fetchone()
    conn.close()
    summary_fa = row['content'] if row else ''

    thumbnail_url = item.get('url_thumbnail') or ''
    thumbnail_b64 = ''
    thumbnail_mime = 'image/jpeg'

    if thumbnail_url:
        try:
            resp = httpx.get(thumbnail_url, timeout=20, follow_redirects=True,
                             headers={'User-Agent': 'Mozilla/5.0'})
            if resp.status_code == 200:
                ct = resp.headers.get('content-type', 'image/jpeg').split(';')[0].strip()
                thumbnail_mime = ct if ct.startswith('image/') else 'image/jpeg'
                thumbnail_b64 = base64.b64encode(resp.content).decode('ascii')
        except Exception as e:
            log.warning("thumbnail fetch failed for %s: %s", item_id, e)

    title = item.get('title') or item.get('title_fa') or ''

    _ps(item_id, 'artwork', 'running')
    try:
        artwork = _run(generate_artwork(
            title, summary_fa,
            thumbnail_url=thumbnail_url,
            thumbnail_b64=thumbnail_b64,
            thumbnail_mime=thumbnail_mime,
        ))
        used: str = _m() or LLM_MODEL or ''
        db.save_ai_content(item_id, 'artwork', 'fa', artwork)
        db.set_status(item_id, 'done')
        _ps(item_id, 'artwork', 'done', model=used)
        log.info("artwork generated for %s", item_id)
    except Exception as exc:
        _ps(item_id, 'artwork', 'error', err=str(exc))
        log.error("artwork %s: %s", item_id, exc)
        db.set_status(item_id, 'done')
    return item_id
