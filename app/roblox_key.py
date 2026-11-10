"""Introspecção oficial da API Key Roblox, executada APENAS no backend.

Endpoint documentado: https://apis.roblox.com/api-keys/v1/introspect
O resultado fornece authorizedUserId e, quando disponíveis, userIds/groupIds
com escopo asset/write. Nunca serializar ou registrar o valor da API Key.
"""
from __future__ import annotations
import re
import httpx

URL = 'https://apis.roblox.com/api-keys/v1/introspect'
ID_PATTERN = re.compile(r'^[1-9][0-9]{0,19}$')


class KeyInspectionError(Exception):
    pass


def _ids(items):
    if not isinstance(items, list):
        return []
    found=[]
    for value in items:
        item = str(value).strip()
        if item == '*' or ID_PATTERN.fullmatch(item):
            if item not in found: found.append(item)
    return found


def parse_introspection(payload):
    """Extrai destinos reais sem assumir que uma chave revela um único grupo."""
    if not isinstance(payload, dict):
        raise KeyInspectionError('Roblox retornou um formato de chave desconhecido.')
    if payload.get('enabled') is False:
        raise KeyInspectionError('Esta API Key está desativada no Roblox Creator Hub.')
    if payload.get('expired') is True:
        raise KeyInspectionError('Esta API Key expirou. Gere ou renove a chave no Creator Hub.')

    owner = str(payload.get('authorizedUserId') or '').strip()
    if not ID_PATTERN.fullmatch(owner):
        owner = ''
    users, groups = [], []
    writable=False
    for scope in payload.get('scopes') or []:
        if not isinstance(scope, dict): continue
        if str(scope.get('name','')).lower() not in ('asset', 'assets'): continue
        operations = {str(x).strip().lower() for x in (scope.get('operations') or [])}
        if 'write' not in operations: continue
        writable=True
        for item in _ids(scope.get('userIds')):
            if item not in users:users.append(item)
        for item in _ids(scope.get('groupIds')):
            if item not in groups:groups.append(item)
    if not writable:
        raise KeyInspectionError('A chave não possui Assets > Write. Ative essa permissão no Creator Hub.')

    # Escopos com lista "*" permitem vários destinos, mas não identificam
    # o ID do grupo. Para usuário, authorizedUserId identifica o dono da chave.
    if owner and ('*' in users or owner in users or (not users and not groups)):
        preferred_user = owner
    else:
        permitted_users = [item for item in users if item != '*']
        preferred_user = permitted_users[0] if len(permitted_users) == 1 else ''
    explicit_groups = [item for item in groups if item != '*']
    preferred_group = explicit_groups[0] if len(explicit_groups)==1 and '*' not in groups else ''
    default_type = 'user' if preferred_user else ('group' if preferred_group else None)
    default_id = preferred_user or preferred_group or None
    return {
        'authorized_user_id': owner or None,
        'allowed_user_ids': users,
        'allowed_group_ids': groups,
        'default_creator_type': default_type,
        'default_creator_id': default_id,
        'suggested_group_id': preferred_group or None,
        'asset_write': True,
        'needs_group_choice': ('*' in groups or len(explicit_groups)>1),
        'note': ('A API Key informa o usuário autorizado; para publicar em grupo, escolha o grupo '
                 'uma vez em Roblox Key se houver vários grupos ou permissão genérica.'),
    }


def inspect_key(api_key: str, *, client=None):
    if not isinstance(api_key,str) or len(api_key.strip()) < 16 or len(api_key)>5000:
        raise KeyInspectionError('Cole uma API Key Roblox completa e válida.')
    own_client = client is None
    if own_client:
        client = httpx.Client(timeout=httpx.Timeout(12, connect=5), follow_redirects=False)
    try:
        try:
            r=client.post(URL, json={'apiKey':api_key.strip()}, headers={'Accept':'application/json'})
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise KeyInspectionError('Roblox indisponível para validar a chave neste momento.') from exc
        if r.status_code in (401,403):
            raise KeyInspectionError('A chave foi recusada pelo Roblox. Verifique permissões e restrições de IP do Railway.')
        if r.status_code==429:
            raise KeyInspectionError('Limite de verificações de chave atingido no Roblox. Tente mais tarde.')
        if r.status_code!=200:
            raise KeyInspectionError(f'Não foi possível verificar a chave no Roblox (HTTP {r.status_code}).')
        try:
            result=parse_introspection(r.json())
        except ValueError as exc:
            raise KeyInspectionError('Roblox retornou uma resposta de chave inválida.') from exc
        return result
    finally:
        if own_client: client.close()


def allowed_destination(inspection: dict, creator_type: str, creator_id: str):
    if not ID_PATTERN.fullmatch(str(creator_id)):
        return False
    allowed=inspection['allowed_user_ids'] if creator_type=='user' else inspection['allowed_group_ids']
    if '*' in allowed or creator_id in allowed:
        return True
    # A ausência de destinos nos dois arrays pode representar autorização
    # implícita ao próprio usuário, mas nunca autoriza usuário em chave só de grupo.
    return (creator_type=='user' and not inspection['allowed_user_ids'] and
            not inspection['allowed_group_ids'] and
            inspection.get('authorized_user_id')==creator_id)
