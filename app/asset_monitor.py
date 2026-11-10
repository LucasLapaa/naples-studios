"""Leitura de metadados e moderação de assets pela API oficial Roblox Open Cloud.

O campo moderationResult.moderationState é a única fonte de 'Approved',
'Reviewing' ou 'Rejected'. Um 200, um Asset ID ou state=Active NÃO
significam aprovação.
"""
from __future__ import annotations

import re
import httpx

ASSET_ENDPOINT = 'https://apis.roblox.com/assets/v1/assets/'
ASSET_ID_RE = re.compile(r'^[1-9][0-9]{0,19}$')


def inspect_asset(asset_id: str, api_key: str, *, client=None) -> dict:
    if not ASSET_ID_RE.fullmatch(str(asset_id)):
        raise ValueError('Asset ID inválido.')
    if not api_key:
        return {'state': 'unavailable', 'message': 'API Key Roblox não configurada.'}
    close_client = client is None
    if close_client:
        client = httpx.Client(timeout=httpx.Timeout(14.0, connect=6.0), follow_redirects=False)
    try:
        try:
            response = client.get(ASSET_ENDPOINT + asset_id,
                                  headers={'x-api-key': api_key, 'Accept': 'application/json'})
        except (httpx.TimeoutException, httpx.TransportError):
            return {'state': 'unavailable', 'message': 'Não foi possível consultar a API Roblox agora.'}
        status = response.status_code
        if status == 401:
            return {'state': 'unauthorized', 'message': 'API Key inválida ou sem autenticação válida.'}
        if status == 403:
            return {'state': 'unauthorized', 'message': 'A chave precisa de Assets > Read ou não tem acesso a este asset.'}
        if status == 404:
            return {'state': 'not_found', 'message': 'Asset não encontrado ou não acessível com esta chave. Não indica moderação.'}
        if status == 429:
            return {'state': 'unavailable', 'message': 'Limite temporário da API Roblox atingido.'}
        if status < 200 or status >= 300:
            return {'state': 'unavailable', 'message': f'Consulta indisponível (HTTP {status}).'}
        try:
            payload = response.json()
        except ValueError:
            return {'state': 'unavailable', 'message': 'Resposta inválida da API Roblox.'}
        if not isinstance(payload, dict):
            return {'state': 'unavailable', 'message': 'Metadados do asset indisponíveis.'}
        mod = payload.get('moderationResult')
        raw = mod.get('moderationState') if isinstance(mod, dict) else None
        # Open Cloud usa valores como MODERATION_STATE_APPROVED (não apenas Approved).
        # Não inferir aprovação do HTTP 200 nem de `state=Active`.
        raw_value = str(raw or '').strip().upper()
        if raw_value.startswith('MODERATION_STATE_'):
            raw_value = raw_value[len('MODERATION_STATE_'):]
        states = {
            'APPROVED': 'approved',
            'REVIEWING': 'reviewing',
            'PENDING_REVIEW': 'reviewing',
            'IN_REVIEW': 'reviewing',
            'REJECTED': 'rejected',
        }
        state = states.get(raw_value, 'unknown')
        msg = {
            'approved': 'Roblox informa Approved: aprovado na moderação.',
            'reviewing': 'Roblox informa Reviewing: em análise.',
            'rejected': 'Roblox informa Rejected: recusado pela moderação.',
            'unknown': 'Asset encontrado, mas a API não informou o estado de moderação.',
        }[state]
        return {'state': state, 'message': msg,
                'asset_state': str(payload.get('state') or '')[:40],
                'display_name': str(payload.get('displayName') or '')[:100]}
    finally:
        if close_client:
            client.close()
