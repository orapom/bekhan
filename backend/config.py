"""Load API configs from .env (URL per line) and .apikey files."""
import os
import re

ENV_FILE    = os.getenv('API_URLS_FILE', os.path.join(os.path.dirname(__file__), '../.env'))
APIKEY_FILE = os.getenv('API_KEY_FILE',  os.path.join(os.path.dirname(__file__), '../.apikey'))

REDIS_URL  = os.getenv('REDIS_URL', 'redis://localhost:6379/0')
DB_PATH    = os.getenv('DB_PATH',   os.path.join(os.path.dirname(__file__), '../data/bekhan.db'))
MEDIA_DIR  = os.getenv('MEDIA_DIR', os.path.join(os.path.dirname(__file__), '../data/media'))
AUDIO_DIR  = os.getenv('AUDIO_DIR', os.path.join(os.path.dirname(__file__), '../data/audio'))

# Optional: set BEKHAN_SECRET to require API key on write endpoints (for Chrome extension)
BEKHAN_SECRET = os.getenv('BEKHAN_SECRET', '')


def _load_model_urls():
    urls = {}
    try:
        with open(ENV_FILE) as f:
            for line in f:
                line = line.strip()
                if not line.startswith('http'):
                    continue
                m = re.search(r'/gateway/models/([^/]+)/', line)
                if m:
                    urls[m.group(1)] = line
                    continue
                m2 = re.search(r'#\s*(\S+)', line)
                if m2:
                    urls[m2.group(1)] = line.split('#')[0].strip()
    except FileNotFoundError:
        pass
    return urls


def _load_apikey():
    try:
        with open(APIKEY_FILE) as f:
            line = f.read().strip()
            return line[7:] if line.startswith('apikey ') else line
    except FileNotFoundError:
        return ''


MODEL_URLS = _load_model_urls()
API_KEY    = _load_apikey()


def get_llm_url():
    for m in ['DeepSeek-V3.2', 'DeepSeek-V3.1', 'GLM-4.6', 'Claude-Haiku-4.5', 'Qwen3-30B-A3B', 'Gemini-3.1-Flash-Lite-Preview']:
        if m in MODEL_URLS:
            return MODEL_URLS[m], m
    return None, None


def get_embed_url():
    for m in ['Bge-m3', 'Gemini-embedding-001', 'Embedding-3-Large']:
        if m in MODEL_URLS:
            return MODEL_URLS[m], m
    return None, None


def get_asr_url():
    for m in ['mlx-whisper-local', 'GPT-4o-Transcribe', 'Whisper-1', 'Xerxes-1']:
        if m in MODEL_URLS:
            return MODEL_URLS[m], m
    return None, None


def get_image_url():
    """Get Gemini image generation model URL."""
    for m in MODEL_URLS:
        if any(k in m.lower() for k in ('imagen', 'gemini-2.0-flash-preview-image', 'gemini-flash-image', 'image-gen')):
            return MODEL_URLS[m], m
    return None, None


LLM_URL,   LLM_MODEL   = get_llm_url()
EMBED_URL, EMBED_MODEL = get_embed_url()
ASR_URL,   ASR_MODEL   = get_asr_url()
IMAGE_URL, IMAGE_MODEL = get_image_url()
