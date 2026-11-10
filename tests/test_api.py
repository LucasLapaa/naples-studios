import asyncio
import subprocess
import time
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient
import app.main as m


@pytest.fixture()
def client_and_db(tmp_path, monkeypatch):
    monkeypatch.setattr(m.config, 'DATA', tmp_path)
    monkeypatch.setattr(m.db, 'DB', tmp_path / 'rbxwave.sqlite3')
    monkeypatch.setattr(m, 'TEMP', tmp_path / 'tmp')
    monkeypatch.setattr(m, 'EXPORTS', tmp_path / 'exports')
    m.RATES.clear()
    with TestClient(m.app, follow_redirects=False) as client:
        yield client, tmp_path


class FakeDiscordClient:
    def __init__(self, *args, **kwargs): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    async def post(self, url, **kwargs):
        assert url.endswith('/oauth2/token')
        assert kwargs['data']['grant_type']=='authorization_code'
        assert kwargs['data']['redirect_uri']=='http://127.0.0.1:8000/auth/discord/callback'
        return httpx.Response(200, json={'access_token':'discord-access-only-test'})
    async def get(self, url, **kwargs):
        assert url.endswith('/users/@me')
        assert kwargs['headers']['Authorization']=='Bearer discord-access-only-test'
        return httpx.Response(200, json={'id':'111222333444555666','username':'Test User','avatar':'abcdef0123456789'})


def login(client, monkeypatch):
    monkeypatch.setattr(m.httpx, 'AsyncClient', FakeDiscordClient)
    start=client.get('/auth/discord')
    assert start.status_code==302
    query=parse_qs(urlparse(start.headers['location']).query)
    assert query['scope']==['identify']
    assert query['redirect_uri']==['http://127.0.0.1:8000/auth/discord/callback']
    state=query['state'][0]
    fail=client.get('/auth/discord/callback',params={'code':'abc','state':'wrong'})
    assert fail.status_code==400
    done=client.get('/auth/discord/callback',params={'code':'fake-code','state':state})
    assert done.status_code==303,done.text
    assert done.headers['location']=='/#dashboard'
    assert 'httponly' in done.headers['set-cookie'].lower()
    assert client.get('/auth/discord/callback',params={'code':'fake-code','state':state}).status_code==400
    data=client.get('/api/session')
    assert data.status_code==200
    assert data.json()['user']['username']=='Test User'
    return data.json()['csrf']


def test_oauth_state_session_and_csrf(client_and_db, monkeypatch):
    client, _ =client_and_db
    assert client.get('/api/session').status_code==401
    assert 'NAPLES' in client.get('/').text
    csrf=login(client,monkeypatch)
    assert client.get('/api/jobs').status_code==200
    assert client.post('/api/logout').status_code==403
    assert client.post('/api/logout',headers={'X-CSRF-Token':'wrong'}).status_code==403
    assert client.post('/api/logout',headers={'X-CSRF-Token':csrf,'Origin':'https://evil.test'}).status_code==403
    assert client.post('/api/logout',headers={'X-CSRF-Token':csrf}).status_code==200
    assert client.get('/api/jobs').status_code==401


def test_full_workflow_and_id_history(client_and_db, monkeypatch):
    client, data=client_and_db
    csrf=login(client,monkeypatch)
    events=[]
    def fake_upload(audio,title,creator_id,creator_type,key):
        events.append('upload')
        assert audio.is_file() and audio.stat().st_size>0
        assert creator_id=='999888777'
        assert key==m.config.ROBLOX_API_KEY
        return 'operations/test-op-123'
    def fake_poll(op,key,*args):
        events.append('poll')
        assert op=='operations/test-op-123'
        return '812345678901234'
    monkeypatch.setattr(m,'create_roblox_asset',fake_upload)
    monkeypatch.setattr(m,'poll_roblox_asset',fake_poll)
    wav=data/'original.wav'
    subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-f','lavfi','-i','sine=frequency=440:duration=4','-y',str(wav)],check=True)
    no_csrf=client.post('/api/jobs',data={'title':'Original autorizado','creator_id':'999888777','creator_type':'user','rights_confirmed':'true'},files={'audio_file':('teste.wav',wav.read_bytes(),'audio/wav')})
    assert no_csrf.status_code==403
    res=client.post('/api/jobs',data={'title':'Original autorizado','creator_id':'999888777','creator_type':'user','rights_confirmed':'true'},files={'audio_file':('teste.wav',wav.read_bytes(),'audio/wav')},headers={'X-CSRF-Token':csrf})
    assert res.status_code==202, res.text
    job_id=res.json()['job_id']
    for _ in range(120):
        record=next((x for x in client.get('/api/jobs').json()['jobs'] if x['id']==job_id),None)
        if record and record['status'] in ('concluido','erro'):
            break
        time.sleep(.09)
    assert record['status']=='concluido',record
    assert record['asset_id']=='812345678901234'
    assert record['has_audio']
    assert 1.5 < record['final_duration'] < 1.8
    assert 'discord_user_id' not in record
    assert client.get('/api/jobs/'+job_id+'/audio').status_code==200
    assert b'roblox-fake-test-key-0000000000' not in (data/'rbxwave.sqlite3').read_bytes()
    assert client.delete('/api/jobs/'+job_id,headers={'X-CSRF-Token':csrf}).status_code==200
    assert not (data/'exports'/f'{job_id}.mp3').exists()
    assert events == ['upload','poll']


def test_safety_url_and_user_segregation(client_and_db,monkeypatch):
    client, data=client_and_db
    csrf=login(client,monkeypatch)
    bad=client.post('/api/jobs',data={'title':'URL teste','creator_id':'999888777','creator_type':'user','rights_confirmed':'true','source_url':'https://youtube.com.evil.test/steal'},headers={'X-CSRF-Token':csrf})
    assert bad.status_code==400
    with m.db.connect() as con:
        con.execute("INSERT INTO jobs (id,discord_user_id,title,source_type,source_reference,creator_id,creator_type,status,stage) VALUES (?,?,?,?,?,?,?,?,?)",
                    ('a'*32,'someone_else','segredo','Arquivo local','segredo.wav','1001','user','concluido','ok'))
    assert all(x['title']!='segredo' for x in client.get('/api/jobs').json()['jobs'])
    assert client.get('/api/jobs/'+'a'*32+'/audio').status_code==404
