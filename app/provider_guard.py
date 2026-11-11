"""Fila por provedor, com pausa LOCAL do YouTube desativada por padrão.

Isso não ignora erros externos HTTP 429 / LOGIN_REQUIRED: cada tentativa falha
normalmente se a origem recusar acesso, sem fazer retries automáticos adicionais.
SoundCloud mantém sua proteção anterior. Uma réplica/worker por processo.
"""
from collections import OrderedDict
from contextlib import contextmanager
from pathlib import Path
import json
import os
import tempfile
import threading
import time

PROVIDERS = ('YouTube', 'SoundCloud')
_lock = threading.RLock()
_provider_locks = {name: threading.Lock() for name in PROVIDERS}
_last_attempt = {}
_preview_cache = OrderedDict()
_MAX_PREVIEW_KEYS = 150


def _state_path():
    root = Path(os.getenv('NAPLES_PROVIDER_STATE_DIR') or os.getenv('NAPLES_DATA_DIR') or Path(__file__).resolve().parents[1] / 'data')
    return root / 'provider-cooldowns.json'


def _duration(env, default, low=0, high=24 * 3600):
    try:
        return max(low, min(high, float(os.getenv(env, str(default)))))
    except ValueError:
        return float(default)


def _load_state():
    try:
        raw = json.loads(_state_path().read_text(encoding='utf-8'))
        return raw if isinstance(raw, dict) else {}
    except (FileNotFoundError, OSError, json.JSONDecodeError, ValueError):
        return {}


def _save_state(state):
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=path.parent, prefix='.naples-guard-', delete=False) as f:
            temp_path = Path(f.name)
            json.dump(state, f)
        os.replace(temp_path, path)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def _youtube_pause_enabled():
    # Opcional: reativar a proteção antiga se o operador desejar.
    return os.getenv('NAPLES_YOUTUBE_PAUSE_ENABLED', '0').strip().lower() in ('1', 'true', 'yes', 'on')


def status(platform):
    # Ignora inclusive cooldowns antigos ainda existentes no volume do Railway.
    if platform == 'YouTube' and not _youtube_pause_enabled():
        return {'cooldown_seconds': 0, 'reason': None}
    if platform not in PROVIDERS:
        return {'cooldown_seconds': 0, 'reason': None}
    with _lock:
        data = _load_state().get(platform, {})
    remaining = max(0, int(float(data.get('until', 0)) - time.time() + 0.999))
    return {'cooldown_seconds': remaining, 'reason': str(data.get('reason') or '') if remaining else None}


def assert_available(platform):
    current = status(platform)
    if current['cooldown_seconds']:
        raise ProviderPaused(platform, current['cooldown_seconds'], current['reason'])


class ProviderPaused(Exception):
    def __init__(self, platform, seconds, reason):
        self.platform = platform
        self.seconds = seconds
        self.reason = reason
        super().__init__(f'{platform} em pausa temporária por {seconds} segundos; motivo: {reason}.')


@contextmanager
def download_slot(platform):
    """Uma extração por provedor; pausas não afetam outros provedores/arquivo local."""
    if platform not in PROVIDERS:
        yield
        return
    with _provider_locks[platform]:
        assert_available(platform)
        env = 'NAPLES_YOUTUBE_MIN_GAP_SECONDS' if platform == 'YouTube' else 'NAPLES_SOUNDCLOUD_MIN_GAP_SECONDS'
        min_gap = (0 if platform == 'YouTube' and not _youtube_pause_enabled()
                   else _duration(env, 25 if platform == 'YouTube' else 6, high=120))
        elapsed = time.monotonic() - _last_attempt.get(platform, -1e12)
        if elapsed < min_gap:
            time.sleep(min_gap - elapsed)
        assert_available(platform)
        _last_attempt[platform] = time.monotonic()
        yield


def record_failure(platform, code):
    """Pausar falhas de controle de acesso; não repete contra bloqueio conhecido."""
    if platform not in PROVIDERS:
        return
    if platform == 'YouTube' and not _youtube_pause_enabled():
        # Registra o erro no histórico da aplicação, mas NÃO cria pausa local.
        return
    if code == 'SOURCE_RATE_LIMIT':
        seconds = _duration('NAPLES_429_COOLDOWN_SECONDS', 3600, low=60)
    elif code in ('YOUTUBE_VERIFICATION', 'PROVIDER_VERIFICATION'):
        seconds = _duration('NAPLES_VERIFICATION_COOLDOWN_SECONDS', 1800, low=60)
    else:
        return
    with _lock:
        state = _load_state()
        current = state.get(platform, {})
        expiry = max(float(current.get('until', 0)), time.time() + seconds)
        state[platform] = {'until': expiry, 'reason': code}
        _save_state(state)


def preview_get(key):
    now = time.monotonic()
    with _lock:
        value = _preview_cache.get(key)
        if value is None:
            return None
        expiry, result = value
        if expiry < now:
            _preview_cache.pop(key, None)
            return None
        _preview_cache.move_to_end(key)
        return dict(result)


def preview_put(key, result, ttl_seconds=1800):
    with _lock:
        _preview_cache[key] = (time.monotonic() + ttl_seconds, dict(result))
        _preview_cache.move_to_end(key)
        while len(_preview_cache) > _MAX_PREVIEW_KEYS:
            _preview_cache.popitem(last=False)


def _reset_for_tests():
    """Utilizado exclusivamente por testes locais de unidade."""
    with _lock:
        _last_attempt.clear()
        _preview_cache.clear()
