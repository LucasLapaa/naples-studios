"""Naples Studios v24: publicação assíncrona de imagens por Roblox Open Cloud.

Não modifica o pipeline de áudio. A imagem pertence sempre ao criador ligado
à chave Roblox pessoal e os registros ficam isolados por Discord ID.
"""
import json
import re
import subprocess
import time
from pathlib import Path

import httpx

from . import database as db
from .core import CREATE_URL, ProcessingError, ensure_http_ok, poll_roblox_asset

MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_DIMENSION = 7999  # Roblox exige menor que 8000 x 8000


def initialize():
    with db.connect() as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS image_jobs (
            id TEXT PRIMARY KEY, discord_id TEXT NOT NULL,
            title TEXT NOT NULL, file_name TEXT NOT NULL,
            asset_type TEXT NOT NULL, creator_type TEXT NOT NULL,
            creator_id TEXT NOT NULL, status TEXT NOT NULL,
            asset_id TEXT, operation TEXT, error TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )''')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_image_jobs_owner ON image_jobs(discord_id, created_at)')
        # O processo de envio não é durável entre deploys, mas uma operação criada
        # no Roblox permanece recuperável pela rota de verificação.
        conn.execute("UPDATE image_jobs SET status=CASE WHEN operation IS NULL THEN 'interrompido' ELSE 'pendente' END, "
                     "error='Servidor reiniciado; confira a operação.' WHERE status IN ('fila','enviando','confirmando')")


def create_job(job_id, user_id, title, filename, asset_type, creator_type, creator_id):
    with db.connect() as conn:
        conn.execute('''INSERT INTO image_jobs
          (id,discord_id,title,file_name,asset_type,creator_type,creator_id,status)
          VALUES (?,?,?,?,?,?,?,'fila')''', (job_id, user_id, title, filename, asset_type, creator_type, creator_id))


def update(job_id, **kwargs):
    allowed = {'status', 'asset_id', 'operation', 'error'}
    fields = {k: v for k, v in kwargs.items() if k in allowed}
    if not fields:
        return
    with db.connect() as conn:
        conn.execute('UPDATE image_jobs SET '+', '.join(k+'=?' for k in fields)+", updated_at=datetime('now') WHERE id=?",
                     (*fields.values(), job_id))


def list_jobs(user_id):
    with db.connect() as conn:
        return [dict(r) for r in conn.execute('''SELECT id,title,file_name,asset_type,status,asset_id,
        operation,error,created_at,updated_at FROM image_jobs WHERE discord_id=?
        ORDER BY created_at DESC,rowid DESC LIMIT 100''', (user_id,)).fetchall()]


def get_job(user_id, job_id):
    with db.connect() as conn:
        r = conn.execute('SELECT * FROM image_jobs WHERE id=? AND discord_id=?', (job_id, user_id)).fetchone()
        return dict(r) if r else None


def validate_image(path: Path, filename: str) -> str:
    """Confere assinatura, dimensões reais e integridade sem dependência nova.

    O ffprobe já existe no ambiente de processamento de áudio.
    """
    ext = Path(filename).suffix.lower()
    if ext not in ('.png', '.jpg', '.jpeg'):
        raise ProcessingError('Imagem deve ser PNG ou JPG/JPEG.')
    size = path.stat().st_size
    if size < 32 or size > MAX_IMAGE_BYTES:
        raise ProcessingError('A imagem deve ter até 10 MB e não pode estar vazia.')
    head = path.open('rb').read(32)
    if ext == '.png':
        if not head.startswith(b'\x89PNG\r\n\x1a\n') or head[12:16] != b'IHDR':
            raise ProcessingError('PNG inválido ou incompatível com a extensão.')
        mime = 'image/png'
    else:
        if not head.startswith(b'\xff\xd8\xff'):
            raise ProcessingError('JPEG inválido ou incompatível com a extensão.')
        mime = 'image/jpeg'
    try:
        p = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0',
                            '-show_entries', 'stream=width,height', '-of', 'json', str(path)],
                           capture_output=True, text=True, timeout=20, check=False)
        info = json.loads(p.stdout)
        video = (info.get('streams') or [{}])[0]
        width, height = int(video.get('width', 0)), int(video.get('height', 0))
    except (OSError, ValueError, IndexError, KeyError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        raise ProcessingError('Não foi possível validar a imagem enviada.') from exc
    if p.returncode != 0 or min(width, height) <= 0 or max(width, height) > MAX_DIMENSION:
        raise ProcessingError('Imagem inválida ou dimensões acima do limite Roblox (menor que 8000×8000).')
    if width * height > 40_000_000:
        raise ProcessingError('Resolução muito alta. Envie uma imagem de até 40 megapixels.')
    return mime


def create_roblox_image(path, title, creator_id, creator_type, api_key, asset_type='Decal', client=None):
    if asset_type not in ('Decal', 'Image') or creator_type not in ('user', 'group'):
        raise ProcessingError('Tipo de imagem ou destino inválido.')
    if not re.fullmatch(r'[1-9][0-9]{0,19}', str(creator_id)):
        raise ProcessingError('ID do destino Roblox inválido.')
    mime = validate_image(path, path.name)
    key = 'userId' if creator_type == 'user' else 'groupId'
    payload = {'assetType': asset_type, 'displayName': title,
               'description': '', 'creationContext': {'creator': {key: str(creator_id)}}}
    owns_client = client is None
    if owns_client:
        client = httpx.Client(timeout=httpx.Timeout(120.0, connect=20.0), follow_redirects=False)
    try:
        with path.open('rb') as stream:
            response = client.post(CREATE_URL, headers={'x-api-key': api_key},
                                   data={'request': json.dumps(payload, ensure_ascii=False)},
                                   files={'fileContent': (path.name, stream, mime)})
        ensure_http_ok(response)
        obj = response.json()
        operation = obj.get('path') or obj.get('name') or ''
        if not isinstance(operation, str) or not re.fullmatch(r'operations/[A-Za-z0-9_-]+', operation):
            raise ProcessingError('Roblox não retornou uma operação de upload válida.')
        return operation
    except (httpx.RequestError, ValueError) as exc:
        raise ProcessingError(f'Erro de comunicação no upload da imagem ({type(exc).__name__}).') from exc
    finally:
        if owns_client:
            client.close()


def process_image(job_id, path, title, creator_id, creator_type, key, asset_type):
    try:
        update(job_id, status='enviando', error=None)
        operation = create_roblox_image(path, title, creator_id, creator_type, key, asset_type)
        update(job_id, status='confirmando', operation=operation)
        asset_id = poll_roblox_asset(operation, key, tries=18, interval=3)
        if asset_id:
            update(job_id, status='concluido', asset_id=str(asset_id), error=None)
        else:
            update(job_id, status='pendente', error='Roblox ainda processando; use Verificar publicação.')
    except ProcessingError as exc:
        update(job_id, status='erro', error=str(exc)[:600])
    except Exception as exc:
        update(job_id, status='erro', error=f'Falha interna no upload ({type(exc).__name__}).')
    finally:
        Path(path).unlink(missing_ok=True)
