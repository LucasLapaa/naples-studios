"""Processamento de áudio, integração com Roblox e validações, sem acoplamento à interface."""
import json
import os
import re
import subprocess
import time
import logging
import shutil
from pathlib import Path
from urllib.parse import urlparse

import httpx

CREATE_URL = 'https://apis.roblox.com/assets/v1/assets'
OPERATIONS_URL = 'https://apis.roblox.com/assets/v1/operations/'
MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_OUTPUT_BYTES = 20 * 1024 * 1024
MAX_DURATION = 7 * 60
log = logging.getLogger("naples.media")

class ProcessingError(Exception):
    pass

class PlatformAccessError(ProcessingError):
    """Falha ao obter mídia do provedor, separada de processamento e publicação."""
    def __init__(self, message: str, code: str = 'SOURCE_UNAVAILABLE'):
        super().__init__(message)
        self.code = code


def source_error_code(platform: str, message: str) -> str:
    msg = (message or '').casefold()
    if any(x in msg for x in ('not a bot', 'confirm you', 'captcha', 'unusual traffic')):
        return 'YOUTUBE_VERIFICATION' if platform == 'YouTube' else 'PROVIDER_VERIFICATION'
    if any(x in msg for x in ('unable to extract client id', 'could not extract client id', 'client_id')):
        return 'SOUNDCLOUD_CLIENT_ID'
    if any(x in msg for x in ('aws waf', 'cloudflare challenge', 'challenge required', 'datadome')):
        return 'PROVIDER_VERIFICATION'
    if any(x in msg for x in ('login required', 'requires login', 'private video', 'private track', 'members-only', 'age-restricted')):
        return 'SOURCE_RESTRICTED'
    if any(x in msg for x in ('http error 429', 'too many requests', 'rate limit')):
        return 'SOURCE_RATE_LIMIT'
    if any(x in msg for x in ('http error 401', 'http error 403', 'forbidden', 'unauthorized')):
        return 'SOURCE_FORBIDDEN'
    if any(x in msg for x in ('http error 404', 'not found', 'video unavailable', 'track unavailable')):
        return 'SOURCE_NOT_FOUND'
    if any(x in msg for x in ('unsupported url', 'requested format is not available', 'no video formats found')):
        return 'SOURCE_FORMAT_UNAVAILABLE'
    if any(x in msg for x in ('timed out', 'connection reset', 'http error 500', 'http error 502', 'http error 503', 'http error 504')):
        return 'SOURCE_NETWORK'
    return 'SOURCE_UNAVAILABLE'


def source_failure_message(platform: str, code: str) -> str:
    messages = {
        'YOUTUBE_VERIFICATION': 'O YouTube exigiu uma verificação de acesso. O servidor não pode ignorar essa exigência. Para conteúdo seu, obtenha o arquivo por um meio autorizado e use Arquivo local.',
        'SOUNDCLOUD_CLIENT_ID': 'O extrator não conseguiu obter a identificação de acesso do SoundCloud. Isso pode acontecer quando o serviço limita acessos de servidores. Se a faixa for sua, use o arquivo original; para download habilitado, utilize a opção oficial Baixar arquivo no SoundCloud.',
        'PROVIDER_VERIFICATION': f'O {platform} exigiu verificação de acesso que este servidor não consegue concluir. Use um arquivo disponibilizado pelo titular para download.',
        'SOURCE_RESTRICTED': f'O conteúdo do {platform} exige login, autorização ou é privado. O Naples Studios não acessa conteúdos restritos.',
        'SOURCE_RATE_LIMIT': f'O {platform} limitou temporariamente as solicitações. Evite novas tentativas em sequência; use arquivo autorizado.',
        'SOURCE_FORBIDDEN': f'O {platform} negou acesso ao conteúdo (401/403). Verifique as permissões de download da faixa.',
        'SOURCE_NOT_FOUND': f'O endereço do {platform} não está disponível ou foi removido. Confira se é uma faixa individual válida.',
        'SOURCE_FORMAT_UNAVAILABLE': f'O {platform} não disponibilizou formato de áudio utilizável para esse link.',
        'SOURCE_NETWORK': f'Falha temporária de rede ao conectar ao {platform}.',
        'SOURCE_UNAVAILABLE': f'O {platform} não forneceu o áudio para download. Verifique nos logs protegidos o diagnóstico técnico e use upload direto quando houver bloqueio.',
    }
    return messages.get(code, messages['SOURCE_UNAVAILABLE'])



def explain_platform_failure(platform: str, message: str) -> str:
    return source_failure_message(platform, source_error_code(platform, message))


def safe_debug(message: str) -> str:
    """Retira URLs, parâmetros e cadeias potencialmente sensíveis dos logs."""
    message = re.sub(r'https?://\S+', '[URL]', message or '')
    message = re.sub(r'(?i)(authorization|cookie|token|api[_-]?key|password)\s*[:=]\s*\S+', r'\1=[REDACTED]', message)
    return message[-900:].replace('\n', ' | ')


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
        detail = (r.stderr or r.stdout or '').strip()[-1600:]
        raise ProcessingError(f'Não foi possível processar este áudio. {detail}')
    return r.stdout.strip()

def download_platform(url: str, workdir: Path) -> Path:
    platform = platform_from_url(url)
    workdir.mkdir(parents=True, exist_ok=True)
    output_pattern = str(workdir / 'fonte.%(ext)s')
    # Apenas URLs permitidas, sem cookies, sem autenticação automática, sem playlists.
    # Não usar --max-downloads 1: yt-dlp pode retornar exit status 101 mesmo
    # tendo concluído o primeiro arquivo, que o wrapper interpretaria como erro.
    # --no-playlist restringe a um só item; verificamos o único arquivo ao final.
    # EJS é instalado localmente via yt-dlp[default]; não baixa scripts arbitrários.
    command = [
        'yt-dlp', '--ignore-config', '--no-playlist',
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
        code = source_error_code(platform, str(exc))
        log.warning('Falha de importação | plataforma=%s | codigo=%s | diagnostico=%s',
                    platform, code, safe_debug(str(exc)))
        raise PlatformAccessError(f'[{code}] {source_failure_message(platform, code)}', code=code) from exc
    files = [p for p in workdir.glob('fonte.*') if p.is_file()]
    if len(files) != 1:
        raise PlatformAccessError('A plataforma não entregou um único arquivo de áudio utilizável. Tente upload local.')
    source = files[0]
    if source.stat().st_size > MAX_FILE_BYTES:
        raise PlatformAccessError('O arquivo da plataforma supera 50 MB. Envie um arquivo menor.')
    return source

def preview_platform(url: str) -> dict:
    """Busca apenas metadados de uma faixa pública. Não baixa áudio.

    Não usa cookies nem contorna exigência de login/verificação da origem.
    """
    platform = platform_from_url(url)
    command = [
        'yt-dlp', '--ignore-config', '--no-playlist',
        '--skip-download', '--dump-single-json', '--no-warnings',
        '--js-runtimes', 'node', '--socket-timeout', '12',
        '--retries', '1', '--no-write-info-json', '--', url,
    ]
    try:
        raw = run(command, 45)
        if len(raw) > 2_000_000:
            raise ProcessingError('Metadados maiores do que o esperado.')
        payload = json.loads(raw)
        if not isinstance(payload, dict) or payload.get('_type') in ('playlist', 'multi_video'):
            raise ProcessingError('Envie um link de uma única música, não uma playlist.')
        title = str(payload.get('title') or '').strip()[:120]
        if not title:
            title = 'Música sem título informado'
        duration = payload.get('duration')
        duration = round(float(duration), 1) if isinstance(duration, (int, float)) and 0 < duration < 100000 else None
        return {
            'platform': platform, 'title': title,
            'duration': duration,
            'uploader': str(payload.get('uploader') or '')[:100],
            'note': 'Prévia informativa. O download depende da autorização e da disponibilidade na plataforma.'
        }
    except (ProcessingError, ValueError, TypeError, json.JSONDecodeError) as exc:
        if isinstance(exc, ProcessingError) and str(exc).startswith('Ferramenta yt-dlp não instalada'):
            raise PlatformAccessError('O yt-dlp não está instalado no servidor.') from exc
        code = source_error_code(platform, str(exc))
        log.warning('Falha de prévia | plataforma=%s | codigo=%s | erro=%s',
                    platform, code, safe_debug(str(exc)))
        raise PlatformAccessError(f'[{code}] {source_failure_message(platform, code)}', code=code) from exc


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
        inspect_output_audio(output)

        return {
            'original_duration': round(original_duration, 1),
            'final_duration': round(final_duration, 1),
            'size_bytes': output.stat().st_size,
        }
    except Exception:
        # Evita disponibilizar ou deixar no histórico um arquivo incompleto.
        output.unlink(missing_ok=True)
        raise

def inspect_output_audio(path: Path) -> dict:
    """Verificação técnica antes de chamar Roblox; NÃO é um teste de direitos autorais."""
    proc = subprocess.run([
        'ffprobe', '-v', 'error', '-select_streams', 'a:0',
        '-show_entries', 'stream=codec_name,sample_rate,channels',
        '-of', 'json', str(path)
    ], capture_output=True, text=True, timeout=25, check=False)
    if proc.returncode:
        raise ProcessingError('O arquivo final não pôde ser analisado pelo ffprobe.')
    try:
        audio = json.loads(proc.stdout).get('streams', [])[0]
        codec = audio['codec_name']
        sr = int(audio['sample_rate'])
        channels = int(audio['channels'])
    except (IndexError, KeyError, ValueError, TypeError, json.JSONDecodeError):
        raise ProcessingError('O arquivo final não contém uma faixa de áudio válida.')
    if codec not in ('mp3', 'vorbis') or sr != 44100 or channels not in (1, 2):
        raise ProcessingError('Formato de áudio inválido para Roblox: reexporte em MP3 ou OGG, 44.1 kHz, mono/estéreo.')
    return {'codec': codec, 'sample_rate': sr, 'channels': channels}


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
