"""Processamento de áudio, integração com Roblox e validações, sem acoplamento à interface."""
import json
import os
import re
import subprocess
import time
import logging
import shutil
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import httpx
from curl_cffi import CurlMime
from curl_cffi.requests import Session as CurlSession
from curl_cffi.requests.exceptions import RequestException as CurlError
from . import provider_guard

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
    # HTTP 429 tem prioridade mesmo quando a resposta também menciona login.
    if any(x in msg for x in ('http error 429', 'too many requests', 'rate limit')):
        return 'SOURCE_RATE_LIMIT'
    if any(x in msg for x in ('not a bot', 'confirm you', 'captcha', 'unusual traffic', 'login_required', 'sign in to confirm')):
        return 'YOUTUBE_VERIFICATION' if platform == 'YouTube' else 'PROVIDER_VERIFICATION'
    if any(x in msg for x in ('unable to extract client id', 'could not extract client id', 'client_id')):
        return 'SOUNDCLOUD_CLIENT_ID'
    if any(x in msg for x in ('aws waf', 'cloudflare challenge', 'challenge required', 'datadome')):
        return 'PROVIDER_VERIFICATION'
    if any(x in msg for x in ('login required', 'requires login', 'private video', 'private track', 'members-only', 'age-restricted')):
        return 'SOURCE_RESTRICTED'
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
        'YOUTUBE_VERIFICATION': 'O YouTube exigiu verificação de acesso (LOGIN_REQUIRED). Vincular o Google OAuth2 não fornece uma sessão de download ao yt-dlp. Para áudios autorizados, use um arquivo disponibilizado pelo titular ou tente novamente quando o acesso estiver disponível.',
        'SOUNDCLOUD_CLIENT_ID': 'O extrator não conseguiu obter a identificação de acesso do SoundCloud. Isso pode acontecer quando o serviço limita acessos de servidores. Se a faixa for sua, use o arquivo original; para download habilitado, utilize a opção oficial Baixar arquivo no SoundCloud.',
        'PROVIDER_VERIFICATION': f'O {platform} exigiu verificação de acesso que este servidor não consegue concluir. Use um arquivo disponibilizado pelo titular para download.',
        'SOURCE_RESTRICTED': f'O conteúdo do {platform} exige login, autorização ou é privado. O Naples Studios não acessa conteúdos restritos.',
        'SOURCE_RATE_LIMIT': f'O {platform} limitou a importação (HTTP 429). A pausa interna do YouTube está desativada por padrão, mas isso não libera o acesso da origem. Aguarde a liberação ou use um arquivo autorizado.',
        'SOURCE_COOLDOWN': f'O {platform} está em pausa temporária após uma restrição de acesso. Não repetiremos as tentativas até o fim da pausa. Arquivos locais continuam disponíveis.',
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

def _direct_audio_host(host: str) -> bool:
    """Somente provedores conhecidos de object storage público/URL assinada.

    Não aceitamos destinos arbitrários de rede nem redirecionamentos para evitar SSRF.
    A lista não concede permissão para copiar conteúdo de terceiros.
    """
    if host == 'storage.googleapis.com' or host.endswith('.storage.googleapis.com'):
        return True
    if host.startswith('pub-') and host.endswith('.r2.dev') and host.count('.') == 2:
        return True
    if host == 's3.amazonaws.com' or re.fullmatch(r's3[.-][a-z0-9-]+\.amazonaws\.com', host):
        return True
    if re.fullmatch(r'[a-z0-9.-]+\.s3[.-][a-z0-9-]+\.amazonaws\.com', host):
        return True
    if host.endswith('.blob.core.windows.net') and host.count('.') == 4:
        return True
    if host.endswith('.digitaloceanspaces.com') and host.count('.') >= 2:
        return True
    return False


def platform_from_url(value):
    try:
        parsed = urlparse(value)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.port:
            raise ValueError()
        host = parsed.hostname.lower().rstrip('.')
        youtube = {'youtube.com','www.youtube.com','m.youtube.com','music.youtube.com','youtu.be','www.youtu.be'}
        soundcloud = {'soundcloud.com','www.soundcloud.com','on.soundcloud.com'}
        if len(value) > 3000 or any(ord(c) < 32 for c in value):
            raise ValueError()
        if host in youtube:
            return 'YouTube'
        if host in soundcloud:
            return 'SoundCloud'
        if _direct_audio_host(host):
            return 'Arquivo HTTPS'
        raise ValueError()
    except (ValueError, TypeError):
        raise ProcessingError('Use um link HTTPS do YouTube/SoundCloud ou um link direto de arquivo em R2, S3, GCS, Azure Blob ou Spaces.')


def source_reference_for_history(url: str) -> str:
    """Não armazena tokens de assinatura de links de arquivos."""
    if platform_from_url(url) == 'Arquivo HTTPS':
        p = urlparse(url)
        return urlunparse((p.scheme, p.netloc, p.path, '', '', ''))[:1000]
    return url[:1000]


def download_direct_audio(url: str, workdir: Path, client=None) -> Path:
    """Baixa exclusivamente objetos públicos/autorizados de provedores conhecidos.

    Sem cookies, sem seguir redirects, com tamanho máximo de 50 MB. Arquivo obtido
    é validado pelo ffprobe/FFmpeg posteriormente; não confia na extensão.
    """
    if platform_from_url(url) != 'Arquivo HTTPS':
        raise ProcessingError('O endereço não é um link direto de áudio autorizado.')
    owns_client = client is None
    if owns_client:
        client = httpx.Client(timeout=httpx.Timeout(40.0, connect=10.0), follow_redirects=False, trust_env=False)
    workdir.mkdir(parents=True, exist_ok=True)
    target = workdir / 'fonte.media'
    total = 0
    try:
        with client.stream('GET', url, headers={'Accept':'audio/*,application/octet-stream,video/webm,application/ogg'}) as response:
            if response.status_code in (301,302,303,307,308):
                raise PlatformAccessError('O link redirecionou. Cole a URL final direta do arquivo, emitida pelo seu armazenamento.', 'DIRECT_REDIRECT')
            if response.status_code >= 400:
                raise PlatformAccessError(f'Arquivo HTTPS indisponível (HTTP {response.status_code}). Verifique se o link está válido e liberado para download.', 'DIRECT_HTTP')
            length = response.headers.get('content-length','')
            if length.isdigit() and int(length) > MAX_FILE_BYTES:
                raise PlatformAccessError('O arquivo original excede 50 MB.', 'DIRECT_TOO_LARGE')
            content_type = response.headers.get('content-type','').split(';')[0].strip().lower()
            if content_type.startswith('text/') or content_type in ('application/json','application/xml','text/html'):
                raise PlatformAccessError('O endereço retornou uma página/texto em vez de um arquivo de áudio.', 'DIRECT_NOT_AUDIO')
            with target.open('wb') as destination:
                for chunk in response.iter_bytes(chunk_size=256*1024):
                    if chunk:
                        total += len(chunk)
                        if total > MAX_FILE_BYTES:
                            raise PlatformAccessError('O arquivo original excede 50 MB.', 'DIRECT_TOO_LARGE')
                        destination.write(chunk)
        if not total:
            raise PlatformAccessError('O endereço retornou um arquivo vazio.', 'DIRECT_EMPTY')
        return target
    except httpx.RequestError as exc:
        raise PlatformAccessError(f'Falha ao conectar ao armazenamento do arquivo ({type(exc).__name__}).', 'DIRECT_NETWORK') from exc
    except Exception:
        target.unlink(missing_ok=True)
        raise
    finally:
        if owns_client:
            client.close()


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

def normalize_youtube_video_url(url: str) -> str:
    """Valida um único vídeo e remove parâmetros de rastreamento/playlist.

    Não altera cookies, assinatura, geolocalização, autenticação ou verificações
    da origem. URLs de canais, playlists e buscas não são arquivos individuais.
    """
    if platform_from_url(url) != 'YouTube':
        return url
    from urllib.parse import parse_qs
    p = urlparse(url)
    host = (p.hostname or '').lower()
    path = p.path.rstrip('/')
    video_id = ''
    if host in ('youtu.be', 'www.youtu.be'):
        segments = path.strip('/').split('/')
        if len(segments) == 1:
            video_id = segments[0]
    elif path == '/watch':
        video_id = (parse_qs(p.query).get('v') or [''])[0]
    else:
        segments = path.strip('/').split('/')
        if len(segments) == 2 and segments[0] in ('shorts', 'live', 'embed', 'v'):
            video_id = segments[1]
    if video_id:
        # IDs oficiais conhecidos são normalizados; formatos inesperados
        # continuam disponíveis para o extrator, preservando compatibilidade.
        if re.fullmatch(r'[a-zA-Z0-9_-]{11}', video_id):
            return f'https://www.youtube.com/watch?v={video_id}'
        return url
    raise ProcessingError('Cole o link HTTPS de um vídeo individual do YouTube (watch, youtu.be, Shorts ou live). Links de canal, playlist e busca não são aceitos.')


def download_platform(url: str, workdir: Path) -> Path:
    platform = platform_from_url(url)
    if platform == 'YouTube':
        url = normalize_youtube_video_url(url)
    if platform == 'Arquivo HTTPS':
        return download_direct_audio(url, workdir)
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
        '--socket-timeout', '15', '--retries', '1', '--fragment-retries', '1',
        '--retry-sleep', 'http:exp=1:8', '--retry-sleep', 'fragment:exp=1:8',
        '--format', 'bestaudio/best', '--output', output_pattern,
        '--', url,
    ]
    try:
        with provider_guard.download_slot(platform):
            run(command, 180)
    except provider_guard.ProviderPaused as exc:
        raise PlatformAccessError(f'[SOURCE_COOLDOWN] {source_failure_message(platform, "SOURCE_COOLDOWN")} Tempo restante: {exc.seconds}s.', code='SOURCE_COOLDOWN') from exc
    except ProcessingError as exc:
        # Nunca publica os logs brutos do yt-dlp, URLs privadas ou dados de sessão.
        if str(exc).startswith('Ferramenta yt-dlp não instalada'):
            raise PlatformAccessError('O yt-dlp não está instalado no servidor. Refaça o deploy do Dockerfile.') from exc
        code = source_error_code(platform, str(exc))
        provider_guard.record_failure(platform, code)
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

def _oembed_preview(url: str, platform: str, client=None) -> dict:
    """Metadata pública; não libera nem baixa o áudio do YouTube/SoundCloud."""
    endpoint = ('https://www.youtube.com/oembed' if platform == 'YouTube'
                else 'https://soundcloud.com/oembed')
    owns_client = client is None
    if owns_client:
        client = httpx.Client(timeout=httpx.Timeout(9.0, connect=4.0), follow_redirects=False, trust_env=False)
    try:
        response = client.get(endpoint, params={'url': url, 'format': 'json'})
        if response.status_code == 429:
            provider_guard.record_failure(platform, 'SOURCE_RATE_LIMIT')
            raise ValueError('Limite de prévias públicas (HTTP 429)')
        if response.status_code != 200 or len(response.content) > 120_000:
            raise ValueError('Metadados indisponíveis')
        data = response.json()
        title = str(data.get('title','')).strip()[:120]
        if not title:
            raise ValueError('Título ausente')
        return {
            'platform': platform, 'title': title, 'duration': None,
            'uploader': str(data.get('author_name') or '')[:100],
            'preview_status': 'metadata_only', 'audio_access_verified': False,
            'note': 'Metadados públicos consultados. Esta prévia NÃO confirma que a origem fornecerá o áudio.'
        }
    finally:
        if owns_client:
            client.close()


def preview_platform(url: str) -> dict:
    """Prévia pública com cache; não força yt-dlp quando o YouTube limita acesso.

    A prévia serve para mostrar metadados e não comprova autorização para baixar.
    Nenhuma chamada de prévia executa download da mídia.
    """
    platform = platform_from_url(url)
    if platform == 'Arquivo HTTPS':
        raw_name = urlparse(url).path.rsplit('/', 1)[-1] or 'Áudio hospedado'
        raw_name = re.sub(r'[^\w .()\-]', '_', raw_name, flags=re.UNICODE).strip()
        return {'platform': platform, 'title': (raw_name or 'Áudio hospedado')[:120],
                'duration': None, 'uploader': '', 'preview_status': 'metadata_only',
                'audio_access_verified': False,
                'note': 'Link direto; acesso, tamanho e codec serão validados durante a importação.'}
    cache_url = normalize_youtube_video_url(url) if platform == 'YouTube' else url
    from hashlib import sha256
    # Nunca salva URLs assinadas ou de sessão em cache persistente.
    cache_key = platform + ':' + sha256(cache_url.encode('utf-8')).hexdigest()
    result = provider_guard.preview_get(cache_key)
    if result is not None:
        return result
    try:
        provider_guard.assert_available(platform)
        result = _oembed_preview(cache_url, platform)
        provider_guard.preview_put(cache_key, result)
        return result
    except provider_guard.ProviderPaused as exc:
        raise PlatformAccessError(
            f'[SOURCE_COOLDOWN] {source_failure_message(platform, "SOURCE_COOLDOWN")} Tempo restante: {exc.seconds}s.',
            code='SOURCE_COOLDOWN') from exc
    except (ValueError, httpx.RequestError, json.JSONDecodeError, httpx.HTTPStatusError) as exc:
        # Após um oEmbed indisponível, não dispara automaticamente um segundo
        # extractor yt-dlp para o MESMO link. Evita multiplicar HTTP 429.
        log.info('Prévia pública %s indisponível: %s', platform, type(exc).__name__)
        limited = isinstance(exc, ValueError) and 'HTTP 429' in str(exc)
        result = {'platform': platform, 'title': 'Prévia indisponível', 'duration': None,
                  'uploader': '', 'preview_status': 'rate_limited' if limited else 'unavailable',
                  'audio_access_verified': False,
                  'note': ('A origem limitou a consulta de metadados (HTTP 429). Isso não é uma pausa do Naples Studios.'
                           if limited else 'Não foi possível consultar os metadados públicos. O acesso ao áudio também não está confirmado.')}
        provider_guard.preview_put(cache_key, result, ttl_seconds=180)
        return result


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
    """Prepara áudio autorizado: 2.5x COM pitch, EQ, volume e validação técnica.

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
    # 4. Aplica EQ sobre o áudio já acelerado (Hz após a mudança de pitch).
    # 5. Normaliza loudness e limita picos reais para reduzir distorção de saída.
    #    loudnorm em passagem única opera de modo dinâmico: melhora a padronização,
    #    mas não repara distorções presentes na fonte e não altera a moderação Roblox.
    # 6. Atenua a saída em 50% (equivalente a aproximadamente -6,02 dB),
    #    para compensar o Sound.Volume=2 no Roblox. Não usar volume=0:
    #    silêncio digital multiplicado por 2 continua sendo silêncio.
    #    IMPORTANTE: ganho deve ser aplicado APÓS loudnorm; caso contrário,
    #    loudnorm elevaria o nível novamente e anularia a atenuação.
    # 7. loudnorm pode usar oversampling; voltamos explicitamente a 44,1 kHz.
    audio_filters = (
        'aresample=44100,'
        'asetrate=110250,'
        'aresample=44100,'
        'highpass=f=100,'
        'lowpass=f=15000,'
        'loudnorm=I=-16:TP=-2:LRA=11,'
        'volume=0.5,'
        'aresample=44100'
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
        validate_audio_decode(output)

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


def validate_audio_decode(path: Path) -> None:
    """Decodifica a saída inteira para detectar arquivos corrompidos ou incompletos.

    Sem áudio exportado para stdout; o FFmpeg apenas verifica os frames.
    Erros interrompem o envio antes de chamar a API do Roblox.
    """
    try:
        process = subprocess.run([
            'ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error',
            '-xerror', '-i', str(path), '-map', '0:a:0', '-f', 'null', '-'
        ], capture_output=True, text=True, timeout=90, check=False)
    except FileNotFoundError as exc:
        raise ProcessingError('FFmpeg indisponível para validar o áudio final.') from exc
    except subprocess.TimeoutExpired as exc:
        raise ProcessingError('Verificação de integridade do áudio excedeu o tempo limite.') from exc
    if process.returncode != 0:
        raise ProcessingError('Arquivo final não passou na verificação de integridade; upload cancelado.')


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
        # Roblox exige o campo description; string vazia evita texto automático.
        'description': '',
        'creationContext': {'creator': {key: creator_id}}
    }
    owns_client = client is None
    if owns_client:
        client = CurlSession(timeout=150, http_version='v1', trust_env=False, allow_redirects=False)
    try:
        if owns_client:
            form = CurlMime()
            try:
                form.addpart(
                    name='request',
                    data=json.dumps(payload, ensure_ascii=False).encode('utf-8')
                )
                form.addpart(
                    name='fileContent',
                    filename=mp3.name,
                    content_type=content_type,
                    local_path=str(mp3)
                )
                resp = client.post(
                    CREATE_URL,
                    headers={'x-api-key': api_key},
                    multipart=form
                )
            finally:
                form.close()
        else:
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
    except (httpx.RequestError, CurlError) as e:
        raise ProcessingError(f'Falha de comunicação com Roblox: {type(e).__name__}.')
    finally:
        if owns_client:
            client.close()

def poll_roblox_asset(operation: str, api_key: str, client=None, tries=30, interval=3):
    if not re.fullmatch(r'operations/[A-Za-z0-9_\-]+', operation):
        raise ProcessingError('ID da operação inválido.')
    owns_client = client is None
    if owns_client:
        client = CurlSession(timeout=30, http_version='v1', trust_env=False, allow_redirects=False)
    try:
        for attempt in range(tries):
            try:
                response = client.get(OPERATIONS_URL + operation.split('/', 1)[1], headers={'x-api-key': api_key})
            except (httpx.RequestError, CurlError):
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
