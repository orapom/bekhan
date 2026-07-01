"""
Backup bekhan's data volume.

- DB: safe hot backup via sqlite3's backup API (works correctly even in WAL
  mode with the app running concurrently). Timestamped snapshots, keeps the
  last N (default 7).
- Media/audio: optional, off by default (can be large). Pass --media to
  also produce a timestamped tar.gz of media/ + audio/.

Run inside the container (so it uses the same /data volume as the app):
    docker compose exec api python backup.py
    docker compose exec api python backup.py --media

Or via the Makefile:
    make backup
    make backup-full
"""
import os
import sys
import glob
import sqlite3
import tarfile
import argparse
from datetime import datetime, timezone

DB_PATH = os.getenv('DB_PATH', '/data/bekhan.db')
MEDIA_DIR = os.getenv('MEDIA_DIR', '/data/media')
AUDIO_DIR = os.getenv('AUDIO_DIR', '/data/audio')
BACKUP_DIR = os.getenv('BACKUP_DIR', '/data/backups')
KEEP = int(os.getenv('BACKUP_KEEP', '7'))


def backup_db(ts: str) -> str:
    if not os.path.exists(DB_PATH):
        raise FileNotFoundError(f"DB not found: {DB_PATH}")
    dest = os.path.join(BACKUP_DIR, f'bekhan-{ts}.db')
    src_conn = sqlite3.connect(DB_PATH)
    dst_conn = sqlite3.connect(dest)
    try:
        with dst_conn:
            src_conn.backup(dst_conn)
    finally:
        src_conn.close()
        dst_conn.close()
    return dest


def backup_media(ts: str) -> str:
    dest = os.path.join(BACKUP_DIR, f'media-{ts}.tar.gz')
    with tarfile.open(dest, 'w:gz') as tar:
        for d in (MEDIA_DIR, AUDIO_DIR):
            if os.path.isdir(d) and os.listdir(d):
                tar.add(d, arcname=os.path.basename(d))
    return dest


def prune(pattern: str, keep: int):
    if keep <= 0:
        return
    files = sorted(glob.glob(os.path.join(BACKUP_DIR, pattern)))
    for f in files[:-keep]:
        try:
            os.remove(f)
            print(f"  removed old backup: {os.path.basename(f)}")
        except Exception as e:
            print(f"  could not remove {f}: {e}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--media', action='store_true',
                        help='also back up media/ + audio/ (can be large)')
    parser.add_argument('--keep', type=int, default=KEEP,
                        help=f'number of snapshots to retain (default {KEEP})')
    args = parser.parse_args()

    os.makedirs(BACKUP_DIR, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')

    try:
        db_dest = backup_db(ts)
        size_mb = os.path.getsize(db_dest) / 1e6
        print(f"DB backup:    {db_dest} ({size_mb:.1f} MB)")
    except Exception as e:
        print(f"DB backup FAILED: {e}", file=sys.stderr)
        sys.exit(1)

    if args.media:
        media_dest = backup_media(ts)
        size_mb = os.path.getsize(media_dest) / 1e6
        print(f"Media backup: {media_dest} ({size_mb:.1f} MB)")
        prune('media-*.tar.gz', args.keep)

    prune('bekhan-*.db', args.keep)
    print(f"Done. Keeping last {args.keep} snapshot(s) per type in {BACKUP_DIR}")


if __name__ == '__main__':
    main()
