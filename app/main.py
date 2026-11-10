"""Naples Studios - OAuth Discord + produção de áudio + Roblox Open Cloud."""
import asyncio
import hashlib
import ipaddress
import os
import re
import secrets
import shutil
import time
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlencode, urlparse

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import config, database as db, settings_store as store
from .core import (
    MAX_FILE_BYTES, ProcessingError, convert_2_5x, create_roblox_asset,
    download_platform, platform_from_url, poll_roblox_asset, valid_creator_id, valid_title
)

EXPORTS = config.DATA / 'exports'
TEMP = config.DATA / 'tmp'
TASKS: set[asyncio.Task] = set()
QUEUE = asyncio.Semaphore(2)
RATES = defaultdict(deque)
ID_RE = re.compile(r'^[0-9a-f]{32}$')


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
    if not row or row['discord_id'] not in config.DISCORD_ALLOWED_USER_IDS:
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
    return d


def process_job(job_id: str, local: Path | None, url: str, title: str, creator_id: str, creator_type: str):
    work = TEMP / job_id
    work.mkdir(parents=True, exist_ok=True)
    try:
        if local:
            source = local
        else:
            db.update_job(job_id, status='processando', stage='Importando link autorizado com yt-dlp')
            source = download_platform(url, work)
        db.update_job(job_id, status='processando', stage='FFmpeg: aceleração 2.5x e conversão MP3')
        metadata = convert_2_5x(source, EXPORTS / f'{job_id}.mp3')
        db.update_job(job_id, **metadata)
        db.update_job(job_id, status='enviando', stage='Criando asset no Roblox Open Cloud')
        operation = create_roblox_asset(EXPORTS / f'{job_id}.mp3', title, creator_id, creator_type, config.ROBLOX_API_KEY)
        db.update_job(job_id, status='confirmando', operation=operation, stage='Aguardando Roblox emitir Asset ID')
        asset_id = poll_roblox_asset(operation, config.ROBLOX_API_KEY)
        if asset_id:
            db.update_job(job_id, status='concluido', asset_id=asset_id, error=None, stage='ID criado; moderação e permissões ainda podem estar pendentes')
        else:
            db.update_job(job_id, status='pendente', stage='Roblox ainda processando. Use Verificar operação.')
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
    store.initialize()
    config.reload_settings()
    if not store.has_admin():
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
        "connect-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
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
    return {'settings': store.public_values(), 'ready': config.ready(),
            'callback_url': config.APP_BASE_URL + '/auth/discord/callback'}


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
    for k, val in data.items():
        if k in ('discord_client_secret', 'roblox_api_key'):
            if val is not None and (not isinstance(val, str) or len(val) > 5000):
                raise HTTPException(400, 'Credencial inválida.')
            if val and val.strip():
                values[k] = val.strip()
        elif k in ('cookie_secure', 'enable_platform_links'):
            if not isinstance(val, bool):
                raise HTTPException(400, f'{k} deve ser booleano.')
            values[k] = val
        else:
            if not isinstance(val, str) or len(val) > 2048:
                raise HTTPException(400, f'{k} inválido.')
            values[k] = val.strip()
    if not values.get('discord_client_secret') or len(values.get('discord_client_secret')) < 8:
        raise HTTPException(400, 'Preencha o Client Secret do Discord (mínimo 8 caracteres).')
    if not values.get('roblox_api_key') or len(values.get('roblox_api_key')) < 16:
        raise HTTPException(400, 'Preencha uma API Key Roblox válida (mínimo 16 caracteres).')
    try:
        values['app_base_url'] = config.validate_settings(values)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    # Segredos jamais aparecem nas respostas JSON/logs de aplicação.
    store.set_values({k: v for k, v in values.items() if k in permitted})
    config.reload_settings()
    return {'ok': True, 'ready': config.ready(), 'callback_url': config.APP_BASE_URL + '/auth/discord/callback'}


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


@app.post('/api/admin/logout')
def admin_logout(request: Request):
    token = current_admin(request, mutate=True)
    store.expire_admin_session(token)
    resp = JSONResponse({'ok': True})
    resp.delete_cookie(config.ADMIN_COOKIE, path='/')
    return resp


@app.get('/auth/discord')
def discord_login(request: Request):
    if not config.ready():
        return RedirectResponse('/configuracoes', status_code=303)
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
    if not discord_id.isdigit() or discord_id not in config.DISCORD_ALLOWED_USER_IDS:
        return RedirectResponse('/?login_error=acesso', status_code=303)

    username = str(user.get('global_name') or user.get('username') or 'Usuário')[:100]
    avatar = profile_avatar(discord_id, user.get('avatar'))
    session_token = secrets.token_urlsafe(48)
    now = int(time.time())
    with db.connect() as conn:
        db.prune(conn)
        conn.execute('''INSERT INTO sessions (token_hash,discord_id,username,avatar_url,csrf_token_hash,created_at,expires_at)
                      VALUES (?,?,?,?,?,?,?)''',
                     (hashed(session_token), discord_id, username, avatar, hashed(derive_csrf(session_token)), now, now + config.SESSION_HOURS * 3600))
        # O CSRF é gerado por sessão e devolvido via endpoint autenticado (não em URL).
    resp = RedirectResponse('/#dashboard', status_code=303)
    resp.set_cookie(config.SESSION_COOKIE, session_token, max_age=config.SESSION_HOURS * 3600, httponly=True,
                    secure=config.COOKIE_SECURE, samesite='lax', path='/')
    resp.delete_cookie(config.OAUTH_COOKIE, path='/auth/discord')
    return resp


@app.get('/api/session')
def session(request: Request):
    user = require_session(request)
    # CSRF: valor derivado? Guardamos apenas hash; geramos um segundo token de sessão de acesso
    # usando HMAC estável a partir de token de cookie e um segredo de aplicação no servidor.
    return {
        'authenticated': True,
        'user': {'id': user['discord_id'], 'username': user['username'], 'avatar_url': user['avatar_url']},
        'csrf': derive_csrf(request.cookies[config.SESSION_COOKIE]),
        'config': {
            'platform_links': config.ENABLE_PLATFORM_LINKS, 'speed': 2.5,
            'creator_id': config.ROBLOX_CREATOR_ID, 'creator_type': config.ROBLOX_CREATOR_TYPE,
            'version': '2.0', 'allowed_creator_ids': sorted(config.ROBLOX_ALLOWED_CREATOR_IDS)
        }
    }


def derive_csrf(session_token: str):
    # Não serializar o hash do banco; derivação HMAC determinística baseada em segredo do servidor.
    import hmac
    secret = config.SESSION_SECRET.encode('utf8')
    return hmac.new(secret, b'naples-csrf-v1:' + session_token.encode('utf8'), 'sha256').hexdigest()


def authorize_request(request: Request, mutate=False):
    return require_session(request, mutate=mutate)


@app.post('/api/logout')
def logout(request: Request):
    authorize_request(request, mutate=True)
    with db.connect() as conn:
        conn.execute('DELETE FROM sessions WHERE token_hash=?', (hashed(request.cookies[config.SESSION_COOKIE]),))
    resp = JSONResponse({'ok': True})
    resp.delete_cookie(config.SESSION_COOKIE, path='/')
    return resp


@app.get('/api/jobs')
def jobs(request: Request):
    user = authorize_request(request)
    return {'jobs': [job_json(row) for row in db.list_jobs(user['discord_id'])]}


@app.post('/api/jobs', status_code=202)
async def submit_job(request: Request, title: str = Form(...), creator_id: str = Form(''),
                     creator_type: str = Form(''), source_url: str = Form(''),
                     rights_confirmed: str = Form('false'), audio_file: UploadFile | None = File(None)):
    user = authorize_request(request, mutate=True)
    rate('publish:' + user['discord_id'], 15, 3600)
    try:
        title = valid_title(title)
        creator_id = valid_creator_id(creator_id or config.ROBLOX_CREATOR_ID)
        creator_type = creator_type or config.ROBLOX_CREATOR_TYPE
        if creator_type not in ('user', 'group'):
            raise ProcessingError('Escolha usuário ou grupo Roblox.')
        if config.ROBLOX_ALLOWED_CREATOR_IDS and creator_id not in config.ROBLOX_ALLOWED_CREATOR_IDS:
            raise ProcessingError('Este destino Roblox não está autorizado pelo administrador.')
        if rights_confirmed != 'true':
            raise ProcessingError('Confirme que possui os direitos necessários para processar e publicar o áudio.')
        url = source_url.strip()
        has_file = bool(audio_file and audio_file.filename)
        if bool(url) == has_file:
            raise ProcessingError('Envie um arquivo OU informe um link, não ambos.')
        if url:
            platform = platform_from_url(url)
            if not config.ENABLE_PLATFORM_LINKS:
                raise ProcessingError('Importação de links está desabilitada no servidor.')
        else:
            platform = 'Arquivo local'
            suffix = Path(audio_file.filename).suffix.lower()
            if suffix not in ('.mp3', '.wav', '.flac', '.ogg', '.m4a', '.webm', '.opus', '.aac'):
                raise ProcessingError('Extensão de arquivo não permitida.')
    except ProcessingError as exc:
        raise HTTPException(400, str(exc)) from exc

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
    db.create_job(job_id, user['discord_id'], title, platform, url if url else (audio_file.filename or 'arquivo'), creator_id, creator_type)
    task = asyncio.create_task(run_job(job_id, local, url, title, creator_id, creator_type))
    TASKS.add(task)
    task.add_done_callback(TASKS.discard)
    return {'job_id': job_id, 'status': 'fila'}


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
    try:
        asset_id = await asyncio.to_thread(poll_roblox_asset, row['operation'], config.ROBLOX_API_KEY, None, 5, 2)
    except ProcessingError as exc:
        raise HTTPException(502, str(exc)) from exc
    if asset_id:
        db.update_job(job_id, status='concluido', stage='ID criado; moderação pode estar pendente', asset_id=asset_id, error=None)
    else:
        db.update_job(job_id, status='pendente', stage='Roblox ainda processando', error=None)
    return {'asset_id': asset_id, 'pending': not bool(asset_id)}


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
        conn.execute('DELETE FROM jobs WHERE id=? AND discord_user_id=?', (job_id, user['discord_id']))
    (EXPORTS / f'{job_id}.mp3').unlink(missing_ok=True)
    return {'ok': True}


@app.get('/healthz')
def health():
    return {'ok': True}
