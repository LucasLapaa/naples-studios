"""Administração de acesso por Discord ID. Exclusivo do proprietário autenticado.

Não modifica cobranças do Mercado Pago; liberações manuais são independentes.
"""
import re
import time
from . import database as db

OWNER_ID = '1143357188111339643'
MONTH = 30 * 86400


def initialize():
    with db.connect() as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS admin_access_grants (
            discord_id TEXT PRIMARY KEY, grant_type TEXT NOT NULL
            CHECK(grant_type IN ('lifetime','30days')),
            granted_at INTEGER NOT NULL, expires_at INTEGER,
            updated_at INTEGER NOT NULL
        )''')
        conn.execute('''CREATE TABLE IF NOT EXISTS admin_access_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            admin_discord_id TEXT NOT NULL, subject_discord_id TEXT NOT NULL,
            action TEXT NOT NULL, created_at INTEGER NOT NULL
        )''')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_access_audit_time ON admin_access_audit(created_at DESC)')


def owner(discord_id: str) -> bool:
    return str(discord_id) == OWNER_ID


def valid_id(discord_id: str) -> bool:
    return bool(re.fullmatch(r'[1-9][0-9]{5,19}', str(discord_id)))


def grant(discord_id: str, kind: str, actor: str, now=None) -> dict:
    if not owner(actor):
        raise PermissionError('Operação restrita ao proprietário.')
    if not valid_id(discord_id) or kind not in ('lifetime','30days','revoke'):
        raise ValueError('ID Discord ou tipo de acesso inválido.')
    if discord_id == OWNER_ID:
        raise ValueError('Acesso do proprietário é permanente e não pode ser modificado.')
    now = int(time.time()) if now is None else int(now)
    expires = now + MONTH if kind == '30days' else None
    with db.connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        exists = conn.execute('SELECT 1 FROM discord_users WHERE discord_id=?', (discord_id,)).fetchone()
        if not exists:
            raise LookupError('Esse Discord ID ainda não entrou no Naples Studios.')
        # Liberações feitas pelo comando do Railway antes do painel podem ter
        # vencimento em 2100. A ação administrativa substitui apenas essas
        # concessões manuais; pagamentos reais e seu histórico ficam intactos.
        conn.execute('''UPDATE billing_entitlements SET revoked_at=COALESCE(revoked_at,?)
            WHERE discord_id=? AND mp_payment_id LIKE 'admin-grant-%' AND revoked_at IS NULL''',
            (now, discord_id))
        if kind == 'revoke':
            conn.execute('DELETE FROM admin_access_grants WHERE discord_id=?', (discord_id,))
        else:
            conn.execute('''INSERT INTO admin_access_grants(discord_id,grant_type,granted_at,expires_at,updated_at)
                VALUES(?,?,?,?,?) ON CONFLICT(discord_id) DO UPDATE SET
                grant_type=excluded.grant_type, granted_at=excluded.granted_at,
                expires_at=excluded.expires_at, updated_at=excluded.updated_at''',
                (discord_id, kind, now, expires, now))
        conn.execute('''INSERT INTO admin_access_audit(admin_discord_id,subject_discord_id,action,created_at)
            VALUES(?,?,?,?)''', (actor, discord_id, 'grant_'+kind, now))
    return {'discord_id': discord_id, 'grant_type': kind, 'expires_at': expires}


def ban(discord_id: str, suspended: bool, actor: str, now=None) -> dict:
    if not owner(actor):
        raise PermissionError('Operação restrita ao proprietário.')
    if not valid_id(discord_id) or not isinstance(suspended, bool):
        raise ValueError('ID Discord ou opção de banimento inválida.')
    if discord_id == OWNER_ID:
        raise ValueError('O proprietário não pode ser banido.')
    now = int(time.time()) if now is None else int(now)
    with db.connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        result = conn.execute('UPDATE discord_users SET suspended=? WHERE discord_id=?',
                              (int(suspended), discord_id))
        if not result.rowcount:
            raise LookupError('Esse Discord ID ainda não entrou no Naples Studios.')
        if suspended:
            conn.execute('DELETE FROM sessions WHERE discord_id=?', (discord_id,))
        conn.execute('''INSERT INTO admin_access_audit(admin_discord_id,subject_discord_id,action,created_at)
            VALUES(?,?,?,?)''', (actor, discord_id, 'ban' if suspended else 'unban', now))
    return {'discord_id': discord_id, 'suspended': suspended}


def list_users(query='', page=1, page_size=30, now=None) -> dict:
    """Lista paginada e estado financeiro calculado apenas por registros oficiais.

    ``pagou`` significa que há um pagamento Pix confirmado, mesmo expirado.
    ``pagamento_ativo`` significa prazo vigente de pagamento não revogado.
    """
    now = int(time.time()) if now is None else int(now)
    page = max(1, min(int(page), 100000))
    page_size = max(1, min(int(page_size), 100))
    query = str(query or '').strip()[:100]
    if query:
        # Busca literal para '%' e '_' não se tornarem curingas de SQL LIKE.
        escaped = query.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
        where = "WHERE (u.discord_id LIKE ? ESCAPE '\\' OR u.username LIKE ? ESCAPE '\\')"
        args = (f'%{escaped}%', f'%{escaped}%')
    else:
        where = ''
        args = ()
    with db.connect() as conn:
        total = conn.execute(f'SELECT COUNT(*) AS n FROM discord_users u {where}', args).fetchone()['n']
        rows = conn.execute(f'''SELECT u.discord_id, u.username, u.avatar_url,
            u.created_at, u.last_login, u.suspended,
            g.grant_type, g.expires_at AS grant_expires_at,
            (SELECT MAX(e.ends_at) FROM billing_entitlements e
              JOIN billing_orders o ON o.mp_payment_id=e.mp_payment_id
              WHERE e.discord_id=u.discord_id AND e.revoked_at IS NULL
                AND o.state='approved') AS paid_until,
            (SELECT MAX(e.ends_at) FROM billing_entitlements e
              WHERE e.discord_id=u.discord_id AND e.revoked_at IS NULL
                AND e.starts_at<=? AND e.mp_payment_id LIKE 'admin-grant-%') AS legacy_until,
            (SELECT MAX(e.ends_at) FROM billing_entitlements e
              WHERE e.discord_id=u.discord_id AND e.revoked_at IS NULL
                AND e.starts_at<=?) AS entitlement_until,
            (SELECT COUNT(*) FROM billing_orders o
              WHERE o.discord_id=u.discord_id AND o.state='approved') AS confirmed_payments
            FROM discord_users u LEFT JOIN admin_access_grants g ON g.discord_id=u.discord_id
            {where} ORDER BY u.last_login DESC, u.discord_id DESC
            LIMIT ? OFFSET ?''', (now, now, *args, page_size, (page-1)*page_size)).fetchall()
        events = conn.execute('''SELECT subject_discord_id,action,created_at FROM admin_access_audit
            ORDER BY id DESC LIMIT 8''').fetchall()
    users = []
    for r in rows:
        d = dict(r)
        lifetime = d['discord_id'] == OWNER_ID or d['grant_type'] == 'lifetime'
        grant_active = d['grant_type'] == '30days' and bool(d['grant_expires_at'] and d['grant_expires_at'] > now)
        expiry = max((int(e) for e in (d['grant_expires_at'] if grant_active else None,
                                      d['entitlement_until']) if e and e > now), default=None)
        d['lifetime'] = lifetime
        d['paid'] = bool(d['confirmed_payments'])
        d['payment_active'] = bool(d['paid_until'] and d['paid_until'] > now)
        d['active'] = bool(lifetime or expiry)
        d['expires_at'] = None if lifetime else expiry
        d['status'] = ('banido' if d['suspended'] else 'vitalicio' if lifetime else
                       '30dias' if grant_active else 'pago' if d['payment_active'] else
                       'liberado' if expiry else 'sem_acesso')
        # Não exibir o identificador interno dos recibos, QR Codes, CPF nem cookies.
        d.pop('entitlement_until', None)
        d.pop('confirmed_payments', None)
        users.append(d)
    return {'users': users, 'page': page, 'page_size': page_size,
            'pages': max(1, (total + page_size-1)//page_size),
            'total': total, 'events': [dict(e) for e in events]}
