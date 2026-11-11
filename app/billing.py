"""Assinatura Naples Studios (Pix Mercado Pago).

O preço e a identidade de proprietário são validados no servidor.
Somente a API oficial confirma pagamentos; nenhum campo enviado pelo navegador concede acesso.
"""
import hmac
import hashlib
import os
import re
import sqlite3
import time
import uuid
from decimal import Decimal, InvalidOperation

import httpx
from . import database as db

OWNER_DISCORD_ID = '1143357188111339643'
PRICE = Decimal('50.00')
PERIOD = 30 * 24 * 3600
MP_API = 'https://api.mercadopago.com/v1/payments'


class BillingError(Exception):
    pass


def enabled() -> bool:
    return os.getenv('NAPLES_BILLING_ENABLED', '0').strip().lower() in ('1', 'true', 'yes', 'on')


def configured() -> bool:
    return bool(os.getenv('MP_ACCESS_TOKEN', '').strip() and os.getenv('MP_WEBHOOK_SECRET', '').strip())


def init_db():
    with db.connect() as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS billing_orders (
            order_id TEXT PRIMARY KEY,
            discord_id TEXT NOT NULL,
            mp_payment_id TEXT UNIQUE,
            state TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL,
            qr_code TEXT NOT NULL DEFAULT '',
            qr_base64 TEXT NOT NULL DEFAULT '',
            ticket_url TEXT NOT NULL DEFAULT ''
        )''')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_billing_orders_user ON billing_orders(discord_id,created_at)')
        conn.execute('''CREATE TABLE IF NOT EXISTS billing_entitlements (
            mp_payment_id TEXT PRIMARY KEY,
            discord_id TEXT NOT NULL,
            starts_at INTEGER NOT NULL,
            ends_at INTEGER NOT NULL,
            revoked_at INTEGER
        )''')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_billing_access ON billing_entitlements(discord_id,ends_at)')


def access_info(discord_id: str, now=None) -> dict:
    now = int(time.time()) if now is None else int(now)
    if discord_id == OWNER_DISCORD_ID:
        return {'enabled': enabled(), 'active': True, 'lifetime': True, 'expires_at': None,
                'price_brl': '50.00', 'configured': configured()}
    if not enabled():
        return {'enabled': False, 'active': True, 'lifetime': False, 'expires_at': None,
                'price_brl': '50.00', 'configured': configured()}
    with db.connect() as conn:
        row = conn.execute('''SELECT MAX(ends_at) AS until_at FROM billing_entitlements
            WHERE discord_id=? AND revoked_at IS NULL AND starts_at <= ? AND ends_at > ?''',
            (discord_id, now, now)).fetchone()
    expiry = row['until_at'] if row else None
    return {'enabled': True, 'active': bool(expiry), 'lifetime': False,
            'expires_at': expiry, 'price_brl': '50.00', 'configured': configured()}


def _headers(idempotency=None):
    token = os.getenv('MP_ACCESS_TOKEN', '').strip()
    if not token:
        raise BillingError('Mercado Pago não configurado no Railway (MP_ACCESS_TOKEN).')
    headers = {'Authorization': 'Bearer ' + token, 'Accept': 'application/json'}
    if idempotency:
        headers['X-Idempotency-Key'] = idempotency
        headers['Content-Type'] = 'application/json'
    return headers


def _request(method, url, **kwargs):
    try:
        with httpx.Client(timeout=httpx.Timeout(20.0, connect=8.0), follow_redirects=False) as client:
            r = client.request(method, url, headers=_headers(kwargs.pop('idempotency', None)), **kwargs)
        if r.status_code != 200 and r.status_code != 201:
            raise BillingError(f'Mercado Pago não concluiu a operação (HTTP {r.status_code}). Confira credenciais e dados do pagador.')
        data = r.json()
        if not isinstance(data, dict):
            raise BillingError('Resposta inválida do Mercado Pago.')
        return data
    except (httpx.RequestError, ValueError) as exc:
        raise BillingError('Não foi possível consultar o Mercado Pago. Tente novamente.') from exc


def _pix_fields(payment):
    info = (payment.get('point_of_interaction') or {}).get('transaction_data') or {}
    pix = info.get('qr_code') or ''
    qr = info.get('qr_code_base64') or ''
    ticket = info.get('ticket_url') or ''
    if len(pix) > 4096 or len(qr) > 2000000 or len(ticket) > 2000:
        raise BillingError('Resposta Pix fora do tamanho esperado.')
    if ticket and not re.match(r'^https://(?:www\.)?mercadopago\.com\.br/(?:sandbox/)?payments/', ticket):
        ticket = ''
    if qr and not re.fullmatch('[a-zA-Z0-9+/=]+', qr):
        qr = ''
    return pix, qr, ticket


def _order_response(row):
    if not row or not row['mp_payment_id']:
        return None
    return {'payment_id': row['mp_payment_id'], 'state': row['state'],
            'qr_code': row['qr_code'], 'qr_base64': row['qr_base64'],
            'ticket_url': row['ticket_url'], 'expires_at': row['expires_at']}


def current_order(discord_id: str, now=None):
    now = int(time.time()) if now is None else int(now)
    with db.connect() as conn:
        row = conn.execute('''SELECT * FROM billing_orders WHERE discord_id=?
            AND state IN ('creating','pending','in_process') AND expires_at>?
            ORDER BY created_at DESC LIMIT 1''', (discord_id, now)).fetchone()
    return row


def start_pix(discord_id: str, email: str, cpf: str):
    if not enabled() or discord_id == OWNER_DISCORD_ID or access_info(discord_id)['active']:
        raise BillingError('A assinatura já está liberada ou a cobrança está desativada.')
    if not configured():
        raise BillingError('Pagamento indisponível: configure MP_ACCESS_TOKEN e MP_WEBHOOK_SECRET no Railway.')
    email = (email or '').strip()
    cpf = re.sub(r'\D', '', cpf or '')
    if len(email) > 254 or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email):
        raise BillingError('Informe um e-mail válido para o pagamento.')
    if not _valid_cpf(cpf):
        raise BillingError('Informe um CPF válido para emitir o Pix.')
    now = int(time.time())
    # BEGIN IMMEDIATE protege contra dois cliques simultâneos no mesmo Railway.
    with db.connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute('''SELECT * FROM billing_orders WHERE discord_id=?
            AND state IN ('creating','pending','in_process') AND expires_at>?
            ORDER BY created_at DESC LIMIT 1''', (discord_id, now)).fetchone()
        if row and row['mp_payment_id']:
            return _order_response(row)
        if row and now - row['created_at'] < 15:
            raise BillingError('Pix em criação. Aguarde alguns segundos antes de tentar novamente.')
        if row:
            order_id = row['order_id']  # Reutiliza chave idempotente se houve timeout.
        else:
            order_id = 'naples_' + uuid.uuid4().hex
            conn.execute('''INSERT INTO billing_orders (order_id,discord_id,state,created_at,expires_at)
                VALUES (?,?,'creating',?,?)''', (order_id, discord_id, now, now + 24*3600))
    # Documento vai SOMENTE ao gateway; não é armazenado no Naples.
    payment = _request('POST', MP_API, idempotency=order_id, json={
        'transaction_amount': 50.0,
        'description': 'Naples Studios - 30 dias',
        'payment_method_id': 'pix',
        'external_reference': order_id,
        'payer': {'email': email, 'identification': {'type': 'CPF', 'number': cpf}}
    })
    payment_id = str(payment.get('id') or '')
    if not payment_id.isdigit() or payment.get('external_reference') != order_id:
        raise BillingError('O gateway retornou uma identificação inesperada. Não houve liberação de acesso.')
    pix, qr, ticket = _pix_fields(payment)
    if not pix and not ticket:
        raise BillingError('O gateway não retornou os dados Pix. Consulte a configuração da conta Mercado Pago.')
    state = str(payment.get('status') or 'pending')
    with db.connect() as conn:
        conn.execute('''UPDATE billing_orders SET mp_payment_id=?,state=?,qr_code=?,qr_base64=?,ticket_url=?
            WHERE order_id=? AND discord_id=?''', (payment_id, state, pix, qr, ticket, order_id, discord_id))
    return {'payment_id': payment_id, 'state': state, 'qr_code': pix,
            'qr_base64': qr, 'ticket_url': ticket, 'expires_at': now + 24*3600}


def _valid_cpf(cpf):
    if len(cpf) != 11 or len(set(cpf)) == 1 or not cpf.isdigit():
        return False
    for length in (9, 10):
        digit = (sum(int(cpf[i])*(length+1-i) for i in range(length)) * 10) % 11
        if digit == 10: digit = 0
        if digit != int(cpf[length]): return False
    return True


def reconcile(payment_id: str):
    """Consulta fonte de verdade; nenhuma confirmação originada do frontend é aceita."""
    payment_id = str(payment_id)
    if not re.fullmatch(r'[0-9]{1,25}', payment_id):
        return False
    with db.connect() as conn:
        order = conn.execute('SELECT * FROM billing_orders WHERE mp_payment_id=?', (payment_id,)).fetchone()
    if not order:
        return False
    payment = _request('GET', f'{MP_API}/{payment_id}')
    # Rejeita crédito cruzado, fraude por ID, montante ou forma de pagamento errada.
    try:
        valid_amount = Decimal(str(payment.get('transaction_amount'))) == PRICE
    except (InvalidOperation, TypeError):
        valid_amount = False
    if (str(payment.get('id')) != payment_id or payment.get('external_reference') != order['order_id']
            or not valid_amount or payment.get('currency_id') != 'BRL'
            or payment.get('payment_method_id') != 'pix'):
        raise BillingError('A confirmação não corresponde ao pedido Pix original.')
    state = str(payment.get('status') or '')
    now = int(time.time())
    with db.connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        conn.execute('UPDATE billing_orders SET state=? WHERE mp_payment_id=?', (state, payment_id))
        if state == 'approved':
            existing = conn.execute('SELECT mp_payment_id FROM billing_entitlements WHERE mp_payment_id=?', (payment_id,)).fetchone()
            if not existing:
                until = conn.execute('''SELECT MAX(ends_at) AS until_at FROM billing_entitlements
                    WHERE discord_id=? AND revoked_at IS NULL''', (order['discord_id'],)).fetchone()['until_at']
                starts = max(now, int(until or 0))
                conn.execute('''INSERT INTO billing_entitlements (mp_payment_id,discord_id,starts_at,ends_at,revoked_at)
                    VALUES (?,?,?,?,NULL)''', (payment_id, order['discord_id'], starts, starts + PERIOD))
        elif state in ('refunded','charged_back','cancelled'):
            conn.execute('UPDATE billing_entitlements SET revoked_at=COALESCE(revoked_at,?) WHERE mp_payment_id=?', (now, payment_id))
    return True


def check_latest(discord_id):
    row = current_order(discord_id)
    if row and row['mp_payment_id']:
        reconcile(str(row['mp_payment_id']))
    return access_info(discord_id)


def verify_webhook(signature: str, request_id: str, data_id: str, secret: str, now=None):
    if not (signature and request_id and secret and re.fullmatch(r'[0-9]{1,25}', data_id or '')):
        return False
    fields = dict(part.strip().split('=',1) for part in signature.split(',') if '=' in part)
    ts, v1 = fields.get('ts',''), fields.get('v1','')
    if not ts.isdigit() or not re.fullmatch(r'[a-fA-F0-9]{64}', v1):
        return False
    now = int(time.time()) if now is None else int(now)
    if abs(now-int(ts)) > 600:
        return False
    manifest = f'id:{data_id};request-id:{request_id};ts:{ts};'
    expected = hmac.new(secret.encode(), manifest.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, v1.lower())
