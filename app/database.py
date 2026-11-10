"""SQLite: histórico, sessão persistente e estados OAuth de uso único."""
import sqlite3
import time
from contextlib import contextmanager
from . import config

DB = config.DATA / 'rbxwave.sqlite3'  # mantém compatibilidade com banco anterior

@contextmanager
def connect():
    config.DATA.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB, timeout=20)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA busy_timeout=20000')
    try:
        yield con
        con.commit()
    finally:
        con.close()


def initialize():
    for p in (config.DATA, config.DATA / 'exports', config.DATA / 'tmp'):
        p.mkdir(parents=True, exist_ok=True)
    with connect() as db:
        db.execute('''CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            title TEXT NOT NULL, source_type TEXT NOT NULL, source_reference TEXT NOT NULL,
            creator_id TEXT NOT NULL, creator_type TEXT NOT NULL,
            status TEXT NOT NULL, stage TEXT NOT NULL,
            asset_id TEXT, operation TEXT, error TEXT,
            original_duration REAL, final_duration REAL, size_bytes INTEGER,
            discord_user_id TEXT NOT NULL DEFAULT ''
        )''')
        columns = {r['name'] for r in db.execute('PRAGMA table_info(jobs)').fetchall()}
        if 'discord_user_id' not in columns:
            db.execute("ALTER TABLE jobs ADD COLUMN discord_user_id TEXT NOT NULL DEFAULT ''")
        if config.LEGACY_DISCORD_OWNER_ID:
            db.execute('UPDATE jobs SET discord_user_id=? WHERE discord_user_id=\'\'', (config.LEGACY_DISCORD_OWNER_ID,))
        # Ao reiniciar, uploads com operação já criada são recuperáveis.
        db.execute("UPDATE jobs SET status=CASE WHEN operation IS NULL THEN 'interrompido' ELSE 'pendente' END, stage='Servidor reiniciado; verifique a operação quando necessário' WHERE status IN ('fila','processando','enviando','confirmando')")
        db.execute('''CREATE TABLE IF NOT EXISTS sessions (
            token_hash TEXT PRIMARY KEY, discord_id TEXT NOT NULL,
            username TEXT NOT NULL, avatar_url TEXT NOT NULL,
            csrf_token_hash TEXT NOT NULL, created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL
        )''')
        db.execute('''CREATE TABLE IF NOT EXISTS oauth_states (
            state_hash TEXT PRIMARY KEY, expires_at INTEGER NOT NULL
        )''')
        db.execute('CREATE INDEX IF NOT EXISTS idx_jobs_owner ON jobs(discord_user_id, created_at)')
        prune(db)


def prune(db):
    db.execute('DELETE FROM sessions WHERE expires_at < ?', (int(time.time()),))
    db.execute('DELETE FROM oauth_states WHERE expires_at < ?', (int(time.time()),))


def update_job(job_id: str, **fields):
    allowed = {'status', 'stage', 'asset_id', 'operation', 'error', 'original_duration', 'final_duration', 'size_bytes'}
    fields = {k: v for k, v in fields.items() if k in allowed}
    if not fields:
        return
    with connect() as db:
        db.execute('UPDATE jobs SET '+', '.join(f'{key}=?' for key in fields)+", updated_at=datetime('now') WHERE id=?", [*fields.values(), job_id])


def create_job(job_id, owner_id, title, source_type, reference, creator_id, creator_type):
    with connect() as db:
        db.execute('''INSERT INTO jobs (id,discord_user_id,title,source_type,source_reference,
            creator_id,creator_type,status,stage) VALUES (?,?,?,?,?,?,?,'fila','Na fila de processamento')''',
            (job_id, owner_id, title, source_type, reference, creator_id, creator_type))


def get_job(job_id, user_id):
    with connect() as db:
        return db.execute('SELECT * FROM jobs WHERE id=? AND discord_user_id=?', (job_id, user_id)).fetchone()


def list_jobs(user_id):
    with connect() as db:
        return db.execute('SELECT * FROM jobs WHERE discord_user_id=? ORDER BY created_at DESC, rowid DESC LIMIT 250', (user_id,)).fetchall()
