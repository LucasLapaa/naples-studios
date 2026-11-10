"""Processamento de áudio, integração com Roblox e validações, sem acoplamento à interface."""
import json
import os
import re
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx

CREATE_URL = 'https://apis.roblox.com/assets/v1/assets'
OPERATIONS_URL = 'https://apis.roblox.com/assets/v1/operations/'
MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_OUTPUT_BYTES = 20 * 1024 * 1024
MAX_DURATION = 7 * 60

class ProcessingError(Exception):
    pass

def valid_creator_id(raw):
    val = str(raw or '').strip()
    if not re.fullmatch(r'[1-9][0-9]{0,19}', val):
        raise ProcessingError('Informe um ID numérico válido de usuário ou grupo Roblox.')
    return val

def valid_title(raw):
    val = str(raw or '').strip()
    if not 3 <= len(val) <= 80:
        raise ProcessingError('Título deve conter entre 3 e 80 caracteres.')
    return val

def platform_from_url(value):
    try:
        parsed = urlparse(value)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.port:
            raise ValueError()
        host = parsed.hostname.lower().rstrip('.')
        allowed = {
            'youtube.com', 'www.youtube.com', 'm.youtube.com', 'music.youtube.com',
            'youtu.be', 'www.youtu.be', 'soundcloud.com', 'www.soundcloud.com',
            'on.soundcloud.com',
        }
        if host not in allowed or len(value) > 2000:
            raise ValueError()
        return 'YouTube' if 'youtu' in host else 'SoundCloud'
    except (ValueError, TypeError):
        raise ProcessingError('Use somente um link HTTPS válido do YouTube ou SoundCloud.')

def run(cmd, timeout):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError:
        raise ProcessingError(f'Ferramenta {cmd[0]} não instalada no servidor.')
    except subprocess.TimeoutExpired:
        raise ProcessingError('O processamento excedeu o tempo permitido.')
    if r.returncode:
        detail = (r.stderr or r.stdout or '').strip()[-450:]
        raise ProcessingError(f'Não foi possível processar este áudio. {detail}')
    return r.stdout.strip()

def download_platform(url: str, workdir: Path) -> Path:
    platform_from_url(url)
    output_pattern = str(workdir / 'fonte.%(ext)s')
    # Sem cookies, sem bypass de DRM/autenticação, sem playlists.
    run([
        'yt-dlp', '--ignore-config', '--no-playlist', '--no-progress',
        '--js-runtimes', 'node',
        '--no-write-info-json', '--no-write-thumbnail', '--no-part',
        '--restrict-filenames', '--max-filesize', '50M', '--socket-timeout', '15',
        '--retries', '1', '--fragment-retries', '1', '--format', 'bestaudio/best',
        '--output', output_pattern, '--', url
    ], 180)
    files = [p for p in workdir.glob('fonte.*') if p.is_file()]
    if len(files) != 1:
        raise ProcessingError('O serviço não entregou um arquivo de áudio utilizável.')
    source = files[0]
    if source.stat().st_size > MAX_FILE_BYTES:
        raise ProcessingError('Arquivo de origem maior que 50 MB.')
    return source

def duration_seconds(path: Path) -> float:
    out = run([
        'ffprobe', '-v', 'error', '-show_entries', 'format=duration',
        '-of', 'default=noprint_wrappers=1:nokey=1', str(path)
    ], 20)
    try:
        duration = float(out)
    except ValueError:
        raise ProcessingError('Não foi possível identificar a duração do áudio.')
    if not 0 < duration < 100000:
        raise ProcessingError('Duração inválida.')
    return duration

def convert_2_5x(source: Path, output: Path) -> dict:
    if source.stat().st_size > MAX_FILE_BYTES:
        raise ProcessingError('Arquivo de origem maior que 50 MB.')
    original_duration = duration_seconds(source)
    if original_duration / 2.5 > MAX_DURATION:
        raise ProcessingError('Após aceleração, o áudio ultrapassaria 7 minutos do Roblox.')
    # Acelera tempo e tom 2,5x: quando Roblox usar PlaybackSpeed=0.4, volta ao original.
    # ATENÇÃO: atempo sozinho preservaria o tom e NÃO compensaria a queda de pitch no Roblox.
    run([
        'ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-y',
        '-i', str(source), '-map', '0:a:0', '-vn', '-sn', '-dn',
        '-af', 'aresample=44100,asetrate=110250,aresample=44100', '-ar', '44100', '-ac', '2',
        '-c:a', 'libmp3lame', '-b:a', '160k', '-map_metadata', '-1', str(output)
    ], 160)
    if not output.exists() or not output.stat().st_size:
        raise ProcessingError('Falha ao gerar o MP3.')
    if output.stat().st_size >= MAX_OUTPUT_BYTES:
        raise ProcessingError('Áudio final tem 20 MB ou mais; reduza a duração.')
    final_duration = duration_seconds(output)
    if final_duration > MAX_DURATION:
        raise ProcessingError('Duração final excede os 7 minutos permitidos pelo Roblox.')
    return {'original_duration': round(original_duration, 1), 'final_duration': round(final_duration, 1), 'size_bytes': output.stat().st_size}

def ensure_http_ok(response):
    if response.status_code >= 400:
        try:
            obj = response.json()
            message = obj.get('message') or obj.get('error') or obj.get('errors') or obj.get('detail') or 'Erro desconhecido'
            if isinstance(message, (list, dict)):
                message = json.dumps(message, ensure_ascii=False)[:300]
        except Exception:
            message = f'HTTP {response.status_code}'
        hint = {401: 'Chave inválida ou sem autorização.', 403: 'Sem permissão de asset:read/asset:write ou conta incompatível.', 429: 'Limite de uploads excedido; tente mais tarde.'}.get(response.status_code, '')
        raise ProcessingError(f'Roblox HTTP {response.status_code}: {str(message)[:300]}. {hint}')

def create_roblox_asset(mp3: Path, title: str, creator_id: str, creator_type: str, api_key: str, client=None):
    if creator_type not in ('user', 'group'):
        raise ProcessingError('Destino Roblox inválido.')
    key = 'userId' if creator_type == 'user' else 'groupId'
    payload = {
        'assetType': 'Audio',
        'displayName': title,
        'description': 'Áudio enviado pelo RBX WAVE. Uso autorizado pelo titular.',
        'creationContext': {'creator': {key: creator_id}}
    }
    owns_client = client is None
    if owns_client:
        client = httpx.Client(timeout=httpx.Timeout(150.0, connect=20.0), follow_redirects=False)
    try:
        with mp3.open('rb') as stream:
            # A API aceita string JSON no campo multipart "request" e arquivo em "fileContent".
            resp = client.post(
                CREATE_URL,
                headers={'x-api-key': api_key},
                data={'request': json.dumps(payload, ensure_ascii=False)},
                files={'fileContent': (mp3.name, stream, 'audio/mpeg')}
            )
        ensure_http_ok(resp)
        obj = resp.json()
        operation = obj.get('path') or obj.get('name') or ''
        if not isinstance(operation, str) or not re.fullmatch(r'operations/[A-Za-z0-9_\-]+', operation):
            raise ProcessingError('O Roblox não retornou um ID de operação válido.')
        return operation
    except httpx.RequestError as e:
        raise ProcessingError(f'Falha de comunicação com Roblox: {type(e).__name__}.')
    finally:
        if owns_client:
            client.close()

def poll_roblox_asset(operation: str, api_key: str, client=None, tries=30, interval=3):
    if not re.fullmatch(r'operations/[A-Za-z0-9_\-]+', operation):
        raise ProcessingError('ID da operação inválido.')
    owns_client = client is None
    if owns_client:
        client = httpx.Client(timeout=httpx.Timeout(30.0, connect=15.0), follow_redirects=False)
    try:
        for attempt in range(tries):
            try:
                response = client.get(OPERATIONS_URL + operation.split('/', 1)[1], headers={'x-api-key': api_key})
            except httpx.RequestError:
                if attempt == tries-1:
                    raise ProcessingError('Não foi possível confirmar o upload no Roblox.')
                time.sleep(interval)
                continue
            ensure_http_ok(response)
            obj = response.json()
            if obj.get('done'):
                if obj.get('error'):
                    error = obj['error']
                    raise ProcessingError(f'Roblox rejeitou o áudio: {str(error.get("message", error))[:350]}')
                result = obj.get('response') or {}
                asset_id = str(result.get('assetId') or '')
                if not asset_id and isinstance(result.get('path'), str):
                    match = re.fullmatch(r'assets/(\d+)', result['path'])
                    if match: asset_id = match.group(1)
                if asset_id.isdigit():
                    return asset_id
                raise ProcessingError('Operação finalizada, mas a API não informou um Asset ID.')
            if attempt < tries-1:
                time.sleep(interval)
        return None  # pendente — verificar manualmente depois
    finally:
        if owns_client:
            client.close()
