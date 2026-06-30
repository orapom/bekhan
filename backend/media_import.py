"""yt-dlp integration and media source detection for bekhan."""
import os
import re
import json
import logging
import tempfile

log = logging.getLogger(__name__)

MEDIA_EXTS = {'mp4', 'webm', 'mkv', 'avi', 'mov', 'flv', 'mp3', 'wav', 'ogg', 'm4a', 'aac', 'flac', 'opus'}

PLATFORM_PATTERNS = {
    'youtube': [r'youtube\.com', r'youtu\.be'],
    'aparat':  [r'aparat\.com'],
    'vimeo':   [r'vimeo\.com'],
    'soundcloud': [r'soundcloud\.com'],
    'dailymotion': [r'dailymotion\.com'],
}


def detect_source(url: str) -> str:
    for platform, patterns in PLATFORM_PATTERNS.items():
        if any(re.search(p, url, re.I) for p in patterns):
            return platform
    ext = url.split('?')[0].rsplit('.', 1)[-1].lower()
    if ext in MEDIA_EXTS:
        return 'direct'
    return 'direct'


def extract_info(url: str) -> dict:
    """Extract metadata without downloading. Returns yt-dlp info dict."""
    import yt_dlp
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'extract_flat': False,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
    return info or {}


def download_audio(url: str, output_path: str) -> str:
    """Download audio to output_path (no extension). Returns actual file path."""
    import yt_dlp
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'format': 'bestaudio/best',
        'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3'}],
        'outtmpl': output_path,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

    for ext in ('mp3', 'm4a', 'webm', 'ogg', 'opus', 'wav'):
        p = f"{output_path}.{ext}" if not output_path.endswith(f'.{ext}') else output_path
        if os.path.exists(p) and os.path.getsize(p) > 0:
            return p
    if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
        return output_path
    raise FileNotFoundError(f"Audio download failed for {url}")


def download_subtitles(url: str, item_id: str, output_dir: str) -> list[dict]:
    """
    Download subtitles from URL (YouTube auto-generated + manual).
    Returns list of {language, path, format} dicts.
    """
    import yt_dlp
    os.makedirs(output_dir, exist_ok=True)
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'skip_download': True,
        'writesubtitles': True,
        'writeautomaticsub': True,
        'subtitleslangs': ['fa', 'en', 'ar'],
        'subtitlesformat': 'vtt/srt/best',
        'outtmpl': os.path.join(output_dir, f'{item_id}.%(ext)s'),
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            ydl.download([url])
        except Exception as e:
            log.warning("subtitle download failed: %s", e)

    result = []
    for fname in os.listdir(output_dir):
        if fname.startswith(item_id) and fname.endswith(('.vtt', '.srt', '.ass')):
            parts = fname.rsplit('.', 2)
            lang = parts[-2] if len(parts) >= 3 else 'unknown'
            result.append({
                'language': lang,
                'path': os.path.join(output_dir, fname),
                'format': parts[-1]
            })
    return result


def parse_subtitle_file(path: str) -> list[dict]:
    """Parse VTT or SRT subtitle file into segments [{start, end, text}]."""
    with open(path, encoding='utf-8', errors='replace') as f:
        content = f.read()

    segments = []
    if path.endswith('.vtt'):
        segments = _parse_vtt(content)
    elif path.endswith('.srt'):
        segments = _parse_srt(content)
    return segments


def _ts_to_sec(ts: str) -> float:
    ts = ts.strip().replace(',', '.')
    parts = ts.split(':')
    try:
        if len(parts) == 3:
            return float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
        elif len(parts) == 2:
            return float(parts[0]) * 60 + float(parts[1])
    except Exception:
        pass
    return 0.0


def _parse_vtt(content: str) -> list[dict]:
    segs = []
    lines = content.splitlines()
    i = 0
    while i < len(lines):
        if '-->' in lines[i]:
            times = lines[i].split('-->')
            start = _ts_to_sec(times[0].strip().split()[-1])
            end = _ts_to_sec(times[1].strip().split()[0])
            texts = []
            i += 1
            while i < len(lines) and lines[i].strip():
                t = re.sub(r'<[^>]+>', '', lines[i]).strip()
                if t:
                    texts.append(t)
                i += 1
            if texts:
                segs.append({'start': start, 'end': end, 'text': ' '.join(texts)})
        i += 1
    return segs


def _parse_srt(content: str) -> list[dict]:
    segs = []
    blocks = re.split(r'\n\n+', content.strip())
    for block in blocks:
        lines = block.strip().splitlines()
        if len(lines) < 3:
            continue
        time_line = None
        for l in lines:
            if '-->' in l:
                time_line = l
                break
        if not time_line:
            continue
        times = time_line.split('-->')
        start = _ts_to_sec(times[0])
        end = _ts_to_sec(times[1].split()[0])
        text_lines = [l for l in lines if '-->' not in l and not l.strip().isdigit()]
        text = ' '.join(text_lines).strip()
        text = re.sub(r'<[^>]+>', '', text).strip()
        if text:
            segs.append({'start': start, 'end': end, 'text': text})
    return segs


def get_stream_url(url: str) -> str:
    """
    Get a fresh direct stream URL (for Aparat + other HLS sources).
    Prefers HLS/m3u8; falls back to best available URL.
    """
    import yt_dlp
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best',
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)

    if not info:
        return ''

    formats = info.get('formats') or []
    for f in formats:
        if f.get('protocol') in ('m3u8', 'm3u8_native', 'hls') and f.get('url'):
            return f['url']
    for f in reversed(formats):
        if f.get('url'):
            return f['url']
    return info.get('url', '')


def build_item_meta(url: str, info: dict) -> dict:
    """Convert yt-dlp info dict to bekhan item fields."""
    source = detect_source(url)
    ext_id = info.get('id') or ''
    title = info.get('title') or info.get('fulltitle') or ''
    thumbnail = info.get('thumbnail') or ''
    duration = info.get('duration')
    upload_date = info.get('upload_date') or ''
    if upload_date and len(upload_date) == 8:
        upload_date = f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:]}"

    media_type = 'audio'
    for f in (info.get('formats') or []):
        if f.get('vcodec') and f['vcodec'] != 'none':
            media_type = 'video'
            break
    if info.get('vcodec') and info['vcodec'] != 'none':
        media_type = 'video'

    return {
        'type': media_type,
        'source': source,
        'external_id': ext_id,
        'title': title,
        'url_source': url,
        'url_thumbnail': thumbnail,
        'duration_sec': duration,
        'date_published': upload_date,
    }
