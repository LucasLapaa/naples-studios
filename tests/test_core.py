import json
from pathlib import Path
import subprocess
import pytest
import httpx
from app.core import (
    ProcessingError, valid_title, valid_creator_id, platform_from_url,
    convert_2_5x, create_roblox_asset, poll_roblox_asset,
)


def test_validation():
    assert valid_creator_id('1234567') == '1234567'
    assert valid_title('Minhas batidas') == 'Minhas batidas'
    assert platform_from_url('https://www.youtube.com/watch?v=abc') == 'YouTube'
    assert platform_from_url('https://soundcloud.com/artista/faixa') == 'SoundCloud'
    for bad in ['http://youtube.com/watch?v=x', 'https://127.0.0.1/foo', 'https://soundcloud.com.evil.test/a', 'file:///etc/passwd']:
        with pytest.raises(ProcessingError):
            platform_from_url(bad)
    with pytest.raises(ProcessingError): valid_creator_id('abc')


def test_ffmpeg_speed_pitch(tmp_path):
    """Sinal 440 Hz deve ficar 2.5x mais rápido e mais agudo na exportação."""
    source = tmp_path / 'original.wav'
    dest = tmp_path / 'resultado.mp3'
    subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-f','lavfi','-i','sine=frequency=440:duration=5','-y',str(source)],check=True)
    data = convert_2_5x(source, dest)
    assert dest.exists()
    assert 1.9 < data['final_duration'] < 2.15
    assert data['size_bytes'] < 20_000_000


def test_roblox_upload_and_poll(tmp_path):
    audio = tmp_path / 'teste.mp3'
    audio.write_bytes(b'test audio bytes for mock')
    calls=[]
    def handler(req):
        calls.append(req)
        assert req.headers['x-api-key'] == 'segredo-chavexxxxxxxxxxxxxxx'
        if req.url.path.endswith('/v1/assets'):
            assert req.method=='POST'
            data=req.content.decode('utf8',errors='replace')
            assert 'fileContent' in data
            assert 'Audio' in data
            assert 'Teste autorizado' in data
            assert '7654321' in data
            return httpx.Response(200,json={'path':'operations/abc-123'})
        assert req.method=='GET' and req.url.path.endswith('/v1/operations/abc-123')
        return httpx.Response(200,json={'done':True,'response':{'path':'assets/123456789987654'}})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        operation=create_roblox_asset(audio,'Teste autorizado','7654321','group','segredo-chavexxxxxxxxxxxxxxx',client)
        assert operation == 'operations/abc-123'
        result=poll_roblox_asset(operation,'segredo-chavexxxxxxxxxxxxxxx',client,tries=1,interval=0)
        assert result=='123456789987654'
    assert len(calls)==2


def test_yt_dlp_uses_safe_process_options(monkeypatch, tmp_path):
    from app import core
    seen=[]
    def fake_run(cmd, timeout):
        seen.append((cmd,timeout))
        assert cmd[0]=='yt-dlp'
        assert '--no-playlist' in cmd
        assert '--ignore-config' in cmd
        assert '--js-runtimes' in cmd
        assert cmd[cmd.index('--js-runtimes')+1]=='node'
        assert '--' in cmd
        (tmp_path/'fonte.opus').write_bytes(b'allowed fake audio')
        return ''
    monkeypatch.setattr(core,'run',fake_run)
    result=core.download_platform('https://www.youtube.com/watch?v=allowed',tmp_path)
    assert result == tmp_path/'fonte.opus'
    assert len(seen)==1
