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

class PlatformAccessError(ProcessingError):
    """Fonte recusou importação; não confundir com falha de FFmpeg ou Roblox."""


def explain_platform_failure(platform: str, message: str) -> str:
    """Converte stderr do extrator em texto seguro, claro e sem credenciais/URLs.

    Não tenta contornar CAPTCHA, login, DRM ou outras restrições da plataforma.
    """
    msg = (message or '').lower()
    if any(x in msg for x in (
        'sign in to confirm you', 'not a bot', 'confirm you’re not a bot',
        "confirm you're not a bot", 'captcha', 'unusual traffic',
    )):
        return ('O YouTube solicitou verificação de acesso automatizado e bloqueou '
                'a importação pelo servidor. Atualizar o extrator pode não resolver. '
                'Use o upload de um arquivo próprio ou obtido com autorização.')
    if any(x in msg for x in ('private video', 'private track', 'this video is private',
                             'this track is private', 'login required', 'requires login',
                             'sign in to view', 'members-only', 'age-restricted')):
        return (f'O conteúdo no {platform} exige login ou permissão adicional. '
                'O Naples Studios não acessa conteúdo privado ou restrito. '
                'Envie um arquivo que você tenha autorização para usar.')
    if any(x in msg for x in ('http error 429', 'too many requests', 'rate limit',
                             'rate-limit', 'request limit exceeded')):
        return (f'O {platform} limitou temporariamente as solicitações desta conexão. '
                'Não tente repetidamente agora; use um arquivo local autorizado.')
    if any(x in msg for x in ('geo-restricted', 'not available in your country',
                             'not available in your region', 'geoblocked')):
        return (f'O {platform} restringe este conteúdo para a região do servidor. '
                'Use outro conteúdo permitido ou um arquivo local autorizado.')
    if any(x in msg for x in ('http error 401', 'http error 403', 'forbidden', 'unauthorized')):
        return (f'O {platform} negou acesso à mídia (HTTP 401/403). '
                'Confirme se o conteúdo está público e disponível para reutilização.')
    if any(x in msg for x in ('http error 404', 'video unavailable', 'track unavailable',
                             'not found', 'has been removed', 'does not exist')):
        return (f'O link no {platform} não está disponível. Confira se a URL existe '
                'e se o autor permite acessar o conteúdo.')
    if any(x in msg for x in ('unsupported url', 'no video formats found',
                             'requested format is not available', 'no formats found')):
        return (f'O {platform} não ofereceu um formato de áudio compatível neste link. '
                'Atualize o yt-dlp ou envie um arquivo local autorizado.')
    if any(x in msg for x in ('timed out', 'connection reset', 'temporary failure',
                             'network is unreachable', 'http error 500', 'http error 502',
                             'http error 503', 'http error 504')):
        return (f'Falha temporária de conexão ao {platform}. '
                'Tente novamente mais tarde ou use arquivo local.')
    return (f'Não foi possível importar o link do {platform}. '
            'Verifique se é público, acessível e autorizado para uso; '
            'caso contrário utilize o envio de arquivo local. Consulte logs do Railway '
            'para diagnóstico técnico (não compartilhe credenciais).')

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
    platform = platform_from_url(url)
    workdir.mkdir(parents=True, exist_ok=True)
    output_pattern = str(workdir / 'fonte.%(ext)s')
    # Apenas URLs permitidas, sem cookies, sem autenticação automática, sem playlists.
    # EJS é instalado localmente via yt-dlp[default]; não baixa scripts arbitrários.
    command = [
        'yt-dlp', '--ignore-config', '--no-playlist', '--max-downloads', '1',
        '--no-progress', '--js-runtimes', 'node', '--no-write-info-json',
        '--no-write-thumbnail', '--no-part', '--restrict-filenames',
        '--abort-on-unavailable-fragments', '--max-filesize', '50M',
        '--socket-timeout', '15', '--retries', '2', '--fragment-retries', '2',
        '--retry-sleep', 'http:exp=1:8', '--retry-sleep', 'fragment:exp=1:8',
        '--format', 'bestaudio/best', '--output', output_pattern,
        '--', url,
    ]
    try:
        run(command, 180)
    except ProcessingError as exc:
        # Nunca publica os logs brutos do yt-dlp, URLs privadas ou dados de sessão.
        if str(exc).startswith('Ferramenta yt-dlp não instalada'):
            raise PlatformAccessError('O yt-dlp não está instalado no servidor. Refaça o deploy do Dockerfile.') from exc
        raise PlatformAccessError(explain_platform_failure(platform, str(exc))) from exc
    files = [p for p in workdir.glob('fonte.*') if p.is_file()]
    if len(files) != 1:
        raise PlatformAccessError('A plataforma não entregou um único arquivo de áudio utilizável. Tente upload local.')
    source = files[0]
    if source.stat().st_size > MAX_FILE_BYTES:
        raise PlatformAccessError('O arquivo da plataforma supera 50 MB. Envie um arquivo menor.')
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
    """Acelera áudio em 2.5x COM pitch, aplica EQ e limpa metadados.

    MP3: saída padrão utilizada pelo painel Naples Studios.
    OGG: suportado para processamento manual ou futuras versões do painel.
    Nenhuma opção desta função contorna moderação ou copyright do Roblox.
    """
    source = Path(source)
    output = Path(output)
    if not source.is_file():
        raise ProcessingError('Arquivo de origem não encontrado.')
    if source.resolve() == output.resolve():
        raise ProcessingError('A origem e a saída precisam ser arquivos diferentes.')
    if source.stat().st_size > MAX_FILE_BYTES:
        raise ProcessingError('Arquivo de origem maior que 50 MB.')

    fmt = output.suffix.lower()
    if fmt == '.mp3':
        # Remoção explícita de tags ID3v1 e ID3v2.
        encoder = ['-c:a', 'libmp3lame', '-b:a', '160k',
                   '-id3v2_version', '0', '-write_id3v1', '0']
    elif fmt == '.ogg':
        encoder = ['-c:a', 'libvorbis', '-qscale:a', '5']
    else:
        raise ProcessingError('Formato final deve ser .mp3 ou .ogg.')

    original_duration = duration_seconds(source)
    if original_duration / 2.5 >= MAX_DURATION:
        raise ProcessingError('Após aceleração, o áudio precisa ter menos de 7 minutos.')

    # 1. Padroniza a ENTRADA em 44100 Hz, independentemente da taxa original.
    # 2. asetrate reinterpreta as amostras como 110250 Hz (= 44100 x 2.5),
    #    acelerando tempo e pitch juntos — sem o filtro atempo.
    # 3. Reamostra para 44100 Hz para gerar um arquivo normalizado.
    # 4. Aplica EQ sobre o ÁUDIO FINAL (Hz após a mudança de pitch).
    audio_filters = (
        'aresample=44100,'
        'asetrate=110250,'
        'aresample=44100,'
        'highpass=f=100,'
        'lowpass=f=15000'
    )

    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        run([
            'ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-y',
            '-i', str(source),
            '-map', '0:a:0', '-vn', '-sn', '-dn',
            '-af', audio_filters,
            '-ar', '44100', '-ac', '2',
            *encoder,
            '-map_metadata', '-1', '-map_chapters', '-1',
            str(output),
        ], 160)

        if not output.is_file() or output.stat().st_size <= 0:
            raise ProcessingError('Falha ao gerar o arquivo de áudio.')
        if output.stat().st_size >= MAX_OUTPUT_BYTES:
            raise ProcessingError('Áudio final precisa ter menos de 20 MB.')
        final_duration = duration_seconds(output)
        if final_duration >= MAX_DURATION:
            raise ProcessingError('Áudio final precisa ter menos de 7 minutos.')

        return {
            'original_duration': round(original_duration, 1),
            'final_duration': round(final_duration, 1),
            'size_bytes': output.stat().st_size,
        }
    except Exception:
        # Evita disponibilizar ou deixar no histórico um arquivo incompleto.
        output.unlink(missing_ok=True)
        raise

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
    content_type = {'.mp3': 'audio/mpeg', '.ogg': 'audio/ogg'}.get(mp3.suffix.lower())
    if not content_type:
        raise ProcessingError('Envio Roblox aceita apenas MP3 ou OGG nesta integração.')
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
                files={'fileContent': (mp3.name, stream, content_type)}
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
