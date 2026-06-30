"""
ArvanCloud AI gateway client (OpenAI-compatible).
Adapted from enghelab/minbar for bekhan — general media content.
"""
import json
import os
import re
import logging
import httpx
from config import LLM_URL, LLM_MODEL, EMBED_URL, EMBED_MODEL, API_KEY, MODEL_URLS, IMAGE_URL, IMAGE_MODEL

log = logging.getLogger(__name__)

_HEADERS = lambda: {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}

_LLM_FALLBACK_ORDER = ['DeepSeek-V3.2', 'DeepSeek-V3.1', 'GLM-4.6', 'Claude-Haiku-4.5', 'Qwen3-30B-A3B', 'Gemini-3.1-Flash-Lite-Preview']
_ASR_FALLBACK_ORDER = ['GPT-4o-Transcribe', 'Whisper-1', 'Xerxes-1']  # no local whisper

_last_model: dict = {'name': None}


def get_last_model() -> str | None:
    return _last_model['name']


def _model_config() -> dict:
    """Load optional /data/model_config.json for custom ordering."""
    cfg_path = os.path.join(os.path.dirname(os.getenv('DB_PATH', '/data/bekhan.db')), 'model_config.json')
    try:
        with open(cfg_path) as f:
            return json.load(f)
    except Exception:
        return {}


def _llm_order() -> list[str]:
    cfg = _model_config()
    return cfg.get('llm') or _LLM_FALLBACK_ORDER


def _asr_order() -> list[str]:
    cfg = _model_config()
    return cfg.get('asr') or _ASR_FALLBACK_ORDER


def _load_prompts() -> dict:
    """Load custom prompt instructions from /data/prompts_config.json."""
    cfg_dir = os.path.dirname(os.getenv('DB_PATH', '/data/bekhan.db'))
    try:
        with open(os.path.join(cfg_dir, 'prompts_config.json')) as f:
            return json.load(f)
    except Exception:
        return {}


async def chat(messages: list[dict],
               temperature: float = 0.3,
               max_tokens: int = 4096,
               json_mode: bool = False,
               timeout: float = 240) -> str:
    if not LLM_URL:
        raise RuntimeError("No LLM URL configured")

    candidates = [(LLM_URL, LLM_MODEL)] if LLM_URL else []
    for m in _llm_order():
        if m != LLM_MODEL and m in MODEL_URLS:
            candidates.append((MODEL_URLS[m], m))

    payload: dict = {
        "model": LLM_MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    last_exc: Exception | None = None
    async with httpx.AsyncClient(timeout=timeout) as client:
        for url_base, model_id in candidates:
            url = url_base.rstrip('/') + '/chat/completions'
            p = {**payload, "model": model_id}
            try:
                r = await client.post(url, json=p, headers=_HEADERS())
                if r.status_code in (404, 502, 503):
                    log.warning("chat: %s returned %s, trying fallback", model_id, r.status_code)
                    last_exc = httpx.HTTPStatusError(f"{r.status_code}", request=r.request, response=r)
                    continue
                r.raise_for_status()
                _last_model['name'] = model_id
                return r.json()['choices'][0]['message']['content']
            except httpx.HTTPStatusError as e:
                log.warning("chat: %s HTTP error %s, trying fallback", model_id, e)
                last_exc = e
            except Exception as e:
                log.warning("chat: %s error %s, trying fallback", model_id, e)
                last_exc = e
    raise RuntimeError(f"All LLM models failed. Last error: {last_exc}")


def _parse_json(raw: str):
    m = re.search(r'```(?:json)?\s*([\s\S]+?)\s*```', raw)
    if m:
        raw = m.group(1)
    m2 = re.search(r'[\[{][\s\S]*[\]}]', raw)
    if m2:
        raw = m2.group()
    return json.loads(raw)


# ── ASR ────────────────────────────────────────────────────────────────────────

def _get_duration(audio_path: str) -> float:
    import subprocess
    r = subprocess.run(
        ['ffprobe', '-v', 'quiet', '-show_entries', 'format=duration',
         '-of', 'csv=p=0', audio_path],
        capture_output=True, text=True, timeout=30,
    )
    return float(r.stdout.strip() or '0') or 3600.0


def _detect_silences(audio_path: str) -> list:
    import subprocess
    r = subprocess.run(
        ['ffmpeg', '-i', audio_path, '-af', 'silencedetect=n=-35dB:d=0.5', '-f', 'null', '-'],
        capture_output=True, text=True, timeout=180,
    )
    mids = []
    t_start = None
    for line in r.stderr.split('\n'):
        ms = re.search(r'silence_start: ([\d.]+)', line)
        if ms:
            t_start = float(ms.group(1))
        me = re.search(r'silence_end: ([\d.]+)', line)
        if me and t_start is not None:
            mids.append((t_start + float(me.group(1))) / 2.0)
            t_start = None
    return mids


def _split_audio_chunks(audio_path: str, max_mb: float = 19.0, overlap_sec: float = 3.0) -> list:
    import subprocess, tempfile, math
    size_mb = os.path.getsize(audio_path) / (1024 * 1024)
    if size_mb <= max_mb:
        dur = _get_duration(audio_path)
        return [(audio_path, 0.0, dur)]

    total_dur = _get_duration(audio_path)
    n_chunks = math.ceil(size_mb / max_mb)
    target_dur = total_dur / n_chunks
    silences = _detect_silences(audio_path)

    boundaries = [0.0]
    for i in range(1, n_chunks):
        target = i * target_dur
        if silences:
            nearest = min(silences, key=lambda t: abs(t - target))
            boundaries.append(nearest if abs(nearest - target) <= 60.0 else target)
        else:
            boundaries.append(target)
    boundaries.append(total_dur)

    tmpdir = tempfile.mkdtemp()
    chunks = []
    for i in range(len(boundaries) - 1):
        nom_start, nom_end = boundaries[i], boundaries[i + 1]
        actual_start = max(0.0, nom_start - (overlap_sec if i > 0 else 0.0))
        actual_end = min(total_dur, nom_end + (overlap_sec if i < n_chunks - 1 else 0.0))
        out = os.path.join(tmpdir, f'chunk_{i:03d}.mp3')
        subprocess.run([
            'ffmpeg', '-y', '-ss', str(actual_start), '-to', str(actual_end),
            '-i', audio_path, '-c', 'copy', out,
        ], capture_output=True, timeout=120)
        chunks.append((out, nom_start, nom_end))
    return chunks


async def _call_asr(url: str, model: str, audio_bytes: bytes, language: str,
                   context_hint: str = '') -> list:
    auth = {"Authorization": f"Bearer {API_KEY}"}
    hint = context_hint[:200] if context_hint else ''
    attempts = [
        {'model': model, 'language': language, 'response_format': 'verbose_json',
         'timestamp_granularities[]': 'word', 'prompt': hint},
        {'model': model, 'language': language, 'response_format': 'verbose_json', 'prompt': hint},
        {'model': model, 'language': language, 'response_format': 'verbose_json'},
        {'model': model, 'language': language, 'response_format': 'json'},
        {'model': model, 'response_format': 'verbose_json'},
        {'model': model, 'response_format': 'json'},
        {'model': model, 'language': language, 'response_format': 'text'},
        {'model': model},
    ]
    async with httpx.AsyncClient(timeout=600) as client:
        for params in attempts:
            fmt = params.get('response_format', 'json')
            try:
                async with client.stream(
                    'POST', url,
                    files={'file': ('audio.mp3', audio_bytes, 'audio/mpeg')},
                    data=params,
                    headers=auth,
                ) as r:
                    if r.status_code >= 400:
                        continue
                    body = await r.aread()
            except Exception as exc:
                log.warning("ASR %s error: %s", model, exc)
                continue

            ct = getattr(r, 'headers', {}).get('content-type', '')
            if fmt == 'text' or 'text/plain' in ct:
                text = body.decode('utf-8', errors='replace').strip()
                if text:
                    return [{'start': None, 'end': None, 'text': text}]
                continue
            try:
                data = json.loads(body)
            except Exception:
                text = body.decode('utf-8', errors='replace').strip()
                if text:
                    return [{'start': None, 'end': None, 'text': text}]
                continue

            raw_segs = data.get('segments') or []
            if raw_segs:
                out = []
                for s in raw_segs:
                    seg = {'start': s.get('start', 0), 'end': s.get('end', 0),
                           'text': s.get('text', '').strip()}
                    if s.get('words'):
                        seg['words'] = [
                            {'word': w.get('word', ''), 'start': w.get('start', 0),
                             'end': w.get('end', 0)}
                            for w in s['words']
                        ]
                    out.append(seg)
                return out
            text = data.get('text', '')
            if text:
                return [{'start': None, 'end': None, 'text': text.strip()}]
    return []


def _asr_model_id(config_key: str) -> str:
    mapping = {
        'mlx-whisper-local': 'mlx-whisper-turbo',
        'GPT-4o-Transcribe': 'gpt-4o-transcribe',
        'Whisper-1': 'whisper-1',
        'Xerxes-1': 'xerxes-om1',
    }
    return mapping.get(config_key, config_key.lower())


async def _transcribe_bytes(audio_bytes: bytes, language: str, context_hint: str = '') -> list:
    available = [(m, MODEL_URLS[m]) for m in _asr_order() if m in MODEL_URLS]
    if not available:
        raise RuntimeError("No ASR URL configured")

    last_exc = None
    for config_key, model_url in available:
        model_id = _asr_model_id(config_key)
        url = model_url.rstrip('/') + '/audio/transcriptions'
        try:
            result = await _call_asr(url, model_id, audio_bytes, language, context_hint)
            if result:
                _last_model['name'] = config_key
                return result
        except Exception as exc:
            last_exc = exc
    if last_exc:
        raise last_exc
    return []


async def _transcribe_bytes_single(audio_bytes: bytes, language: str,
                                    model_key: str, context_hint: str = '') -> list:
    """Transcribe with a specific model (no fallback chain)."""
    if model_key not in MODEL_URLS:
        return []
    model_id = _asr_model_id(model_key)
    url = MODEL_URLS[model_key].rstrip('/') + '/audio/transcriptions'
    try:
        return await _call_asr(url, model_id, audio_bytes, language, context_hint)
    except Exception as exc:
        log.warning("_transcribe_bytes_single %s: %s", model_key, exc)
        return []


def _assemble_chunks(results: list, overlap_sec: float = 3.0) -> list:
    """Merge sorted (chunk_idx, segs, nom_start) list into final segment list."""
    all_segments = []
    for enum_i, (_, segs, nom_start) in enumerate(results):
        if not segs:
            continue
        actual_start = max(0.0, nom_start - overlap_sec) if enum_i > 0 else nom_start
        for seg in segs:
            s_rel = seg.get('start')
            e_rel = seg.get('end')
            if s_rel is None:
                all_segments.append({'start': None, 'end': None, 'text': seg['text']})
                continue
            s_abs = actual_start + s_rel
            e_abs = actual_start + e_rel if e_rel is not None else None
            if s_abs < nom_start and enum_i > 0:
                continue
            out_seg = {'start': round(s_abs, 2),
                       'end': round(e_abs, 2) if e_abs is not None else None,
                       'text': seg['text']}
            if seg.get('words'):
                out_seg['words'] = [
                    {'word': w['word'],
                     'start': round(actual_start + w['start'], 2),
                     'end': round(actual_start + w['end'], 2)}
                    for w in seg['words']
                ]
            all_segments.append(out_seg)
    return all_segments


async def transcribe_audio(audio_path: str, language: str = 'fa',
                           context_hint: str = '',
                           on_progress=None) -> list:
    """Transcribe audio file. Chunks are processed in parallel (up to 4 concurrent)."""
    import shutil, asyncio
    chunks = _split_audio_chunks(audio_path, max_mb=19.0, overlap_sec=3.0)
    is_split = len(chunks) > 1 or chunks[0][0] != audio_path
    n = len(chunks)
    sem = asyncio.Semaphore(4)
    done_count = [0]

    async def _do_chunk(idx: int, chunk_path: str, nom_start: float):
        async with sem:
            with open(chunk_path, 'rb') as f:
                data = f.read()
            segs = await _transcribe_bytes(data, language, context_hint)
            done_count[0] += 1
            if on_progress:
                try:
                    on_progress(done_count[0], n)
                except Exception:
                    pass
            return (idx, segs, nom_start)

    raw = await asyncio.gather(
        *[_do_chunk(i, cp, ns) for i, (cp, ns, _) in enumerate(chunks)],
        return_exceptions=True,
    )
    results = sorted(
        [r for r in raw if not isinstance(r, BaseException)],
        key=lambda x: x[0],
    )
    try:
        return _assemble_chunks(results)
    finally:
        if is_split:
            tmpdir = os.path.dirname(chunks[0][0])
            try:
                shutil.rmtree(tmpdir, ignore_errors=True)
            except Exception:
                pass


async def _merge_dual_transcripts(segs_a: list, segs_b: list,
                                   model_a: str, model_b: str) -> list:
    """LLM picks/merges two ASR results for the same audio chunk."""
    if not segs_a:
        return segs_b
    if not segs_b:
        return segs_a
    text_a = ' '.join(s['text'] for s in segs_a).strip()
    text_b = ' '.join(s['text'] for s in segs_b).strip()
    if text_a == text_b:
        return segs_a
    prompt = (
        f"دو رونویسی از یک بخش صوتی دارم:\n"
        f"مدل {model_a}:\n{text_a}\n\n"
        f"مدل {model_b}:\n{text_b}\n\n"
        "بهترین رونویسی را انتخاب کن یا ترکیب کن تا دقیق‌ترین متن حاصل شود. "
        "فقط متن نهایی را بدون توضیح برگردان."
    )
    try:
        merged = await chat([{"role": "user", "content": prompt}], temperature=0.1, max_tokens=2048)
        merged = merged.strip()
        base = segs_a if segs_a and segs_a[0].get('start') is not None else segs_b
        if base and base[0].get('start') is not None:
            return [{'start': base[0]['start'], 'end': base[-1].get('end'), 'text': merged}]
        return [{'start': None, 'end': None, 'text': merged}]
    except Exception:
        return segs_a


async def transcribe_audio_dual(audio_path: str, language: str = 'fa',
                                context_hint: str = '',
                                on_progress=None) -> list:
    """Run top-2 ASR models in parallel per chunk, LLM-merge results."""
    import shutil, asyncio
    order = _asr_order()
    if len(order) < 2:
        return await transcribe_audio(audio_path, language, context_hint, on_progress)

    model_a, model_b = order[0], order[1]
    chunks = _split_audio_chunks(audio_path, max_mb=19.0, overlap_sec=3.0)
    is_split = len(chunks) > 1 or chunks[0][0] != audio_path
    n = len(chunks)
    sem = asyncio.Semaphore(3)
    done_count = [0]

    async def _do_chunk(idx: int, chunk_path: str, nom_start: float):
        async with sem:
            with open(chunk_path, 'rb') as f:
                data = f.read()
            pair = await asyncio.gather(
                _transcribe_bytes_single(data, language, model_a, context_hint),
                _transcribe_bytes_single(data, language, model_b, context_hint),
                return_exceptions=True,
            )
            sa = pair[0] if not isinstance(pair[0], BaseException) else []
            sb = pair[1] if not isinstance(pair[1], BaseException) else []
            merged = await _merge_dual_transcripts(sa, sb, model_a, model_b)
            _last_model['name'] = model_a
            done_count[0] += 1
            if on_progress:
                try:
                    on_progress(done_count[0], n)
                except Exception:
                    pass
            return (idx, merged, nom_start)

    raw = await asyncio.gather(
        *[_do_chunk(i, cp, ns) for i, (cp, ns, _) in enumerate(chunks)],
        return_exceptions=True,
    )
    results = sorted(
        [r for r in raw if not isinstance(r, BaseException)],
        key=lambda x: x[0],
    )
    try:
        return _assemble_chunks(results)
    finally:
        if is_split:
            tmpdir = os.path.dirname(chunks[0][0])
            try:
                shutil.rmtree(tmpdir, ignore_errors=True)
            except Exception:
                pass


# ── LLM pipeline steps ─────────────────────────────────────────────────────────

async def correct_transcript_segments(segments: list, title: str = '', language: str = 'fa') -> list:
    if not segments:
        return segments

    custom_instr = _load_prompts().get('correct', '')
    instr = custom_instr or (
        "You are correcting ASR (automatic speech recognition) errors in a transcript.\n"
        f"Content language: {language}. Fix homophones, run-together words, misheard terms.\n"
        f"Content: {title or 'general media'}\n\n"
        "Return ONLY a JSON array of corrected strings, same count as input, same order.\n"
        "Fix only clear errors. If uncertain, keep original. No explanations."
    )

    BATCH = 120
    corrected = list(segments)
    for batch_start in range(0, len(segments), BATCH):
        batch = segments[batch_start: batch_start + BATCH]
        lines = [f"{i+1}. {s['text']}" for i, s in enumerate(batch)]
        prompt = f"{instr}\n\nSegments:\n{chr(10).join(lines)}"
        try:
            raw = await chat([{"role": "user", "content": prompt}], temperature=0.1, max_tokens=4096)
            fixes = _parse_json(raw)
            if isinstance(fixes, list) and len(fixes) == len(batch):
                for j, fix in enumerate(fixes):
                    if isinstance(fix, str) and fix.strip():
                        corrected[batch_start + j] = {**corrected[batch_start + j], 'text': fix.strip()}
        except Exception as exc:
            log.warning("correct_transcript batch %d failed: %s", batch_start, exc)
    return corrected


async def generate_book_paragraphs(segments: list, title: str = '') -> list:
    if not segments:
        return []

    WORDS_PER_CHUNK = 1500
    chunks = []
    cur = []
    wc = 0
    for s in segments:
        cur.append(s)
        wc += len((s.get('text') or '').split())
        if wc >= WORDS_PER_CHUNK:
            chunks.append(cur)
            cur = []
            wc = 0
    if cur:
        chunks.append(cur)

    custom_para_instr = _load_prompts().get('paragraphs', '')
    para_instr = custom_para_instr or (
        "متن زیر رونویسی خام یک محتوای رسانه‌ای است که توسط سیستم ASR تهیه شده.\n"
        "این متن را به یک بخش کتاب تبدیل کن:\n"
        "۱. یک عنوان معنادار فارسی (۲ تا ۵ کلمه) برای این بخش بساز\n"
        "۲. متن را به نثر روان و خوانا تبدیل کن\n"
        "۳. پاراگراف‌بندی طبیعی با خط خالی بین پاراگراف‌ها\n"
        "۴. همه محتوا را حفظ کن — فقط پاکسازی، نه حذف"
    )

    result = []
    for chunk in chunks:
        start_sec = chunk[0].get('start')
        end_sec = chunk[-1].get('end')
        raw_text = ' '.join(s.get('text', '').strip() for s in chunk)
        prompt = (
            f"{para_instr}\n\n"
            'فقط JSON برگردان: {"title_fa": "عنوان بخش", "text_fa": "متن پاک‌شده"}\n\n'
            f"عنوان محتوا: {title or 'محتوای رسانه‌ای'}\n"
            f"متن خام:\n{raw_text}"
        )
        try:
            raw = await chat([{"role": "user", "content": prompt}], temperature=0.3,
                             max_tokens=4096, json_mode=True)
            data = _parse_json(raw)
            result.append({
                'title_fa': data.get('title_fa', ''),
                'text_fa': data.get('text_fa', raw_text),
                'start_sec': start_sec,
                'end_sec': end_sec,
            })
        except Exception as exc:
            log.warning("generate_book_paragraphs chunk failed: %s", exc)
            result.append({'title_fa': '', 'text_fa': raw_text,
                           'start_sec': start_sec, 'end_sec': end_sec})
    return result


async def summarize_fa(text: str, title: str = '') -> dict:
    """Returns {summary_fa, main_theme}"""
    custom_instr = _load_prompts().get('summary', '')
    instr = custom_instr or (
        "متن رونویسی این محتوا را برایم خلاصه کن.\n"
        "یک خلاصه روایی نیم‌صفحه‌ای به فارسی روان بنویس — انگار دوستت برایت تعریف می‌کند چه چیزی در این ویدیو/صدا بود.\n"
        "نه عنوان، نه لیست، نه بولت‌پوینت — فقط چند پاراگراف پیوسته که بگوید چه موضوعاتی مطرح شد، "
        "چه نکات مهمی گفته شد، و پیام اصلی محتوا چیست.\n"
        "حداقل ۱۵۰ کلمه و حداکثر ۳۰۰ کلمه."
    )
    prompt = (
        f"{instr}\n\n"
        f"عنوان: {title or 'بدون عنوان'}\n\n"
        'JSON برگردان:\n'
        '{\n'
        '  "summary_fa": "خلاصه روایی نیم‌صفحه‌ای",\n'
        '  "main_theme": "موضوع اصلی به فارسی (یک جمله)"\n'
        '}\n\n'
        f"متن:\n{text[:5000]}"
    )
    raw = await chat([{"role": "user", "content": prompt}], temperature=0.4, json_mode=True)
    try:
        return _parse_json(raw)
    except Exception:
        return {"summary_fa": raw[:500], "main_theme": ""}


async def generate_infographic(text: str, summary_fa: str = '', title: str = '') -> dict:
    prompt = (
        "این محتوای رسانه‌ای را تحلیل کن و یک اینفوگرافیک ساختاریافته بساز.\n"
        f"عنوان: {title or 'بدون عنوان'}\n"
        f"خلاصه: {summary_fa or ''}\n\n"
        "JSON برگردان با این کلیدها:\n"
        "{\n"
        '  "main_theme": "موضوع اصلی به فارسی",\n'
        '  "topic_map": [{"topic": "...", "subtopics": ["...", "..."], "weight": 1-5}],\n'
        '  "timeline": [{"phase": "opening|early|middle|late|conclusion", "topic_fa": "...", "insight_fa": "..."}],\n'
        '  "key_concepts": [{"term_fa": "...", "type": "person|place|concept|org|event", "mentions": 1}],\n'
        '  "style_fa": "سبک محتوا (مثلاً: آموزشی، روایی، تحلیلی)",\n'
        '  "audience_fa": "مخاطب هدف"\n'
        "}\n\n"
        f"متن:\n{text[:3000]}"
    )
    raw = await chat([{"role": "user", "content": prompt}], temperature=0.4, json_mode=True)
    try:
        return _parse_json(raw)
    except Exception:
        return {"main_theme": summary_fa[:100] if summary_fa else '', "topic_map": []}


async def extract_mentions(text: str) -> dict:
    prompt = (
        "اشخاص، کتاب‌ها، مکان‌ها و سازمان‌های ذکر شده در این محتوا را استخراج کن.\n"
        "JSON برگردان:\n"
        '{\n'
        '  "persons": [{"name_fa": "...", "role_fa": "...", "mentions": N}],\n'
        '  "books": [{"title_fa": "...", "author_fa": "...", "mentions": N}],\n'
        '  "places": [{"name_fa": "...", "type_fa": "...", "mentions": N}],\n'
        '  "orgs": [{"name_fa": "...", "type_fa": "...", "mentions": N}]\n'
        "}\n\n"
        "فقط موارد صریحاً ذکر شده را بیاور. آرایه‌های خالی برای موارد یافت‌نشده.\n\n"
        f"متن:\n{text[:5000]}"
    )
    raw = await chat([{"role": "user", "content": prompt}], temperature=0.2, json_mode=True)
    try:
        data = _parse_json(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


async def mark_sacred_segments(segments: list) -> list:
    """Return seg_index list for segments containing sacred/religious Arabic text."""
    items = [{'i': s.get('seg_index', i), 't': (s.get('text') or '').strip()}
             for i, s in enumerate(segments) if s.get('text')][:300]
    prompt = (
        "بخش‌هایی از رونویسی که مستقیماً شامل متن عربی دینی هستند را شناسایی کن:\n"
        "- آیات قرآنی (عربی)\n- احادیث یا روایات (عربی)\n- دعاها یا اذکار عربی\n"
        "فقط اگر متن عربی دینی است شامل کن. متن فارسی یا توضیح دینی را شامل نکن.\n"
        'JSON برگردان: {"sacred": [i1, i2, ...]}\n\n'
        f"بخش‌ها:\n{json.dumps(items, ensure_ascii=False)}"
    )
    raw = await chat([{"role": "user", "content": prompt}], json_mode=True, temperature=0.1)
    try:
        data = _parse_json(raw)
        result = data.get('sacred', [])
        return [int(x) for x in result if isinstance(x, (int, float))]
    except Exception:
        return []


async def mark_external_quotes(segments: list) -> list:
    """Return seg_index list for segments where speaker quotes external sources (orange)."""
    items = [{'i': s.get('seg_index', i), 't': (s.get('text') or '').strip()}
             for i, s in enumerate(segments) if s.get('text')][:300]
    prompt = (
        "بخش‌هایی از رونویسی را شناسایی کن که در آن‌ها گوینده مستقیماً از شخص دیگر، کتاب، "
        "مقاله یا منبع خارجی نقل‌قول می‌کند.\n"
        "نقل‌قول مستقیم = گوینده کلام شخص دیگری را می‌خواند یا می‌آورد.\n"
        "اگر گوینده خودش صحبت می‌کند، شامل نکن.\n"
        'JSON برگردان: {"quotes": [i1, i2, ...]}\n\n'
        f"بخش‌ها:\n{json.dumps(items, ensure_ascii=False)}"
    )
    raw = await chat([{"role": "user", "content": prompt}], json_mode=True, temperature=0.1)
    try:
        data = _parse_json(raw)
        result = data.get('quotes', [])
        return [int(x) for x in result if isinstance(x, (int, float))]
    except Exception:
        return []


# ── Artwork ────────────────────────────────────────────────────────────────────

async def generate_artwork(title: str, summary_fa: str, thumbnail_url: str = '',
                           thumbnail_b64: str = '', thumbnail_mime: str = 'image/jpeg') -> str:
    """
    Generate artwork image.
    1. Try Gemini image model (returns PNG as base64).
    2. Fall back to LLM SVG generation.
    """
    if thumbnail_b64 and IMAGE_URL and IMAGE_MODEL:
        result = await _gemini_image_from_thumbnail(title, summary_fa, thumbnail_b64, thumbnail_mime)
        if result:
            return result

    if not thumbnail_b64 and IMAGE_URL and IMAGE_MODEL:
        result = await _gemini_image_from_text(title, summary_fa)
        if result:
            return result

    if thumbnail_b64:
        return _image_svg(title, thumbnail_b64, thumbnail_mime)

    return await _llm_svg_artwork(title, summary_fa)


async def _gemini_image_from_thumbnail(title: str, summary_fa: str,
                                       image_b64: str, image_mime: str) -> str:
    """Ask Gemini to create artwork inspired by the thumbnail image."""
    url = IMAGE_URL.rstrip('/') + '/images/generations'
    prompt = (
        f"Create a visually striking thumbnail artwork for media titled: {title}\n"
        f"Summary: {summary_fa[:200] if summary_fa else ''}\n"
        "Style: modern, clean, high-contrast. Keep key visual elements from the reference."
    )
    payload = {
        "model": IMAGE_MODEL,
        "prompt": prompt,
        "n": 1,
        "size": "1024x1024",
        "response_format": "b64_json",
    }
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(url, json=payload, headers=_HEADERS())
            if r.status_code == 200:
                data = r.json()
                b64 = data['data'][0].get('b64_json', '')
                if b64:
                    return f"data:image/png;base64,{b64}"
    except Exception as exc:
        log.warning("Gemini image from thumbnail failed: %s", exc)
    return ''


async def _gemini_image_from_text(title: str, summary_fa: str) -> str:
    """Generate artwork from summary text using Gemini image model."""
    url = IMAGE_URL.rstrip('/') + '/images/generations'
    prompt = (
        f"Create a visually striking thumbnail artwork for: {title}\n"
        f"Topic: {summary_fa[:300] if summary_fa else 'media content'}\n"
        "Style: modern digital art, vibrant colors, abstract composition that represents the theme."
    )
    payload = {
        "model": IMAGE_MODEL,
        "prompt": prompt,
        "n": 1,
        "size": "1024x1024",
        "response_format": "b64_json",
    }
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(url, json=payload, headers=_HEADERS())
            if r.status_code == 200:
                data = r.json()
                b64 = data['data'][0].get('b64_json', '')
                if b64:
                    return f"data:image/png;base64,{b64}"
    except Exception as exc:
        log.warning("Gemini image from text failed: %s", exc)
    return ''


async def _llm_svg_artwork(title: str, summary_fa: str) -> str:
    prompt = (
        "Create a beautiful abstract SVG artwork (600x400 viewBox) for a media item.\n"
        "Style: modern geometric, rich colors, visually distinctive.\n"
        "No text, no labels — pure visual art only.\n"
        "Use gradients, shapes, and patterns. Must look like a professional thumbnail.\n\n"
        f"Media title: {title}\n"
        f"Topic: {summary_fa[:200] if summary_fa else 'general media'}\n\n"
        "Output ONLY the SVG element (starting with <svg and ending with </svg>). No explanation."
    )
    raw = await chat([{"role": "user", "content": prompt}], temperature=0.7, max_tokens=2048)
    m = re.search(r'<svg[\s\S]*?</svg>', raw, re.IGNORECASE)
    return m.group() if m else _fallback_svg(title)


def _image_svg(title: str, image_b64: str, image_mime: str) -> str:
    safe_title = title.replace('<', '&lt;').replace('>', '&gt;').replace('&', '&amp;')
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 600 400">
  <image href="data:{image_mime};base64,{image_b64}" x="0" y="0" width="600" height="400"
         preserveAspectRatio="xMidYMid slice"/>
  <defs>
    <linearGradient id="ov" x1="0" y1="0" x2="0" y2="1">
      <stop offset="40%" stop-color="rgba(0,0,0,0)"/>
      <stop offset="100%" stop-color="rgba(0,0,0,0.82)"/>
    </linearGradient>
  </defs>
  <rect width="600" height="400" fill="url(#ov)"/>
  <text x="300" y="368" text-anchor="middle" font-family="Geeza Pro,Tahoma,serif"
        font-size="22" fill="#f0e0b0" opacity="0.95">{safe_title}</text>
</svg>'''


def _fallback_svg(title: str) -> str:
    letter = title[0] if title else 'ب'
    hue = sum(ord(c) for c in title) % 360
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 600 400">
  <defs>
    <radialGradient id="bg" cx="40%" cy="40%" r="70%">
      <stop offset="0%" stop-color="hsl({hue},45%,22%)"/>
      <stop offset="100%" stop-color="hsl({(hue+40)%360},35%,10%)"/>
    </radialGradient>
  </defs>
  <rect width="600" height="400" fill="url(#bg)"/>
  <circle cx="300" cy="200" r="140" fill="none" stroke="hsl({hue},50%,50%)" stroke-width="1" opacity="0.4"/>
  <circle cx="300" cy="200" r="90" fill="none" stroke="hsl({hue},50%,55%)" stroke-width="0.7" opacity="0.3"/>
  <polygon points="300,90 370,210 230,210" fill="none" stroke="hsl({hue},60%,65%)" stroke-width="1" opacity="0.5"/>
  <polygon points="300,310 370,190 230,190" fill="none" stroke="hsl({hue},60%,65%)" stroke-width="1" opacity="0.5"/>
  <text x="300" y="215" text-anchor="middle" font-family="serif" font-size="72"
        fill="hsl({hue},60%,75%)" opacity="0.55">{letter}</text>
</svg>'''
