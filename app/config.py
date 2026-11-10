"""Configuração em memória recarregável a partir de banco criptografado.
   Os padrões podem ser migrados de variáveis antigas, mas nenhum .env é obrigatório.
"""
import os
import secrets
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.getenv('NAPLES_DATA_DIR', str(ROOT / 'data'))).resolve()
SESSION_HOURS = 12
SESSION_COOKIE = 'naples_session'
OAUTH_COOKIE = 'naples_oauth_state'
ADMIN_COOKIE = 'naples_admin'
LEGACY_DISCORD_OWNER_ID = os.getenv('LEGACY_DISCORD_OWNER_ID', '')

APP_BASE_URL = 'http://127.0.0.1:8000'
DISCORD_CLIENT_ID = ''
DISCORD_CLIENT_SECRET = ''
DISCORD_ALLOWED_USER_IDS = set()
# Cadastro livre: qualquer Discord autenticado entra sem aprovação manual.
DISCORD_PUBLIC_REGISTRATION = True
ROBLOX_API_KEY = ''
ROBLOX_CREATOR_ID = ''
ROBLOX_CREATOR_TYPE = 'user'
ROBLOX_ALLOWED_CREATOR_IDS = set()
ENABLE_PLATFORM_LINKS = True
COOKIE_SECURE = False
# Chave de sessão só existe depois da criação do administrador; nenhum default inseguro em produção.
SESSION_SECRET = ''


def reload_settings():
    from . import settings_store
    persisted = settings_store.read_values()
    def get(k, env='', default=''):
        # Recupera configurações anteriores, mas permite Railway Variables
        # preencher OAuth2 caso o valor persistido esteja vazio.
        saved = persisted.get(k)
        return saved if saved not in ('', None) else (os.getenv(env, default) if env else default)
    global APP_BASE_URL, DISCORD_CLIENT_ID, DISCORD_CLIENT_SECRET, DISCORD_ALLOWED_USER_IDS, DISCORD_PUBLIC_REGISTRATION
    global ROBLOX_API_KEY, ROBLOX_CREATOR_ID, ROBLOX_CREATOR_TYPE, ROBLOX_ALLOWED_CREATOR_IDS
    global ENABLE_PLATFORM_LINKS, COOKIE_SECURE, SESSION_SECRET
    APP_BASE_URL = str(get('app_base_url', 'APP_BASE_URL', 'http://127.0.0.1:8000')).rstrip('/')
    DISCORD_CLIENT_ID = str(get('discord_client_id', 'DISCORD_CLIENT_ID'))
    DISCORD_CLIENT_SECRET = str(get('discord_client_secret', 'DISCORD_CLIENT_SECRET'))
    DISCORD_ALLOWED_USER_IDS = {x.strip() for x in str(get('discord_allowed_user_ids', 'DISCORD_ALLOWED_USER_IDS')).split(',') if x.strip()}
    raw_public = get('discord_public_registration', 'DISCORD_PUBLIC_REGISTRATION', '1')
    DISCORD_PUBLIC_REGISTRATION = raw_public is True or str(raw_public).lower() in ('1','true','on')
    ROBLOX_API_KEY = str(get('roblox_api_key', 'ROBLOX_API_KEY'))
    ROBLOX_CREATOR_ID = str(get('roblox_creator_id', 'ROBLOX_CREATOR_ID'))
    ROBLOX_CREATOR_TYPE = str(get('roblox_creator_type', 'ROBLOX_CREATOR_TYPE', 'user'))
    ROBLOX_ALLOWED_CREATOR_IDS = {x.strip() for x in str(get('roblox_allowed_creator_ids', 'ROBLOX_ALLOWED_CREATOR_IDS')).split(',') if x.strip()}
    raw_links = get('enable_platform_links', 'ENABLE_PLATFORM_LINKS', '1')
    ENABLE_PLATFORM_LINKS = raw_links is True or str(raw_links).lower() in ('1','true','on')
    raw_secure = get('cookie_secure', 'COOKIE_SECURE', '0')
    COOKIE_SECURE = raw_secure is True or str(raw_secure).lower() in ('1','true','on')
    SESSION_SECRET = str(get('session_secret', 'SESSION_SECRET'))


def ready():
    return (DISCORD_CLIENT_ID.isdigit() and bool(DISCORD_CLIENT_SECRET) and
            (DISCORD_PUBLIC_REGISTRATION or bool(DISCORD_ALLOWED_USER_IDS)) and all(x.isdigit() for x in DISCORD_ALLOWED_USER_IDS) and
            len(ROBLOX_API_KEY) >= 16 and bool(ROBLOX_CREATOR_ID) and ROBLOX_CREATOR_ID.isdigit() and
            ROBLOX_CREATOR_TYPE in ('user','group') and len(SESSION_SECRET) >= 32)


def validate_settings(values):
    base = str(values['app_base_url']).strip().rstrip('/')
    p = urlparse(base)
    if p.scheme not in ('http', 'https') or not p.netloc or p.path or p.query or p.fragment or p.username or p.password:
        raise ValueError('Informe a URL base do site, sem caminhos (ex.: http://127.0.0.1:8000).')
    if p.scheme == 'http' and p.hostname not in ('127.0.0.1','localhost','::1'):
        raise ValueError('Para acesso externo, configure um domínio HTTPS.')
    if p.scheme == 'https' and not values['cookie_secure']:
        raise ValueError('Ative cookies seguros para URLs HTTPS.')
    if values['cookie_secure'] and p.scheme != 'https':
        raise ValueError('Cookies seguros exigem HTTPS. Desative-os no modo local.')
    if not str(values['discord_client_id']).isdigit():
        raise ValueError('Client ID do Discord deve ser numérico.')
    ids = [x.strip() for x in str(values['discord_allowed_user_ids']).split(',') if x.strip()]
    if not all(x.isdigit() for x in ids):
        raise ValueError('IDs de usuário Discord precisam ser numéricos.')
    if not values.get('discord_public_registration', True) and not ids:
        raise ValueError('No modo restrito, informe ao menos um ID Discord autorizado.')
    if values.get('roblox_creator_id') and not str(values['roblox_creator_id']).isdigit():
        raise ValueError('Informe um ID numérico de usuário/grupo Roblox.')
    if values.get('roblox_api_key') and not str(values['roblox_creator_id']).isdigit():
        raise ValueError('Uma API Key Roblox salva precisa ter ID de criador válido.')
    if values['roblox_creator_type'] not in ('user','group'):
        raise ValueError('Tipo de criador Roblox inválido.')
    allowed = [x.strip() for x in str(values['roblox_allowed_creator_ids']).split(',') if x.strip()]
    if not all(x.isdigit() for x in allowed):
        raise ValueError('IDs Roblox permitidos precisam ser numéricos.')
    return base


def oauth_ready():
    """Discord OAuth is application-level; Roblox is set after login per user."""
    return (DISCORD_CLIENT_ID.isdigit() and bool(DISCORD_CLIENT_SECRET) and
            (DISCORD_PUBLIC_REGISTRATION or bool(DISCORD_ALLOWED_USER_IDS)) and
            all(i.isdigit() for i in DISCORD_ALLOWED_USER_IDS) and
            len(SESSION_SECRET) >= 32)
