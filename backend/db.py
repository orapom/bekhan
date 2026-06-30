"""SQLite schema and helpers for bekhan."""
import os
import sqlite3
import uuid
import json
from datetime import datetime, timezone
from config import DB_PATH

os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS items (
    id              TEXT PRIMARY KEY,
    type            TEXT NOT NULL DEFAULT 'video',   -- video | audio
    source          TEXT NOT NULL DEFAULT 'upload',  -- youtube | aparat | direct | upload
    external_id     TEXT,
    title           TEXT,
    title_fa        TEXT,
    url_source      TEXT,
    url_thumbnail   TEXT,
    file_path       TEXT,
    duration_sec    REAL,
    date_published  TEXT,
    language        TEXT DEFAULT 'fa',
    collections_json TEXT DEFAULT '[]',
    tags_json       TEXT DEFAULT '[]',
    status          TEXT NOT NULL DEFAULT 'indexed',
    error_msg       TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS transcript_segments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id     TEXT NOT NULL,
    language    TEXT NOT NULL DEFAULT 'fa',
    seg_index   INTEGER NOT NULL,
    start_sec   REAL,
    end_sec     REAL,
    text        TEXT,
    words_json  TEXT,
    FOREIGN KEY (item_id) REFERENCES items(id)
);
CREATE INDEX IF NOT EXISTS idx_seg_item ON transcript_segments(item_id, language);

CREATE TABLE IF NOT EXISTS ai_content (
    item_id      TEXT NOT NULL,
    content_type TEXT NOT NULL,
    language     TEXT NOT NULL DEFAULT 'fa',
    content      TEXT,
    model_id     TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (item_id, content_type, language)
);

CREATE TABLE IF NOT EXISTS pipeline_state (
    item_id      TEXT NOT NULL,
    step         TEXT NOT NULL,
    language     TEXT NOT NULL DEFAULT '_',
    status       TEXT DEFAULT 'pending',
    started_at   TEXT,
    done_at      TEXT,
    error_msg    TEXT,
    model_used   TEXT,
    progress_pct INTEGER DEFAULT 0,
    PRIMARY KEY (item_id, step, language)
);
"""


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        for migration in [
            "ALTER TABLE pipeline_state ADD COLUMN model_used TEXT",
            "ALTER TABLE pipeline_state ADD COLUMN progress_pct INTEGER DEFAULT 0",
            "ALTER TABLE transcript_segments ADD COLUMN speaker TEXT",
        ]:
            try:
                conn.execute(migration)
                conn.commit()
            except Exception:
                pass


def _now():
    return datetime.now(timezone.utc).isoformat()


def upsert_item(data: dict) -> str:
    conn = get_conn()
    try:
        sid = data.get('id')
        ext = data.get('external_id')
        url = data.get('url_source')

        existing = None
        if sid:
            existing = conn.execute("SELECT id FROM items WHERE id=?", (sid,)).fetchone()
        if not existing and ext:
            existing = conn.execute("SELECT id FROM items WHERE external_id=?", (ext,)).fetchone()
        if not existing and url:
            existing = conn.execute("SELECT id FROM items WHERE url_source=?", (url,)).fetchone()

        if existing:
            sid = existing['id']
            updateable = ['type', 'source', 'title', 'title_fa', 'url_source', 'url_thumbnail',
                          'file_path', 'duration_sec', 'date_published', 'language',
                          'collections_json', 'tags_json', 'status', 'error_msg']
            updates = [(f, data[f]) for f in updateable if f in data]
            if updates:
                set_clause = ', '.join(f"{f}=?" for f, _ in updates)
                vals = [v for _, v in updates] + [_now(), sid]
                conn.execute(f"UPDATE items SET {set_clause}, updated_at=? WHERE id=?", vals)
        else:
            if not sid:
                sid = str(uuid.uuid4())
            cols = ['id', 'type', 'source', 'external_id', 'title', 'title_fa',
                    'url_source', 'url_thumbnail', 'file_path', 'duration_sec',
                    'date_published', 'language', 'collections_json', 'tags_json',
                    'status', 'created_at', 'updated_at']
            merged = {'id': sid, 'created_at': _now(), 'updated_at': _now(), **data}
            present = [c for c in cols if c in merged]
            conn.execute(
                f"INSERT INTO items ({','.join(present)}) VALUES ({','.join('?'*len(present))})",
                [merged[c] for c in present]
            )
        conn.commit()
        return sid
    finally:
        conn.close()


def set_status(item_id: str, status: str, error_msg: str = None):
    conn = get_conn()
    try:
        conn.execute(
            "UPDATE items SET status=?, error_msg=?, updated_at=? WHERE id=?",
            (status, error_msg, _now(), item_id)
        )
        conn.commit()
    finally:
        conn.close()


def save_segments(item_id: str, language: str, segments: list):
    conn = get_conn()
    try:
        conn.execute(
            "DELETE FROM transcript_segments WHERE item_id=? AND language=?",
            (item_id, language)
        )
        rows = []
        for i, s in enumerate(segments):
            words = s.get('words')
            words_json = json.dumps(words, ensure_ascii=False) if words else None
            rows.append((item_id, language, i, s.get('start'), s.get('end'),
                         str(s.get('text', '')), words_json))
        conn.executemany(
            "INSERT INTO transcript_segments "
            "(item_id, language, seg_index, start_sec, end_sec, text, words_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows
        )
        conn.commit()
    finally:
        conn.close()


def get_transcript_text(item_id: str, language: str = 'fa') -> str:
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT text FROM transcript_segments WHERE item_id=? AND language=? ORDER BY seg_index",
            (item_id, language)
        ).fetchall()
        return ' '.join(r['text'] for r in rows if r['text'])
    finally:
        conn.close()


def save_ai_content(item_id: str, content_type: str, language: str,
                    content: str, model_id: str = None):
    conn = get_conn()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO ai_content "
            "(item_id, content_type, language, content, model_id, created_at) VALUES (?,?,?,?,?,?)",
            (item_id, content_type, language, content, model_id, _now())
        )
        conn.commit()
    finally:
        conn.close()


def ai_content_exists(item_id: str, content_type: str, language: str = 'fa') -> bool:
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT 1 FROM ai_content WHERE item_id=? AND content_type=? AND language=?",
            (item_id, content_type, language)
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def get_item(item_id: str) -> dict | None:
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_items(limit: int = 100, source: str = None, status: str = None,
               collection: str = None, tag: str = None) -> list:
    conn = get_conn()
    try:
        clauses = []
        params = []
        if source:
            clauses.append("source=?")
            params.append(source)
        if status:
            clauses.append("status=?")
            params.append(status)
        if collection:
            clauses.append("collections_json LIKE ?")
            params.append(f'%"{collection}"%')
        if tag:
            clauses.append("tags_json LIKE ?")
            params.append(f'%"{tag}"%')
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        rows = conn.execute(
            f"SELECT * FROM items {where} ORDER BY created_at DESC LIMIT ?", params
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def set_pipeline_state(item_id: str, step: str, status: str,
                       language: str = '_', error_msg: str | None = None,
                       model_used: str | None = None, progress_pct: int | None = None):
    conn = get_conn()
    try:
        now = _now()
        if status == 'running':
            existing = conn.execute(
                "SELECT started_at FROM pipeline_state WHERE item_id=? AND step=? AND language=?",
                (item_id, step, language)
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE pipeline_state SET status='running', progress_pct=? "
                    "WHERE item_id=? AND step=? AND language=?",
                    (progress_pct or 0, item_id, step, language)
                )
            else:
                conn.execute(
                    "INSERT INTO pipeline_state "
                    "(item_id, step, language, status, started_at, done_at, error_msg, model_used, progress_pct) "
                    "VALUES (?, ?, ?, 'running', ?, NULL, NULL, NULL, ?)",
                    (item_id, step, language, now, progress_pct or 0)
                )
        else:
            pct = 100 if status == 'done' else (progress_pct or 0)
            conn.execute(
                "INSERT OR REPLACE INTO pipeline_state "
                "(item_id, step, language, status, started_at, done_at, error_msg, model_used, progress_pct) "
                "VALUES (?, ?, ?, ?, "
                "COALESCE((SELECT started_at FROM pipeline_state WHERE item_id=? AND step=? AND language=?), ?), "
                "?, ?, ?, ?)",
                (item_id, step, language, status,
                 item_id, step, language, now,
                 now, error_msg, model_used, pct)
            )
        conn.commit()
    finally:
        conn.close()


def get_pipeline_progress(item_id: str) -> list:
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT step, language, status, started_at, done_at, error_msg, model_used, progress_pct "
            "FROM pipeline_state WHERE item_id=? ORDER BY started_at",
            (item_id,)
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            # compute duration_sec from timestamps
            if d.get('started_at') and d.get('done_at'):
                try:
                    from datetime import datetime, timezone
                    fmt = '%Y-%m-%dT%H:%M:%S.%f+00:00'
                    def _parse(s):
                        for f in (fmt, '%Y-%m-%dT%H:%M:%S+00:00', '%Y-%m-%dT%H:%M:%S.%fZ'):
                            try: return datetime.strptime(s, f).replace(tzinfo=timezone.utc)
                            except: pass
                        return None
                    t0, t1 = _parse(d['started_at']), _parse(d['done_at'])
                    if t0 and t1:
                        d['duration_sec'] = round((t1 - t0).total_seconds(), 1)
                except Exception:
                    d['duration_sec'] = None
            else:
                d['duration_sec'] = None
            result.append(d)
        return result
    finally:
        conn.close()


def transcript_segment_count(item_id: str, language: str = 'fa') -> int:
    conn = get_conn()
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM transcript_segments WHERE item_id=? AND language=?",
            (item_id, language)
        ).fetchone()[0]
    finally:
        conn.close()


def get_all_collections() -> list[str]:
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT DISTINCT collections_json FROM items WHERE collections_json != '[]'"
        ).fetchall()
        seen = set()
        result = []
        for r in rows:
            try:
                for c in json.loads(r['collections_json']):
                    if c and c not in seen:
                        seen.add(c)
                        result.append(c)
            except Exception:
                pass
        return sorted(result)
    finally:
        conn.close()


def get_all_tags() -> list[str]:
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT DISTINCT tags_json FROM items WHERE tags_json != '[]'"
        ).fetchall()
        seen = set()
        result = []
        for r in rows:
            try:
                for t in json.loads(r['tags_json']):
                    if t and t not in seen:
                        seen.add(t)
                        result.append(t)
            except Exception:
                pass
        return sorted(result)
    finally:
        conn.close()


def get_pipeline_stats() -> dict:
    conn = get_conn()
    try:
        steps = ['import', 'transcribe', 'correct', 'translate', 'diarize', 'paragraphs',
                 'summarize', 'mentions', 'infographic', 'sacred', 'quotes', 'artwork']
        total = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        done = conn.execute("SELECT COUNT(*) FROM items WHERE status='done'").fetchone()[0]
        result: dict = {'total': total, 'done': done, 'steps': {}, 'avg_sec': {}, 'last_model': {}}
        for step in steps:
            row = conn.execute(
                "SELECT COUNT(*) as c FROM pipeline_state WHERE step=? AND status='done'", (step,)
            ).fetchone()
            result['steps'][step] = row['c'] if row else 0
            # average duration for done steps that have both timestamps
            avg_row = conn.execute(
                "SELECT AVG((julianday(done_at) - julianday(started_at)) * 86400) as avg "
                "FROM pipeline_state WHERE step=? AND status='done' AND started_at IS NOT NULL AND done_at IS NOT NULL",
                (step,)
            ).fetchone()
            if avg_row and avg_row['avg']:
                result['avg_sec'][step] = round(float(avg_row['avg']), 1)
            # last model used for this step
            mdl_row = conn.execute(
                "SELECT model_used FROM pipeline_state WHERE step=? AND status='done' AND model_used IS NOT NULL "
                "ORDER BY done_at DESC LIMIT 1", (step,)
            ).fetchone()
            if mdl_row and mdl_row['model_used']:
                result['last_model'][step] = mdl_row['model_used']
        return result
    finally:
        conn.close()
