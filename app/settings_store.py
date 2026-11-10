"""Segredos e preferencias persistentes, configurados exclusivamente por rotas autenticadas.

AES-128-CBC? Não: Fernet (AES-128-CBC + HMAC-SHA256) da lib cryptography.
Chave fica exclusivamente em data/.naples-master.key (0600), nunca no navegador.
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
from pathlib import Path
from cryptography.fernet import Fernet

SENSITIVE = {'discord_client_secret', 'roblox_api_key', 'session_secret'}
DEFAULTS = {
    'app_base_url': 'http://127.0.0.1:8000', 'cookie_secure': False,
    'discord_client_id': '', 'discord_client_secret': '',
    'discord_allowed_user_ids': '', 'roblox_api_key': '', 'roblox_creator_id': '',
    'roblox_creator_type': 'user', 'roblox_allowed_creator_ids': '',
    'enable_platform_links': True,
}


def db_path():
    from . import config
    return config.DATA / 'rbxwave.sqlite3'


def key_path():
    from . import config
    return config.DATA / '.naples-master.key'


def connect():
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=20)
    con.row_factory = sqlite3.Row
    return con


def initialize():
    with connect() as c:
        c.execute('CREATE TABLE IF NOT EXISTS app_settings (setting_key TEXT PRIMARY KEY, payload BLOB NOT NULL)')
        c.execute('CREATE TABLE IF NOT EXISTS admin_config (id INTEGER PRIMARY KEY CHECK (id=1), passhash TEXT NOT NULL)')
        c.execute('CREATE TABLE IF NOT EXISTS admin_sessions (token_hash TEXT PRIMARY KEY, csrf_hash TEXT NOT NULL, expires_at INTEGER NOT NULL)')
        c.execute('DELETE FROM admin_sessions WHERE expires_at < ?', (int(time.time()),))
        c.commit()


def setup_code_path():
    from . import config
    return config.DATA / '.naples-setup-code'


def setup_code():
    """Código temporário, apenas para ativação via rede/Docker."""
    p = setup_code_path()
    if not p.exists():
        try:
            fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, 'w', encoding='ascii') as f:
                f.write(secrets.token_urlsafe(24))
    return p.read_text(encoding='ascii').strip()


def _cipher():
    p = key_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, 'wb') as f:
            f.write(Fernet.generate_key())
    return Fernet(p.read_bytes())


def has_admin():
    with connect() as c:
        return bool(c.execute('SELECT 1 FROM admin_config WHERE id=1').fetchone())


def password_hash(password):
    salt = secrets.token_bytes(32)
    result = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, 650_000)
    return 'pbkdf2_sha256$650000$' + base64.b64encode(salt).decode() + '$' + base64.b64encode(result).decode()


def verify_password(password):
    with connect() as c:
        row = c.execute('SELECT passhash FROM admin_config WHERE id=1').fetchone()
    if not row:
        return False
    try:
        algo, count, salt, digest = row['passhash'].split('$')
        if algo != 'pbkdf2_sha256':
            return False
        expected = base64.b64decode(digest)
        given = hashlib.pbkdf2_hmac('sha256', password.encode(), base64.b64decode(salt), int(count))
        return hmac.compare_digest(given, expected)
    except (ValueError, TypeError):
        return False


def bootstrap(password):
    if len(password) < 12 or len(password) > 256:
        raise ValueError('Defina uma senha administrativa com 12 a 256 caracteres.')
    _cipher()  # garante a chave segura antes de marcar administrador como criado
    with connect() as c:
        # INSERT OR IGNORE atômico: somente uma criação, mesmo com duas requisições simultâneas.
        result = c.execute('INSERT OR IGNORE INTO admin_config (id, passhash) VALUES (1, ?)', (password_hash(password),))
        if result.rowcount != 1:
            raise ValueError('O administrador já foi configurado.')
        c.commit()
    # O segredo das sessões é gerado aqui automaticamente; usuário não precisa conhecer .env.
    set_values({'session_secret': secrets.token_urlsafe(64)})
    setup_code_path().unlink(missing_ok=True)


def change_password(new_password):
    if len(new_password) < 12 or len(new_password) > 256:
        raise ValueError('A nova senha deve ter entre 12 e 256 caracteres.')
    with connect() as c:
        c.execute('UPDATE admin_config SET passhash=? WHERE id=1', (password_hash(new_password),))
        c.execute('DELETE FROM admin_sessions')
        c.commit()


def set_values(values):
    cipher = _cipher()
    with connect() as c:
        for key, value in values.items():
            if key not in DEFAULTS and key != 'session_secret':
                raise ValueError('Campo de configuração não reconhecido.')
            # Todos os valores são criptografados para proteção de metadata/credenciais em backups do BD.
            encrypted = cipher.encrypt(json.dumps(value, ensure_ascii=False).encode())
            c.execute('INSERT INTO app_settings(setting_key,payload) VALUES (?,?) ON CONFLICT(setting_key) DO UPDATE SET payload=excluded.payload', (key, encrypted))
        c.commit()


def read_values():
    with connect() as c:
        rows = c.execute('SELECT setting_key, payload FROM app_settings').fetchall()
    if not rows:
        return {}
    cipher = _cipher()
    return {r['setting_key']: json.loads(cipher.decrypt(r['payload']).decode()) for r in rows}


def public_values():
    settings = read_values()
    out = {k: settings.get(k, DEFAULTS[k]) for k in DEFAULTS if k not in SENSITIVE}
    out['discord_client_secret_configured'] = bool(settings.get('discord_client_secret'))
    out['roblox_api_key_configured'] = bool(settings.get('roblox_api_key'))
    return out


def new_admin_session(csrf_hash):
    token = secrets.token_urlsafe(48)
    expires = int(time.time()) + 2 * 3600
    with connect() as c:
        c.execute('INSERT INTO admin_sessions(token_hash,csrf_hash,expires_at) VALUES (?,?,?)', (hashlib.sha256(token.encode()).hexdigest(), csrf_hash, expires))
        c.commit()
    return token


def admin_session(token):
    if not token or len(token) < 32:
        return None
    with connect() as c:
        return c.execute('SELECT * FROM admin_sessions WHERE token_hash=? AND expires_at>?', (hashlib.sha256(token.encode()).hexdigest(), int(time.time()))).fetchone()


def expire_admin_session(token):
    with connect() as c:
        c.execute('DELETE FROM admin_sessions WHERE token_hash=?', (hashlib.sha256(token.encode()).hexdigest(),))
        c.commit()


def save_discord_roblox_key(discord_id: str, api_key: str, creator_type: str, creator_id: str):
    # Encrypt each operator Roblox credentials at rest; never return the key.
    if not discord_id.isdigit() or creator_type not in ('user','group') or not creator_id.isdigit():
        raise ValueError('Identificadores de credencial inválidos.')
    payload = _cipher().encrypt(json.dumps({'api_key': api_key}).encode('utf-8'))
    with connect() as con:
        con.execute('''INSERT INTO user_roblox_credentials
            (discord_id, secret_payload, creator_type, creator_id) VALUES (?, ?, ?, ?)
            ON CONFLICT(discord_id) DO UPDATE SET secret_payload=excluded.secret_payload,
            creator_type=excluded.creator_type, creator_id=excluded.creator_id''',
            (discord_id, payload, creator_type, creator_id))
        con.commit()

def load_discord_roblox_key(discord_id: str):
    with connect() as con:
        row = con.execute('SELECT * FROM user_roblox_credentials WHERE discord_id=?', (discord_id,)).fetchone()
    if row is None: return None
    data = json.loads(_cipher().decrypt(row['secret_payload']).decode('utf-8'))
    return {'api_key':data['api_key'], 'creator_type':row['creator_type'], 'creator_id':row['creator_id']}

def delete_discord_roblox_key(discord_id: str):
    # Encrypted tombstone prevents fallback to the legacy shared key.
    payload = _cipher().encrypt(json.dumps({'api_key': ''}).encode('utf-8'))
    with connect() as con:
        con.execute('''INSERT INTO user_roblox_credentials
            (discord_id, secret_payload, creator_type, creator_id) VALUES (?, ?, 'user', '')
            ON CONFLICT(discord_id) DO UPDATE SET secret_payload=excluded.secret_payload,
            creator_type='user', creator_id='' ''', (discord_id, payload))
        con.commit()
