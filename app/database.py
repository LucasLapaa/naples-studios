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
        # v8: lotes gratuitos, sem cobrança, ligados ao histórico individual.
        if 'batch_id' not in columns:
            db.execute('ALTER TABLE jobs ADD COLUMN batch_id TEXT')
        db.execute('CREATE INDEX IF NOT EXISTS idx_jobs_batch ON jobs(discord_user_id,batch_id)')
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
        # v10: chave Roblox individual do Discord, criptografada via settings_store.
        db.execute('''CREATE TABLE IF NOT EXISTS user_roblox_credentials (
            discord_id TEXT PRIMARY KEY, secret_payload BLOB NOT NULL,
            creator_type TEXT NOT NULL, creator_id TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )''')
        # v7: monitor de moderação e coleções são separados do status de upload.
        for col in (
            "moderation_state TEXT NOT NULL DEFAULT 'unchecked'",
            'moderation_checked_at TEXT',
            'moderation_message TEXT',
        ):
            name=col.split()[0]
            if name not in columns:
                db.execute(f'ALTER TABLE jobs ADD COLUMN {col}')
        db.execute('''CREATE TABLE IF NOT EXISTS collections (
            id TEXT PRIMARY KEY, discord_user_id TEXT NOT NULL,
            name TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )''')
        db.execute('''CREATE TABLE IF NOT EXISTS collection_items (
            collection_id TEXT NOT NULL, job_id TEXT NOT NULL,
            PRIMARY KEY(collection_id, job_id),
            FOREIGN KEY(collection_id) REFERENCES collections(id)
        )''')
        db.execute('CREATE INDEX IF NOT EXISTS idx_col_owner ON collections(discord_user_id, created_at)')
        db.execute('CREATE INDEX IF NOT EXISTS idx_ci_job ON collection_items(job_id)')
        db.execute('CREATE INDEX IF NOT EXISTS idx_jobs_owner ON jobs(discord_user_id, created_at)')
        prune(db)


def prune(db):
    db.execute('DELETE FROM sessions WHERE expires_at < ?', (int(time.time()),))
    db.execute('DELETE FROM oauth_states WHERE expires_at < ?', (int(time.time()),))


def update_job(job_id: str, **fields):
    allowed = {'status', 'stage', 'asset_id', 'operation', 'error', 'original_duration', 'final_duration', 'size_bytes', 'moderation_state', 'moderation_checked_at', 'moderation_message'}
    fields = {k: v for k, v in fields.items() if k in allowed}
    if not fields:
        return
    with connect() as db:
        db.execute('UPDATE jobs SET '+', '.join(f'{key}=?' for key in fields)+", updated_at=datetime('now') WHERE id=?", [*fields.values(), job_id])


def count_active_jobs(owner_id):
    with connect() as db:
        return db.execute('''SELECT COUNT(*) AS n FROM jobs WHERE discord_user_id=?
            AND status IN ('fila','processando','enviando','confirmando')''',
            (owner_id,)).fetchone()['n']


def count_batch_jobs(batch_id, owner_id):
    with connect() as db:
        return db.execute('SELECT COUNT(*) AS n FROM jobs WHERE batch_id=? AND discord_user_id=?',
                          (batch_id, owner_id)).fetchone()['n']


def create_job(job_id, owner_id, title, source_type, reference, creator_id, creator_type, batch_id=None):
    with connect() as db:
        db.execute('''INSERT INTO jobs (id,discord_user_id,title,source_type,source_reference,
            creator_id,creator_type,status,stage,batch_id) VALUES (?,?,?,?,?,?,?,'fila','Na fila de processamento',?)''',
            (job_id, owner_id, title, source_type, reference, creator_id, creator_type, batch_id))


def create_batch_jobs(owner_id, batch_id, records, creator_id, creator_type):
    # Insere todas as faixas do lote em uma transação.
    with connect() as db:
        db.executemany('''INSERT INTO jobs
            (id,discord_user_id,title,source_type,source_reference,creator_id,creator_type,status,stage,batch_id)
            VALUES (?,?,?,?,?,?,?,'fila','Na fila de processamento',?)''',
            [(rec['id'], owner_id, rec['title'], rec['platform'], rec['url'], creator_id,
              creator_type, batch_id) for rec in records])


def get_job(job_id, user_id):
    with connect() as db:
        return db.execute('SELECT * FROM jobs WHERE id=? AND discord_user_id=?', (job_id, user_id)).fetchone()


def list_jobs(user_id):
    with connect() as db:
        return db.execute('SELECT * FROM jobs WHERE discord_user_id=? ORDER BY created_at DESC, rowid DESC LIMIT 250', (user_id,)).fetchall()
