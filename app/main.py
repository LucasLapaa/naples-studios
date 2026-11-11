"""Naples Studios - OAuth Discord + produção de áudio + Roblox Open Cloud."""
import asyncio
import hashlib
import ipaddress
import os
import re
import secrets
import shutil
import time
from datetime import datetime, timezone
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlencode, urlparse

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import config, database as db, settings_store as store, provider_guard, billing
from .asset_monitor import inspect_asset
from .audio_audit import audit_audio, AudioAuditError
from .roblox_key import inspect_key, KeyInspectionError, allowed_destination
from .core import (
    MAX_FILE_BYTES, ProcessingError, PlatformAccessError, convert_2_5x, create_roblox_asset,
    download_platform, platform_from_url, normalize_youtube_video_url, preview_platform, poll_roblox_asset, source_reference_for_history, valid_creator_id, valid_title
)

EXPORTS = config.DATA / 'exports'
TEMP = config.DATA / 'tmp'
TASKS: set[asyncio.Task] = set()
QUEUE = asyncio.Semaphore(2)
RATES = defaultdict(deque)
ID_RE = re.compile(r'^[0-9a-f]{32}$')
BATCH_LIMIT = 20           # limite operacional por lote; não é limite de plano pago
MAX_ACTIVE_JOBS = 60       # evita sobrecarregar a única instância do Railway



def hashed(raw: str) -> str:
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def rate(key, max_events, seconds):
    now = time.monotonic()
    recent = RATES[key]
    while recent and recent[0] < now - seconds:
        recent.popleft()
    if len(recent) >= max_events:
        raise HTTPException(429, 'Muitas solicitações. Tente novamente mais tarde.')
    recent.append(now)


def require_session(request: Request, mutate=False):
    raw = request.cookies.get(config.SESSION_COOKIE, '')
    if len(raw) < 25:
        raise HTTPException(401, 'Faça login com o Discord.')
    with db.connect() as conn:
        row = conn.execute('SELECT * FROM sessions WHERE token_hash=? AND expires_at>?', (hashed(raw), int(time.time()))).fetchone()
        suspended = bool(row and db.user_suspended(conn, row['discord_id']))
    if not row or suspended or (not config.DISCORD_PUBLIC_REGISTRATION and row['discord_id'] not in config.DISCORD_ALLOWED_USER_IDS):
        raise HTTPException(401, 'Sessão expirada ou acesso não autorizado.')
    if mutate:
        csrf = request.headers.get('x-csrf-token', '')
        if not csrf or not secrets.compare_digest(hashed(csrf), row['csrf_token_hash']):
            raise HTTPException(403, 'Token CSRF ausente ou inválido.')
        origin = request.headers.get('origin')
        if origin and origin.rstrip('/') != config.APP_BASE_URL:
            raise HTTPException(403, 'Origem da solicitação não autorizada.')
    return row


def profile_avatar(discord_id: str, avatar_hash: str | None) -> str:
    if avatar_hash and re.fullmatch(r'[a-f0-9_]{8,64}', avatar_hash):
        return f'https://cdn.discordapp.com/avatars/{discord_id}/{avatar_hash}.png?size=128'
    return ''


def job_json(row):
    d = dict(row)
    d.pop('discord_user_id', None)
    d['has_audio'] = (EXPORTS / f"{d['id']}.mp3").is_file()
    # Expor um código estável de falha sem alterar o esquema SQLite existente.
    # O conteúdo detalhado do yt-dlp permanece apenas nos logs protegidos.
    match = re.search(r'Falha na fonte \[([A-Z_]+)\]', d.get('stage') or '')
    d['source_error_code'] = match.group(1) if match else None
    return d


def process_job(job_id: str, local: Path | None, url: str, title: str, creator_id: str, creator_type: str, api_key: str):
    work = TEMP / job_id
    work.mkdir(parents=True, exist_ok=True)
    try:
        if local:
            source = local
        else:
            db.update_job(job_id, status='processando', stage='Importando áudio do link autorizado')
            source = download_platform(url, work)
        db.update_job(job_id, status='processando', stage='FFmpeg: pitch 2.5x, EQ 100 Hz–15 kHz, volume normalizado e validação MP3')
        metadata = convert_2_5x(source, EXPORTS / f'{job_id}.mp3')
        db.update_job(job_id, **metadata)
        db.update_job(job_id, status='enviando', stage='Criando asset no Roblox Open Cloud')
        operation = create_roblox_asset(EXPORTS / f'{job_id}.mp3', title, creator_id, creator_type, api_key)
        db.update_job(job_id, status='confirmando', operation=operation, stage='Aguardando Roblox emitir Asset ID')
        asset_id = poll_roblox_asset(operation, api_key)
        if asset_id:
            db.update_job(job_id, status='concluido', asset_id=asset_id, error=None, stage='ID criado; moderação e permissões ainda podem estar pendentes')
        else:
            db.update_job(job_id, status='pendente', stage='Roblox ainda processando. Use Verificar operação.')
    except PlatformAccessError as exc:
        db.update_job(job_id, status='erro', stage=f'Falha na fonte [{exc.code}]', error=str(exc)[:750])
    except ProcessingError as exc:
        db.update_job(job_id, status='erro', stage='Erro de processamento', error=str(exc)[:750])
    except Exception as exc:
        # Não registrar credenciais nem retornar exceções brutas ao frontend.
        db.update_job(job_id, status='erro', stage='Falha técnica', error=f'Erro interno ({type(exc).__name__}). Consulte o terminal do servidor.')
    finally:
        shutil.rmtree(work, ignore_errors=True)


async def run_job(*args):
    async with QUEUE:
        await asyncio.to_thread(process_job, *args)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.initialize()
    billing.init_db()
    store.initialize()
    config.reload_settings()
    # Sessões seguras funcionam mesmo sem conta administrativa/Roblox cadastrada.
    # Se já existir um SESSION_SECRET, não alteramos (mantém sessões existentes).
    if not config.SESSION_SECRET:
        store.set_values({'session_secret': secrets.token_urlsafe(64)})
        config.reload_settings()
    if not store.has_admin() and os.getenv('NAPLES_SHOW_LEGACY_SETUP', '0') == '1':
        print('\n[ NAPLES STUDIOS ] Primeiro acesso pelo navegador: http://127.0.0.1:8000/configuracoes')
        print('[ NAPLES STUDIOS ] Código de ativação (necessário no Docker/acesso via proxy): ' + store.setup_code() + '\n', flush=True)
    yield


app = FastAPI(title='Naples Studios', docs_url=None, redoc_url=None, lifespan=lifespan)
app.mount('/static', StaticFiles(directory=config.ROOT / 'app' / 'static'), name='static')


@app.middleware('http')
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Permissions-Policy'] = 'camera=(), microphone=(), geolocation=()'
    response.headers['Content-Security-Policy'] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' https://cdn.discordapp.com data:; "
        "connect-src 'self'; media-src 'self' blob:; object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
    )
    if request.url.path.startswith(('/api/', '/auth/')):
        response.headers['Cache-Control'] = 'no-store'
    return response


@app.get('/configuracoes')
def configuration_page():
    return FileResponse(config.ROOT / 'app' / 'static' / 'settings.html')


@app.get('/')
def index():
    return FileResponse(config.ROOT / 'app' / 'static' / 'index.html')


# ---------------------------------------------------------------------------
# Painel de configuração privada. Não existe .env obrigatório.
# A criação inicial da senha somente pode ocorrer na máquina local.
# Todas as alterações posteriores exigem sessão admin + proteção CSRF.
# ---------------------------------------------------------------------------

def check_same_origin(request: Request):
    origin = request.headers.get('origin', '')
    if origin and origin.rstrip('/') not in {
        config.APP_BASE_URL, f'{request.url.scheme}://{request.headers.get("host", "")}'.rstrip('/')
    }:
        raise HTTPException(403, 'Origem não permitida.')
    if request.headers.get('sec-fetch-site') == 'cross-site':
        raise HTTPException(403, 'Requisição de outra origem bloqueada.')
    if request.method not in ('GET', 'HEAD') and 'application/json' not in request.headers.get('content-type', ''):
        raise HTTPException(415, 'Use o tipo application/json.')


def requires_setup_code(request: Request):
    """Na conexão direta local não precisa PIN; conexões via Docker/rede exigem PIN."""
    peer = request.client.host if request.client else ''
    host = request.headers.get('host', '').split(':')[0].lower()
    try:
        is_loopback = ipaddress.ip_address(peer).is_loopback
    except ValueError:
        is_loopback = peer == 'testclient'  # cliente de testes, não endereço de rede
    return not (is_loopback and host in ('localhost', '127.0.0.1', 'testserver'))


def authorize_bootstrap(request: Request, supplied_code=''):
    check_same_origin(request)
    if requires_setup_code(request):
        hostname = request.headers.get('host', '').split(':')[0].lower()
        if hostname not in ('localhost', '127.0.0.1') and request.url.scheme != 'https':
            raise HTTPException(403, 'Ativação pela rede exige HTTPS; no Docker local use 127.0.0.1.')
        if not isinstance(supplied_code, str) or not secrets.compare_digest(supplied_code, store.setup_code()):
            raise HTTPException(403, 'Código de ativação inválido. Consulte o terminal do servidor.')


def admin_csrf(token: str) -> str:
    import hmac
    return hmac.new(config.SESSION_SECRET.encode('utf-8'), b'admin-csrf-v1:' + token.encode('utf-8'), 'sha256').hexdigest()


def current_admin(request: Request, mutate=False):
    token = request.cookies.get(config.ADMIN_COOKIE, '')
    entry = store.admin_session(token)
    if not entry:
        raise HTTPException(401, 'Entre com a senha do administrador.')
    if mutate:
        check_same_origin(request)
        csrf = request.headers.get('x-csrf-token', '')
        if not csrf or not secrets.compare_digest(hashed(csrf), entry['csrf_hash']):
            raise HTTPException(403, 'Token CSRF inválido.')
    return token


def admin_cookie(response, token):
    response.set_cookie(config.ADMIN_COOKIE, token, max_age=7200, httponly=True,
                        secure=config.COOKIE_SECURE, samesite='strict', path='/')


@app.get('/api/setup/status')
def setup_status(request: Request):
    return {'bootstrapped': store.has_admin(), 'ready': config.ready(),
            'requires_setup_code': requires_setup_code(request),
            'redirect_uri': config.APP_BASE_URL + '/auth/discord/callback'}


@app.post('/api/setup/bootstrap')
async def bootstrap(request: Request):
    if store.has_admin():
        raise HTTPException(409, 'Administrador já cadastrado.')
    rate('bootstrap:' + (request.client.host if request.client else ''), 5, 3600)
    data = await request.json()
    pwd = data.get('password', '') if isinstance(data, dict) else ''
    authorize_bootstrap(request, data.get('setup_code', '') if isinstance(data, dict) else '')
    if not isinstance(pwd, str):
        raise HTTPException(400, 'Senha inválida.')
    try:
        store.bootstrap(pwd)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    config.reload_settings()
    # Token aleatório armazenado só por hash; CSRF derivado da mesma sessão.
    # Armazenamento de sessão num único passo (a função abaixo gera o token).
    session_token = store.new_admin_session('')
    with store.connect() as conn:
        conn.execute('UPDATE admin_sessions SET csrf_hash=? WHERE token_hash=?',
                     (hashed(admin_csrf(session_token)), hashed(session_token)))
        conn.commit()
    resp = JSONResponse({'ok': True, 'csrf': admin_csrf(session_token)})
    admin_cookie(resp, session_token)
    return resp


@app.post('/api/admin/login')
async def admin_login(request: Request):
    check_same_origin(request)
    if not store.has_admin():
        raise HTTPException(409, 'Crie a senha inicial primeiro.')
    rate('adminlogin:' + (request.client.host if request.client else ''), 7, 900)
    data = await request.json()
    pwd = data.get('password', '') if isinstance(data, dict) else ''
    if not isinstance(pwd, str) or not store.verify_password(pwd):
        raise HTTPException(401, 'Senha incorreta.')
    token = store.new_admin_session('')
    with store.connect() as conn:
        conn.execute('UPDATE admin_sessions SET csrf_hash=? WHERE token_hash=?',
                     (hashed(admin_csrf(token)), hashed(token)))
        conn.commit()
    resp = JSONResponse({'ok': True, 'csrf': admin_csrf(token)})
    admin_cookie(resp, token)
    return resp


@app.get('/api/admin/session')
def admin_session(request: Request):
    token = current_admin(request)
    return {'ok': True, 'csrf': admin_csrf(token), 'ready': config.ready()}


@app.get('/api/admin/settings')
def admin_settings(request: Request):
    current_admin(request)
    values = store.public_values()
    # O admin web também enxerga a configuração efetiva, mesmo quando OAuth foi
    # informado anteriormente pelo Railway, sem revelar tokens sensíveis.
    for key, effective in (
        ('app_base_url', config.APP_BASE_URL),
        ('discord_client_id', config.DISCORD_CLIENT_ID),
    ):
        if not values.get(key) or (key == 'app_base_url' and values[key] == 'http://127.0.0.1:8000'):
            values[key] = effective
    values['cookie_secure'] = config.COOKIE_SECURE
    values['discord_public_registration'] = config.DISCORD_PUBLIC_REGISTRATION
    if 'discord_allowed_user_ids' not in store.read_values():
        values['discord_allowed_user_ids'] = ', '.join(sorted(config.DISCORD_ALLOWED_USER_IDS))
    values['discord_client_secret_configured'] = bool(config.DISCORD_CLIENT_SECRET)
    values['roblox_api_key_configured'] = bool(config.ROBLOX_API_KEY)
    return {'settings': values, 'ready': config.ready(),
            'callback_url': config.APP_BASE_URL + '/auth/discord/callback'}


@app.post('/api/admin/roblox-key/inspect')
async def inspect_roblox_key(request: Request):
    """Validação da API Key sem expô-la em URLs, resposta, logs ou frontend.
    Requer sessão do administrador e CSRF. Não salva a chave automaticamente.
    """
    current_admin(request, mutate=True)
    rate('key-inspect:' + (request.client.host if request.client else 'unknown'), 8, 600)
    data = await request.json()
    if not isinstance(data, dict):
        raise HTTPException(400, 'Informe uma chave Roblox válida.')
    key = data.get('api_key')
    if not key:
        key = config.ROBLOX_API_KEY  # revalidar chave já salva sem revelá-la
    try:
        return await asyncio.to_thread(inspect_key, key)
    except KeyInspectionError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.put('/api/admin/settings')
async def save_admin_settings(request: Request):
    current_admin(request, mutate=True)
    data = await request.json()
    if not isinstance(data, dict):
        raise HTTPException(400, 'Dados inválidos.')
    permitted = set(store.DEFAULTS) | {'discord_client_secret', 'roblox_api_key'}
    if set(data).difference(permitted):
        raise HTTPException(400, 'Campos não reconhecidos.')
    previous = store.read_values()
    values = {**store.DEFAULTS, **previous}
    # Preserva o OAuth funcional importado do Railway quando for alterado o
    # cadastro público sem reenviar o Client Secret.
    if not values.get('discord_client_id'):
        values['discord_client_id'] = config.DISCORD_CLIENT_ID
    if not values.get('discord_client_secret'):
        values['discord_client_secret'] = config.DISCORD_CLIENT_SECRET
    for k, val in data.items():
        if k in ('discord_client_secret', 'roblox_api_key'):
            if val is not None and (not isinstance(val, str) or len(val) > 5000):
                raise HTTPException(400, 'Credencial inválida.')
            if val and val.strip():
                values[k] = val.strip()
        elif k in ('cookie_secure', 'enable_platform_links', 'discord_public_registration'):
            if not isinstance(val, bool):
                raise HTTPException(400, f'{k} deve ser booleano.')
            values[k] = val
        else:
            if not isinstance(val, str) or len(val) > 2048:
                raise HTTPException(400, f'{k} inválido.')
            values[k] = val.strip()
    if not values.get('discord_client_secret') or len(values.get('discord_client_secret')) < 8:
        raise HTTPException(400, 'Preencha o Client Secret do Discord (mínimo 8 caracteres).')
    # Roblox Key é pessoal, vinculada ao Discord no dashboard, não obrigatória no admin.
    if values.get('roblox_api_key') and len(values['roblox_api_key']) < 16:
        raise HTTPException(400, 'API Key Roblox informada é inválida.')
    # Quando uma nova chave é informada, consulta a API oficial de introspecção
    # para descobrir a identidade autorizada sem exigir ID no formulário de upload.
    # Seleção de grupo ocorre SOMENTE aqui quando não houver grupo único.
    if data.get('roblox_api_key'):
        try:
            key_info = await asyncio.to_thread(inspect_key, values['roblox_api_key'])
        except KeyInspectionError as exc:
            raise HTTPException(400, str(exc)) from exc
        if values['roblox_creator_type'] == 'user':
            suggested = key_info['default_creator_id'] if key_info['default_creator_type']=='user' else key_info.get('authorized_user_id')
            if suggested and allowed_destination(key_info, 'user', suggested):
                values['roblox_creator_id'] = suggested
        elif values['roblox_creator_type']=='group':
            # Grupo precisa ser escolhido explicitamente se a chave autoriza muitos.
            group_id = values.get('roblox_creator_id', '')
            if not group_id or (key_info.get('authorized_user_id') and group_id==key_info['authorized_user_id']):
                group_id = key_info.get('suggested_group_id') or ''
            if not group_id:
                raise HTTPException(400, 'A chave não identifica um único grupo. Informe o ID do grupo apenas em Roblox Key.')
            values['roblox_creator_id'] = group_id
        if not allowed_destination(key_info, values['roblox_creator_type'], values.get('roblox_creator_id','')):
            raise HTTPException(400, 'O destino selecionado não possui Assets > Write nesta chave.')
        # antiga lista opcional pode bloquear involuntariamente o destino da chave nova
        if values.get('roblox_allowed_creator_ids'):
            ids=[x.strip() for x in values['roblox_allowed_creator_ids'].split(',')]
            if values['roblox_creator_id'] not in ids:
                values['roblox_allowed_creator_ids']=values['roblox_creator_id']
    try:
        values['app_base_url'] = config.validate_settings(values)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    # Segredos jamais aparecem nas respostas JSON/logs de aplicação.
    store.set_values({k: v for k, v in values.items() if k in permitted})
    config.reload_settings()
    return {'ok': True, 'ready': config.ready(), 'creator_id': config.ROBLOX_CREATOR_ID, 'creator_type': config.ROBLOX_CREATOR_TYPE, 'callback_url': config.APP_BASE_URL + '/auth/discord/callback'}


@app.post('/api/admin/change-password')
async def admin_change_password(request: Request):
    token = current_admin(request, mutate=True)
    data = await request.json()
    old = data.get('old_password', '') if isinstance(data, dict) else ''
    new = data.get('new_password', '') if isinstance(data, dict) else ''
    if not isinstance(old, str) or not store.verify_password(old):
        raise HTTPException(403, 'Senha atual incorreta.')
    try:
        store.change_password(new)
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    resp = JSONResponse({'ok': True})
    resp.delete_cookie(config.ADMIN_COOKIE, path='/')
    return resp


@app.get('/api/admin/users')
def admin_list_users(request: Request):
    current_admin(request)
    with db.connect() as conn:
        rows = conn.execute('SELECT discord_id,username,avatar_url,created_at,last_login,suspended FROM discord_users ORDER BY last_login DESC LIMIT 300').fetchall()
    return {'users': [dict(x) for x in rows]}


@app.put('/api/admin/users/{discord_id}/suspension')
async def admin_suspend_user(discord_id: str, request: Request):
    current_admin(request, mutate=True)
    if not discord_id.isdigit(): raise HTTPException(400, 'ID Discord inválido.')
    payload = await request.json()
    if not isinstance(payload, dict) or not isinstance(payload.get('suspended'), bool):
        raise HTTPException(400, 'Informe suspended como true ou false.')
    suspended = bool(payload['suspended'])
    with db.connect() as conn:
        result = conn.execute('UPDATE discord_users SET suspended=? WHERE discord_id=?', (int(suspended),discord_id))
        if not result.rowcount: raise HTTPException(404, 'Usuário não encontrado.')
        if suspended: conn.execute('DELETE FROM sessions WHERE discord_id=?', (discord_id,))
    return {'ok': True, 'discord_id': discord_id, 'suspended': suspended}


@app.post('/api/admin/logout')
def admin_logout(request: Request):
    token = current_admin(request, mutate=True)
    store.expire_admin_session(token)
    resp = JSONResponse({'ok': True})
    resp.delete_cookie(config.ADMIN_COOKIE, path='/')
    return resp


@app.get('/auth/discord')
def discord_login(request: Request):
    if not config.oauth_ready():
        return RedirectResponse('/?login_error=config', status_code=303)
    rate('login:' + (request.client.host if request.client else 'unknown'), 20, 600)
    state = secrets.token_urlsafe(36)
    with db.connect() as conn:
        db.prune(conn)
        conn.execute('INSERT INTO oauth_states (state_hash,expires_at) VALUES (?,?)', (hashed(state), int(time.time()) + 600))
    params = {
        'client_id': config.DISCORD_CLIENT_ID,
        'response_type': 'code',
        'redirect_uri': config.APP_BASE_URL + '/auth/discord/callback',
        'scope': 'identify',
        'state': state,
        'prompt': 'consent',
    }
    resp = RedirectResponse('https://discord.com/oauth2/authorize?' + urlencode(params), status_code=302)
    resp.set_cookie(config.OAUTH_COOKIE, state, max_age=600, httponly=True, secure=config.COOKIE_SECURE, samesite='lax', path='/auth/discord')
    return resp


@app.get('/auth/discord/callback')
async def discord_callback(request: Request, code: str = '', state: str = '', error: str = ''):
    if error:
        return RedirectResponse('/?login_error=cancelado', status_code=303)
    raw_cookie = request.cookies.get(config.OAUTH_COOKIE, '')
    if not state or not code or len(code) > 2048 or not raw_cookie or not secrets.compare_digest(raw_cookie, state):
        raise HTTPException(400, 'Estado OAuth inválido ou expirado. Faça login novamente.')
    with db.connect() as conn:
        entry = conn.execute('SELECT expires_at FROM oauth_states WHERE state_hash=?', (hashed(state),)).fetchone()
        conn.execute('DELETE FROM oauth_states WHERE state_hash=?', (hashed(state),))
    if not entry or entry['expires_at'] < int(time.time()):
        raise HTTPException(400, 'Estado OAuth expirado ou já utilizado.')
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=8.0), follow_redirects=False) as client:
            token_response = await client.post('https://discord.com/api/v10/oauth2/token', data={
                'client_id': config.DISCORD_CLIENT_ID,
                'client_secret': config.DISCORD_CLIENT_SECRET,
                'grant_type': 'authorization_code',
                'code': code,
                'redirect_uri': config.APP_BASE_URL + '/auth/discord/callback',
            }, headers={'Content-Type': 'application/x-www-form-urlencoded'})
            if token_response.status_code != 200:
                return RedirectResponse('/?login_error=discord', status_code=303)
            access_token = token_response.json().get('access_token', '')
            if not isinstance(access_token, str) or not access_token:
                return RedirectResponse('/?login_error=discord', status_code=303)
            user_response = await client.get('https://discord.com/api/v10/users/@me', headers={'Authorization': 'Bearer ' + access_token})
            if user_response.status_code != 200:
                return RedirectResponse('/?login_error=discord', status_code=303)
            user = user_response.json()
    except (httpx.HTTPError, ValueError, KeyError):
        return RedirectResponse('/?login_error=discord', status_code=303)

    discord_id = str(user.get('id', ''))
    if not discord_id.isdigit() or (not config.DISCORD_PUBLIC_REGISTRATION and discord_id not in config.DISCORD_ALLOWED_USER_IDS):
        return RedirectResponse('/?login_error=acesso', status_code=303)

    username = str(user.get('global_name') or user.get('username') or 'Usuário')[:100]
    avatar = profile_avatar(discord_id, user.get('avatar'))
    session_token = secrets.token_urlsafe(48)
    now = int(time.time())
    with db.connect() as conn:
        db.prune(conn)
        if db.user_suspended(conn, discord_id):
            return RedirectResponse('/?login_error=acesso', status_code=303)
        db.upsert_discord_user(conn, discord_id, username, avatar)
        conn.execute('''INSERT INTO sessions (token_hash,discord_id,username,avatar_url,csrf_token_hash,created_at,expires_at)
                      VALUES (?,?,?,?,?,?,?)''',
                     (hashed(session_token), discord_id, username, avatar, hashed(derive_csrf(session_token)), now, now + config.SESSION_HOURS * 3600))
        # O CSRF é gerado por sessão e devolvido via endpoint autenticado (não em URL).
    resp = RedirectResponse('/#dashboard', status_code=303)
    resp.set_cookie(config.SESSION_COOKIE, session_token, max_age=config.SESSION_HOURS * 3600, httponly=True,
                    secure=config.COOKIE_SECURE, samesite='lax', path='/')
    resp.delete_cookie(config.OAUTH_COOKIE, path='/auth/discord')
    return resp




def current_roblox_credential(discord_id: str):
    user_key = store.load_discord_roblox_key(discord_id)
    if user_key is not None: return user_key if user_key['api_key'] else None
    # Não compartilhar a Key global herdada com usuários de cadastro público.
    # No modo fechado, somente os operadores legados já autorizados mantêm esse fallback.
    if (not config.DISCORD_PUBLIC_REGISTRATION and
            config.ROBLOX_API_KEY and config.ROBLOX_CREATOR_ID and
            discord_id in config.DISCORD_ALLOWED_USER_IDS):
        return {'api_key': config.ROBLOX_API_KEY,
                'creator_type': config.ROBLOX_CREATOR_TYPE,
                'creator_id': config.ROBLOX_CREATOR_ID, 'legacy': True}
    return None


@app.get('/api/auth/availability')
def auth_availability():
    return {'discord_available':config.oauth_ready()}


@app.put('/api/roblox/connection')
async def connect_roblox(request: Request):
    user = authorize_request(request, mutate=True)
    rate('key:' + user['discord_id'], 12, 3600)
    payload = await request.json()
    if not isinstance(payload, dict): raise HTTPException(400, 'Dados inválidos.')
    key = payload.get('api_key', '')
    if not isinstance(key, str) or not 16 <= len(key.strip()) <= 5000:
        raise HTTPException(400, 'Cole sua API Key completa do Roblox Open Cloud.')
    try:
        info = await asyncio.to_thread(inspect_key, key.strip())
    except KeyInspectionError as exc:
        raise HTTPException(400, str(exc)) from exc
    # Para grupos: só selecionar automaticamente se há exatamente um grupo permitido.
    preferred_type, preferred_id = info.get('default_creator_type'), info.get('default_creator_id')
    requested_type = payload.get('creator_type') or preferred_type
    requested_id = str(payload.get('creator_id') or '').strip() or (preferred_id if requested_type == preferred_type else None)
    if requested_type not in ('user','group') or not requested_id:
        raise HTTPException(422, 'Não foi possível determinar um destino único. Informe o ID do grupo nesta aba.')
    if not allowed_destination(info, requested_type, requested_id):
        raise HTTPException(403, 'Essa API Key não tem Assets > Write para o destino selecionado.')
    store.save_discord_roblox_key(user['discord_id'], key.strip(), requested_type, requested_id)
    return {'configured':True, 'creator_type':requested_type, 'creator_id':requested_id,
            'message':'Chave validada e salva com segurança.'}


@app.delete('/api/roblox/connection')
def disconnect_roblox(request: Request):
    user = authorize_request(request, mutate=True)
    store.delete_discord_roblox_key(user['discord_id'])
    return {'configured':False, 'message':'Chave pessoal desconectada.'}

# ---------------------------------------------------------------------------
# Pix mensal - checkout liberado após login Discord, mas sem assinatura.
# A verificação final ocorre sempre na API do Mercado Pago.
# ---------------------------------------------------------------------------

@app.get('/api/billing/status')
def billing_status(request: Request):
    user = require_session(request)
    return {'subscription': billing.access_info(user['discord_id']),
            'payment': billing._order_response(billing.current_order(user['discord_id']))}


@app.post('/api/billing/pix')
async def billing_new_pix(request: Request):
    user = require_session(request, mutate=True)
    rate('billing-create:' + user['discord_id'], 8, 600)
    data = await request.json()
    if not isinstance(data, dict):
        raise HTTPException(400, 'Dados inválidos.')
    try:
        payment = await asyncio.to_thread(billing.start_pix, user['discord_id'],
                                          data.get('email',''), data.get('cpf',''))
    except billing.BillingError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {'payment': payment, 'subscription': billing.access_info(user['discord_id'])}


@app.post('/api/billing/check')
async def billing_check(request: Request):
    user = require_session(request, mutate=True)
    rate('billing-check:' + user['discord_id'], 24, 600)
    try:
        status = await asyncio.to_thread(billing.check_latest, user['discord_id'])
    except billing.BillingError as exc:
        raise HTTPException(503, str(exc)) from exc
    return {'subscription': status, 'payment': billing._order_response(billing.current_order(user['discord_id']))}


@app.post('/api/billing/webhook')
async def billing_webhook(request: Request):
    secret = os.getenv('MP_WEBHOOK_SECRET', '').strip()
    if not secret or not billing.enabled():
        raise HTTPException(503, 'Webhook ainda não configurado.')
    if int(request.headers.get('content-length') or 0) > 16384:
        raise HTTPException(413, 'Notificação muito grande.')
    body = await request.body()
    if len(body) > 16384:
        raise HTTPException(413, 'Notificação muito grande.')
    try:
        payload = __import__('json').loads(body)
    except ValueError:
        raise HTTPException(400, 'JSON inválido.')
    if not isinstance(payload, dict):
        raise HTTPException(400, 'JSON inválido.')
    params = request.query_params
    param_id = params.get('data.id','')
    body_id = str((payload.get('data') or {}).get('id') or '')
    data_id = param_id or body_id
    if param_id and body_id and param_id != body_id:
        raise HTTPException(401, 'Identificador divergente.')
    if not billing.verify_webhook(request.headers.get('x-signature',''),
                                  request.headers.get('x-request-id',''), data_id, secret):
        raise HTTPException(401, 'Assinatura de webhook inválida.')
    if payload.get('type') == 'payment' or str(payload.get('action','')).startswith('payment.'):
        try:
            await asyncio.to_thread(billing.reconcile, data_id)
        except billing.BillingError:
            # Mercado Pago deve reenviar quando sua API estiver indisponível.
            raise HTTPException(503, 'Falha temporária na consulta do pagamento.')
    return {'ok': True}


@app.get('/api/session')
def session(request: Request):
    user = require_session(request)
    subscription = billing.access_info(user['discord_id'])
    roblox = current_roblox_credential(user['discord_id'])
    # CSRF: valor derivado? Guardamos apenas hash; geramos um segundo token de sessão de acesso
    # usando HMAC estável a partir de token de cookie e um segredo de aplicação no servidor.
    return {
        'authenticated': True,
        'billing': subscription,
        'user': {'id': user['discord_id'], 'username': user['username'], 'avatar_url': user['avatar_url']},
        'csrf': derive_csrf(request.cookies[config.SESSION_COOKIE]),
        'config': {
            'platform_links': config.ENABLE_PLATFORM_LINKS, 'speed': 2.5,
            'creator_id': roblox['creator_id'] if roblox else '', 'creator_type': roblox['creator_type'] if roblox else 'user',
            'version': '11.0', 'roblox_connected': bool(roblox)
        }
    }


def derive_csrf(session_token: str):
    # Não serializar o hash do banco; derivação HMAC determinística baseada em segredo do servidor.
    import hmac
    secret = config.SESSION_SECRET.encode('utf8')
    return hmac.new(secret, b'naples-csrf-v1:' + session_token.encode('utf8'), 'sha256').hexdigest()


def authorize_request(request: Request, mutate=False):
    user = require_session(request, mutate=mutate)
    if not billing.access_info(user['discord_id'])['active']:
        raise HTTPException(402, 'Assinatura vencida. Renove seu acesso por Pix para continuar.')
    return user


@app.post('/api/logout')
def logout(request: Request):
    require_session(request, mutate=True)
    with db.connect() as conn:
        conn.execute('DELETE FROM sessions WHERE token_hash=?', (hashed(request.cookies[config.SESSION_COOKIE]),))
    resp = JSONResponse({'ok': True})
    resp.delete_cookie(config.SESSION_COOKIE, path='/')
    return resp


@app.get('/api/system/diagnostics')
def system_diagnostics(request: Request):
    """Ferramentas locais apenas. Não promete acesso ao YouTube/SoundCloud."""
    authorize_request(request)
    versions = {}
    for tool, args in (
        ('ffmpeg', ['ffmpeg', '-version']),
        ('ffprobe', ['ffprobe', '-version']),
        ('yt-dlp', ['yt-dlp', '--version']),
        ('node', ['node', '--version']),
    ):
        try:
            proc = __import__('subprocess').run(args, capture_output=True, text=True, timeout=8)
            raw = (proc.stdout or proc.stderr or '').splitlines()
            versions[tool] = (raw[0][:110] if raw and proc.returncode == 0 else 'erro')
        except (FileNotFoundError, __import__('subprocess').TimeoutExpired):
            versions[tool] = 'indisponível'
    try:
        from importlib.metadata import version, PackageNotFoundError
        versions['yt-dlp-ejs'] = version('yt-dlp-ejs')
    except PackageNotFoundError:
        versions['yt-dlp-ejs'] = 'não instalado (verifique yt-dlp[default])'
    except Exception:
        versions['yt-dlp-ejs'] = 'não foi possível determinar'
    # O domínio público atende ao site/OAuth; a mídia é buscada do IP de saída
    # do Railway, que não é o mesmo endereço de origem da requisição HTTP.
    versions['public_origin'] = config.APP_BASE_URL
    versions['railway_commit'] = os.getenv('RAILWAY_GIT_COMMIT_SHA', '')[:12] or 'não informado'
    versions['railway_deployment'] = os.getenv('RAILWAY_DEPLOYMENT_ID', '')[:12] or 'não informado'
    # Consultar endereço de saída APENAS ao clicar em Diagnóstico (network=1).
    # Nenhuma URL de música, cookie, token ou API Key é enviada nessa chamada.
    if request.query_params.get('network') == '1':
        try:
            with httpx.Client(timeout=httpx.Timeout(4.0, connect=2.5), follow_redirects=False, trust_env=False) as probe:
                response = probe.get('https://api.ipify.org?format=json')
                response.raise_for_status()
                raw_ip = str(response.json().get('ip') or '')
                ipaddress.ip_address(raw_ip)
                versions['outbound_ip'] = raw_ip
        except (httpx.HTTPError, ValueError, TypeError, KeyError):
            versions['outbound_ip'] = 'consulta indisponível'
    else:
        versions['outbound_ip'] = 'não consultado'
    versions['contexto'] = 'Alterar o domínio do site não altera o destino das chamadas do yt-dlp. Redeploys podem mudar o ambiente/IP de saída do Railway.'
    versions['youtube_guard'] = provider_guard.status('YouTube')
    versions['soundcloud_guard'] = provider_guard.status('SoundCloud')
    versions['nota'] = 'A pausa interna do YouTube está desativada por padrão (v21). HTTP 429/LOGIN_REQUIRED são recusas do YouTube e não podem ser liberadas pelo domínio nem pelo login OAuth2.'
    return versions


@app.post('/api/media/preview')
async def media_preview(request: Request):
    user = authorize_request(request, mutate=True)
    rate('preview:' + user['discord_id'], 12, 900)
    if not config.ENABLE_PLATFORM_LINKS:
        raise HTTPException(403, 'Importação de links não está habilitada pelo administrador.')
    data = await request.json()
    url = data.get('url', '') if isinstance(data, dict) else ''
    try:
        if not isinstance(url, str):
            raise ProcessingError('Informe uma URL válida.')
        platform_from_url(url)
        return await asyncio.to_thread(preview_platform, url)
    except ProcessingError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get('/api/jobs')
def jobs(request: Request):
    user = authorize_request(request)
    return {'jobs': [job_json(row) for row in db.list_jobs(user['discord_id'])]}


@app.post('/api/jobs', status_code=202)
async def submit_job(request: Request, title: str = Form(...), creator_id: str = Form(''),
                     creator_type: str = Form(''), source_url: str = Form(''),
                     rights_confirmed: str = Form('false'), batch_id: str = Form(''),
                     audio_file: UploadFile | None = File(None)):
    user = authorize_request(request, mutate=True)
    rate('publish:' + user['discord_id'], 80, 3600)
    try:
        title = valid_title(title)
        connection = current_roblox_credential(user['discord_id'])
        if not connection:
            raise ProcessingError('Conecte sua API Key na aba Roblox Key antes do upload.')
        creator_id = valid_creator_id(connection['creator_id'])
        creator_type = connection['creator_type']
        api_key = connection['api_key']
        if rights_confirmed != 'true':
            raise ProcessingError('Confirme que possui os direitos necessários para processar e publicar o áudio.')
        url = source_url.strip()
        has_file = bool(audio_file and audio_file.filename)
        if bool(url) == has_file:
            raise ProcessingError('Envie um arquivo OU informe um link, não ambos.')
        if url:
            platform = platform_from_url(url)
            if platform == 'YouTube':
                url = normalize_youtube_video_url(url)
            if not config.ENABLE_PLATFORM_LINKS:
                raise ProcessingError('Importação de links está desabilitada no servidor.')
        else:
            platform = 'Arquivo local'
            suffix = Path(audio_file.filename).suffix.lower()
            if suffix not in ('.mp3', '.wav', '.flac', '.ogg', '.m4a', '.webm', '.opus', '.aac'):
                raise ProcessingError('Extensão de arquivo não permitida.')
    except ProcessingError as exc:
        raise HTTPException(400, str(exc)) from exc

    if url and platform in ('YouTube', 'SoundCloud'):
        paused = provider_guard.status(platform)
        if paused['cooldown_seconds']:
            raise HTTPException(429, f'{platform} temporariamente em pausa após bloqueio do provedor. Tente mais tarde; arquivo local continua disponível.',
                                headers={'Retry-After': str(paused['cooldown_seconds'])})

    if db.count_active_jobs(user['discord_id']) >= MAX_ACTIVE_JOBS:
        raise HTTPException(429, 'A fila está cheia. Aguarde a conclusão de algumas músicas.')
    if batch_id:
        if not ID_RE.fullmatch(batch_id):
            raise HTTPException(400, 'Identificador do lote inválido.')
        if db.count_batch_jobs(batch_id, user['discord_id']) >= BATCH_LIMIT:
            raise HTTPException(429, 'Este lote atingiu o limite de 20 músicas.')
    job_id = uuid.uuid4().hex
    local = None
    if has_file:
        work = TEMP / job_id
        work.mkdir(parents=True, exist_ok=True)
        local = work / ('source' + suffix)
        length = 0
        try:
            with local.open('wb') as fd:
                while part := await audio_file.read(1024 * 1024):
                    length += len(part)
                    if length > MAX_FILE_BYTES:
                        raise HTTPException(413, 'Arquivo de origem excede 50 MB.')
                    fd.write(part)
            if not length:
                raise HTTPException(400, 'Arquivo vazio.')
        except Exception:
            shutil.rmtree(work, ignore_errors=True)
            raise
    db.create_job(job_id, user['discord_id'], title, platform, source_reference_for_history(url) if url else (audio_file.filename or 'arquivo'), creator_id, creator_type, batch_id or None)
    task = asyncio.create_task(run_job(job_id, local, url, title, creator_id, creator_type, api_key))
    TASKS.add(task)
    task.add_done_callback(TASKS.discard)
    return {'job_id': job_id, 'status': 'fila', 'batch_id': batch_id or None}




@app.post('/api/batches/links', status_code=202)
async def submit_link_batch(request: Request):
    # Até 20 jobs com links; nunca contorna verificações da fonte.
    user = authorize_request(request, mutate=True)
    rate('batchlinks:' + user['discord_id'], 4, 3600)
    if not config.ENABLE_PLATFORM_LINKS:
        raise HTTPException(403, 'Importação por links desativada nas configurações.')
    try:
        data = await request.json()
        if not isinstance(data, dict) or data.get('rights_confirmed') is not True:
            raise ProcessingError('Confirme os direitos de uso de todas as músicas do lote.')
        connection = current_roblox_credential(user['discord_id'])
        if not connection: raise ProcessingError('Conecte sua Roblox Key antes de enviar o lote.')
        cid = valid_creator_id(connection['creator_id'])
        ctype = connection['creator_type']
        api_key = connection['api_key']
        items = data.get('items')
        if not isinstance(items, list) or not 1 <= len(items) <= BATCH_LIMIT:
            raise ProcessingError('Informe entre 1 e 20 links por lote.')
        if db.count_active_jobs(user['discord_id']) + len(items) > MAX_ACTIVE_JOBS:
            raise HTTPException(429, 'Fila cheia. Aguarde a conclusão de algumas músicas.')
        records=[]
        seen=set()
        for index,item in enumerate(items,1):
            if not isinstance(item,dict):
                raise ProcessingError(f'Linha {index}: entrada inválida.')
            url=item.get('url')
            if not isinstance(url,str):
                raise ProcessingError(f'Linha {index}: link inválido.')
            url=url.strip()
            platform=platform_from_url(url)
            if platform == 'YouTube':
                try:
                    url = normalize_youtube_video_url(url)
                except ProcessingError as exc:
                    raise ProcessingError(f'Linha {index}: {exc}') from exc
            if url in seen:
                raise ProcessingError(f'Linha {index}: link repetido neste lote.')
            seen.add(url)
            name=item.get('title') or f'Naples Studio - faixa {index:02d}'
            title=valid_title(name)
            records.append({'id':uuid.uuid4().hex,'url':url,'title':title,'platform':platform})
    except ProcessingError as exc:
        raise HTTPException(400,str(exc)) from exc
    batch_id=uuid.uuid4().hex
    db.create_batch_jobs(user['discord_id'], batch_id,
        [{**rec, 'url': source_reference_for_history(rec['url'])} for rec in records], cid, ctype)
    for item in records:
        task=asyncio.create_task(run_job(item['id'], None, item['url'], item['title'], cid, ctype, api_key))
        TASKS.add(task)
        task.add_done_callback(TASKS.discard)
    return {'batch_id':batch_id, 'total':len(records),
            'job_ids':[item['id'] for item in records], 'status':'fila'}


@app.get('/api/batches/{batch_id}')
def batch_status(batch_id: str, request: Request):
    user=authorize_request(request)
    if not ID_RE.fullmatch(batch_id):
        raise HTTPException(400,'Identificador inválido.')
    with db.connect() as conn:
        rows=conn.execute('''SELECT * FROM jobs WHERE discord_user_id=? AND batch_id=?
            ORDER BY created_at, rowid''',(user['discord_id'],batch_id)).fetchall()
    if not rows:
        raise HTTPException(404,'Lote não encontrado.')
    counts={'total':len(rows),'concluidos':0,'falhas':0,'ativos':0,'pendentes':0}
    for row in rows:
        if row['status']=='concluido': counts['concluidos']+=1
        elif row['status'] in ('erro','interrompido'): counts['falhas']+=1
        elif row['status']=='pendente': counts['pendentes']+=1
        else: counts['ativos']+=1
    return {'batch_id':batch_id, **counts,'jobs':[job_json(row) for row in rows]}


@app.post('/api/jobs/{job_id}/verify')
async def verify_job(job_id: str, request: Request):
    user = authorize_request(request, mutate=True)
    if not ID_RE.fullmatch(job_id):
        raise HTTPException(400, 'ID inválido.')
    row = db.get_job(job_id, user['discord_id'])
    if not row:
        raise HTTPException(404, 'Não encontrado.')
    if row['status'] not in ('erro', 'pendente', 'interrompido') or not row['operation']:
        raise HTTPException(409, 'Não existe uma operação pendente para verificar.')
    rate('verify:' + user['discord_id'], 20, 3600)
    connection = current_roblox_credential(user['discord_id'])
    if not connection: raise HTTPException(409,'Conecte a Roblox Key para verificar a operação.')
    try:
        asset_id = await asyncio.to_thread(poll_roblox_asset, row['operation'], connection['api_key'], None, 5, 2)
    except ProcessingError as exc:
        raise HTTPException(502, str(exc)) from exc
    if asset_id:
        db.update_job(job_id, status='concluido', stage='ID criado; moderação pode estar pendente', asset_id=asset_id, error=None)
    else:
        db.update_job(job_id, status='pendente', stage='Roblox ainda processando', error=None)
    return {'asset_id': asset_id, 'pending': not bool(asset_id)}


@app.get('/api/jobs/{job_id}/audio-audit')
async def get_audio_audit(job_id: str, request: Request):
    """Analisa exclusivamente áudio já produzido para o usuário autenticado."""
    user = authorize_request(request)
    if not ID_RE.fullmatch(job_id) or not db.get_job(job_id, user['discord_id']):
        raise HTTPException(404, 'Registro não encontrado.')
    path = EXPORTS / f'{job_id}.mp3'
    if not path.is_file():
        raise HTTPException(404, 'MP3 processado indisponível.')
    rate('audio-audit:' + user['discord_id'], 15, 3600)
    try:
        return await asyncio.to_thread(audit_audio, path)
    except AudioAuditError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get('/api/jobs/{job_id}/audio')
def get_audio(job_id: str, request: Request):
    user = authorize_request(request)
    if not ID_RE.fullmatch(job_id) or not db.get_job(job_id, user['discord_id']):
        raise HTTPException(404, 'Não encontrado.')
    path = EXPORTS / f'{job_id}.mp3'
    if not path.is_file():
        raise HTTPException(404, 'Arquivo processado indisponível.')
    return FileResponse(path, media_type='audio/mpeg', filename='naples-' + job_id[:8] + '-2_5x.mp3')


@app.delete('/api/jobs/{job_id}')
def delete_job(job_id: str, request: Request):
    user = authorize_request(request, mutate=True)
    if not ID_RE.fullmatch(job_id):
        raise HTTPException(400, 'ID inválido.')
    row = db.get_job(job_id, user['discord_id'])
    if not row:
        raise HTTPException(404, 'Não encontrado.')
    if row['status'] in ('fila', 'processando', 'enviando', 'confirmando'):
        raise HTTPException(409, 'Aguarde terminar o processamento.')
    with db.connect() as conn:
        conn.execute('DELETE FROM collection_items WHERE job_id=?', (job_id,))
        conn.execute('DELETE FROM jobs WHERE id=? AND discord_user_id=?', (job_id, user['discord_id']))
    (EXPORTS / f'{job_id}.mp3').unlink(missing_ok=True)
    return {'ok': True}

# Naples Studios v7: coleções particulares e monitor de moderação (sem Premium).
@app.get('/api/roblox/connection')
def roblox_connection(request: Request):
    user = authorize_request(request)
    key = current_roblox_credential(user['discord_id'])
    return {'configured': bool(key),
            'creator_type': key['creator_type'] if key else '',
            'creator_id': key['creator_id'] if key else '',
            'note': 'Chave criptografada no servidor e vinculada à conta Discord.'}


@app.post('/api/monitor/check')
async def monitor_check(request: Request):
    user = authorize_request(request, mutate=True)
    rate('moderation:' + user['discord_id'], 6, 3600)
    data = await request.json()
    ids = data.get('job_ids', []) if isinstance(data, dict) else None
    if not isinstance(ids, list) or not 1 <= len(ids) <= 20 or any(not isinstance(i, str) or not ID_RE.fullmatch(i) for i in ids):
        raise HTTPException(400, 'Selecione entre 1 e 20 registros do seu histórico.')
    if len(ids) != len(set(ids)):
        raise HTTPException(400, 'Não repita registros.')
    rows = [db.get_job(job_id, user['discord_id']) for job_id in ids]
    if any(row is None or not row['asset_id'] for row in rows):
        raise HTTPException(404, 'Algum registro não possui Asset ID ou não pertence à sua conta.')
    connection = current_roblox_credential(user['discord_id'])
    if not connection: raise HTTPException(409, 'Conecte a Roblox Key antes de verificar IDs.')
    guard = asyncio.Semaphore(4)
    async def check(row):
        async with guard:
            result = await asyncio.to_thread(inspect_asset, row['asset_id'], connection['api_key'])
            if result['state'] in ('approved', 'reviewing', 'rejected', 'unknown'):
                db.update_job(row['id'], moderation_state=result['state'],
                              moderation_message=result['message'],
                              moderation_checked_at=datetime.now(timezone.utc).isoformat())
            return {'job_id': row['id'], 'asset_id': row['asset_id'], **result}
    return {'results': await asyncio.gather(*(check(row) for row in rows))}


@app.get('/api/collections')
def collections(request: Request):
    user = authorize_request(request)
    with db.connect() as conn:
        rows = conn.execute('''SELECT c.id, c.name, c.created_at, COUNT(ci.job_id) AS total
                               FROM collections c LEFT JOIN collection_items ci ON ci.collection_id=c.id
                               WHERE c.discord_user_id=?
                               GROUP BY c.id ORDER BY c.created_at DESC, c.rowid DESC''',
                            (user['discord_id'],)).fetchall()
        relations = conn.execute('''SELECT ci.collection_id, ci.job_id FROM collection_items ci
                                    JOIN collections c ON c.id=ci.collection_id
                                    WHERE c.discord_user_id=?''', (user['discord_id'],)).fetchall()
    return {'collections': [dict(row) for row in rows],
            'items': [dict(row) for row in relations]}


@app.post('/api/collections', status_code=201)
async def create_collection(request: Request):
    user = authorize_request(request, mutate=True)
    data = await request.json()
    name = data.get('name', '') if isinstance(data, dict) else ''
    if not isinstance(name, str) or not 2 <= len(name.strip()) <= 50:
        raise HTTPException(400, 'Nome da coleção precisa ter entre 2 e 50 caracteres.')
    name = name.strip()
    collection_id = uuid.uuid4().hex
    with db.connect() as conn:
        count = conn.execute('SELECT COUNT(*) FROM collections WHERE discord_user_id=?',
                             (user['discord_id'],)).fetchone()[0]
        if count >= 40:
            raise HTTPException(429, 'Limite de 40 coleções por conta.')
        conn.execute('INSERT INTO collections (id,discord_user_id,name) VALUES (?,?,?)',
                     (collection_id, user['discord_id'], name))
    return {'id': collection_id, 'name': name}


@app.delete('/api/collections/{collection_id}')
def delete_collection(collection_id: str, request: Request):
    user = authorize_request(request, mutate=True)
    if not ID_RE.fullmatch(collection_id):
        raise HTTPException(400, 'Coleção inválida.')
    with db.connect() as conn:
        row = conn.execute('SELECT id FROM collections WHERE id=? AND discord_user_id=?',
                           (collection_id, user['discord_id'])).fetchone()
        if row is None:
            raise HTTPException(404, 'Coleção não encontrada.')
        conn.execute('DELETE FROM collection_items WHERE collection_id=?', (collection_id,))
        conn.execute('DELETE FROM collections WHERE id=? AND discord_user_id=?', (collection_id, user['discord_id']))
    return {'ok': True}


@app.put('/api/collections/{collection_id}/items/{job_id}')
def add_collection_item(collection_id: str, job_id: str, request: Request):
    user = authorize_request(request, mutate=True)
    if not ID_RE.fullmatch(collection_id) or not ID_RE.fullmatch(job_id):
        raise HTTPException(400, 'IDs inválidos.')
    with db.connect() as conn:
        row = conn.execute('SELECT id FROM collections WHERE id=? AND discord_user_id=?',
                           (collection_id, user['discord_id'])).fetchone()
        job = conn.execute('SELECT id FROM jobs WHERE id=? AND discord_user_id=?',
                           (job_id, user['discord_id'])).fetchone()
        if not row or not job:
            raise HTTPException(404, 'Coleção ou música não encontrada.')
        conn.execute('INSERT OR IGNORE INTO collection_items (collection_id,job_id) VALUES (?,?)',
                     (collection_id, job_id))
    return {'ok': True}


@app.delete('/api/collections/{collection_id}/items/{job_id}')
def delete_collection_item(collection_id: str, job_id: str, request: Request):
    user = authorize_request(request, mutate=True)
    if not ID_RE.fullmatch(collection_id) or not ID_RE.fullmatch(job_id):
        raise HTTPException(400, 'IDs inválidos.')
    with db.connect() as conn:
        row = conn.execute('SELECT id FROM collections WHERE id=? AND discord_user_id=?',
                           (collection_id, user['discord_id'])).fetchone()
        if not row:
            raise HTTPException(404, 'Coleção não encontrada.')
        conn.execute('DELETE FROM collection_items WHERE collection_id=? AND job_id=?',
                     (collection_id, job_id))
    return {'ok': True}


@app.get('/healthz')
def health():
    return {'ok': True}
