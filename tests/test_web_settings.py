"""Verifica setup sem .env, proteção da área administrativa e segredos persistentes."""
import os
import stat
from pathlib import Path

from fastapi.testclient import TestClient
import app.main as m


def test_web_setup_password_security_persistence(tmp_path, monkeypatch):
    for key in ('APP_BASE_URL','COOKIE_SECURE','DISCORD_CLIENT_ID','DISCORD_CLIENT_SECRET','SESSION_SECRET',
                'DISCORD_ALLOWED_USER_IDS','ROBLOX_API_KEY','ROBLOX_CREATOR_ID','ROBLOX_CREATOR_TYPE',
                'ROBLOX_ALLOWED_CREATOR_IDS','ENABLE_PLATFORM_LINKS'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(m.config, 'DATA', tmp_path)
    monkeypatch.setattr(m.db, 'DB', tmp_path / 'rbxwave.sqlite3')
    monkeypatch.setattr(m, 'TEMP', tmp_path / 'tmp')
    monkeypatch.setattr(m, 'EXPORTS', tmp_path / 'exports')
    m.RATES.clear()
    with TestClient(m.app, follow_redirects=False) as client:
        status = client.get('/api/setup/status').json()
        assert status['bootstrapped'] is False and status['ready'] is False
        assert 'Configurações' in client.get('/configuracoes').text
        assert client.get('/api/admin/settings').status_code == 401
        assert client.post('/api/setup/bootstrap',json={'password':'short'}).status_code == 400
        # Attack: attempt initial setup via non-loopback host.
        bad_host = client.post('/api/setup/bootstrap',json={'password':'a-strong-password-1234'},headers={'Host':'external.example'})
        assert bad_host.status_code == 403
        init=client.post('/api/setup/bootstrap',json={'password':'a-strong-password-1234'})
        assert init.status_code == 200, init.text
        csrf=init.json()['csrf']
        assert csrf
        assert 'httponly' in init.headers['set-cookie'].lower()
        assert client.post('/api/setup/bootstrap',json={'password':'another-long-pass-123'}).status_code == 409
        assert client.get('/api/setup/status').json()['bootstrapped']
        assert client.get('/api/admin/settings').json()['settings']['roblox_api_key_configured'] is False
        payload={
            'app_base_url':'http://127.0.0.1:8000', 'cookie_secure':False,
            'discord_client_id':'123456789012345678', 'discord_client_secret':'discord-secret-very-private-321',
            'discord_allowed_user_ids':'111222333444555666, 222333444555666777',
            'roblox_api_key':'roblox-api-key-very-private-999', 'roblox_creator_id':'999888777',
            'roblox_creator_type':'user','roblox_allowed_creator_ids':'999888777','enable_platform_links':True,
        }
        assert client.put('/api/admin/settings',json=payload).status_code == 403
        assert client.put('/api/admin/settings',json=payload,headers={'X-CSRF-Token':'wrong'}).status_code == 403
        assert client.put('/api/admin/settings',json=payload,headers={'X-CSRF-Token':csrf,'Origin':'https://evil.test'}).status_code == 403
        save=client.put('/api/admin/settings',json=payload,headers={'X-CSRF-Token':csrf})
        assert save.status_code == 200,save.text
        assert save.json()['ready'] is True
        public=client.get('/api/admin/settings').json()['settings']
        assert public['discord_client_secret_configured'] is True
        assert public['roblox_api_key_configured'] is True
        assert 'discord_client_secret' not in public and 'roblox_api_key' not in public
        assert b'roblox-api-key-very-private-999' not in (tmp_path/'rbxwave.sqlite3').read_bytes()
        assert b'discord-secret-very-private-321' not in (tmp_path/'rbxwave.sqlite3').read_bytes()
        assert (tmp_path/'.naples-master.key').is_file()
        if os.name=='posix':
            assert stat.S_IMODE((tmp_path/'.naples-master.key').stat().st_mode)==0o600
        # Partial update does not erase secrets.
        same=dict(payload)
        same.pop('roblox_api_key');same.pop('discord_client_secret')
        same['roblox_creator_id']='222888111'
        edit=client.put('/api/admin/settings',json=same,headers={'X-CSRF-Token':csrf})
        assert edit.status_code == 200
        assert m.config.ROBLOX_API_KEY=='roblox-api-key-very-private-999'
        assert m.config.ROBLOX_CREATOR_ID=='222888111'
        assert m.config.DISCORD_CLIENT_SECRET=='discord-secret-very-private-321'
        m.config.reload_settings()
        assert m.config.ROBLOX_CREATOR_ID=='222888111'
        assert client.post('/api/admin/logout',json={},headers={'X-CSRF-Token':csrf}).status_code == 200
        assert client.get('/api/admin/settings').status_code==401
        assert client.post('/api/admin/login',json={'password':'badpassword'}).status_code==401
        new=client.post('/api/admin/login',json={'password':'a-strong-password-1234'})
        assert new.status_code==200
        csrf=new.json()['csrf']
        wrong_password=client.post('/api/admin/change-password',json={'old_password':'wrong','new_password':'new-long-password-888'},headers={'X-CSRF-Token':csrf})
        assert wrong_password.status_code==403
        change=client.post('/api/admin/change-password',json={'old_password':'a-strong-password-1234','new_password':'new-long-password-888'},headers={'X-CSRF-Token':csrf})
        assert change.status_code==200
        assert client.get('/api/admin/session').status_code==401
        assert client.post('/api/admin/login',json={'password':'new-long-password-888'}).status_code==200
        assert m.config.ready() is True
