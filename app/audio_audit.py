"""Auditoria TÉCNICA de áudio. Não prevê direitos autorais ou aprovação Roblox.

Somente trabalha com arquivos já gerados pelo Naples Studios; nunca aceita URLs,
paths arbitrários, cookies ou chaves do frontend.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

class AudioAuditError(Exception):
    pass


def _execute(command: list[str], timeout: int = 45) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise AudioAuditError('FFmpeg/FFprobe indisponível ou tempo limite excedido.') from exc


def audit_audio(path: Path) -> dict:
    path = Path(path)
    if not path.is_file() or path.stat().st_size == 0:
        raise AudioAuditError('Arquivo de áudio não encontrado.')
    if path.stat().st_size >= 20 * 1024 * 1024:
        raise AudioAuditError('O arquivo excede o limite de tamanho do Roblox.')
    info = _execute([
        'ffprobe','-v','error','-select_streams','a:0',
        '-show_entries','stream=codec_name,sample_rate,channels,bit_rate,duration:format=duration,bit_rate,size',
        '-of','json',str(path)], 20)
    if info.returncode:
        raise AudioAuditError('Não foi possível ler a estrutura de áudio do arquivo.')
    try:
        payload = json.loads(info.stdout)
        stream = payload['streams'][0]
        fmt = payload['format']
        codec = str(stream.get('codec_name') or '')
        sample_rate = int(stream.get('sample_rate') or 0)
        channels = int(stream.get('channels') or 0)
        duration = float(fmt.get('duration') or stream.get('duration') or 0)
        bitrate = int(stream.get('bit_rate') or fmt.get('bit_rate') or 0)
    except (KeyError, IndexError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise AudioAuditError('Os metadados técnicos do áudio estão inválidos.') from exc
    alerts = []
    if codec not in ('mp3','vorbis','pcm_s16le','flac'):
        alerts.append('Codec fora dos formatos usados por esta integração.')
    if sample_rate > 48000 or sample_rate <= 0:
        alerts.append('Taxa de amostragem fora dos requisitos do Roblox (máx. 48 kHz).')
    if channels not in (1,2):
        alerts.append('A integração Naples usa canais mono ou estéreo.')
    if not 0 < duration < 420:
        alerts.append('Duração fora do limite de menos de 7 minutos.')
    # volumedetect mede nível, não determina se o áudio é original/possui licença.
    peak = None
    mean = None
    scan = _execute([
        'ffmpeg','-nostdin','-hide_banner','-i',str(path),
        '-map','0:a:0','-vn','-af','volumedetect','-f','null','-'], 90)
    if scan.returncode:
        alerts.append('Não foi possível decodificar o áudio integralmente.')
    else:
        for key, out_name in (('max_volume','peak'), ('mean_volume','mean')):
            match = re.search(rf'{key}:\s*(-?\d+(?:\.\d+)?)\s*dB', scan.stderr)
            if match:
                if out_name == 'peak': peak = float(match.group(1))
                else: mean = float(match.group(1))
        if peak is not None and peak >= -0.1:
            alerts.append('Pico próximo de 0 dBFS; confira possíveis distorções ouvindo o MP3.')
    return {
        'codec': codec, 'sample_rate_hz': sample_rate, 'channels': channels,
        'duration_seconds': round(duration,2), 'bitrate_kbps': round(bitrate/1000) if bitrate else None,
        'size_mb': round(path.stat().st_size/1048576,3),
        'peak_dbfs': peak, 'mean_dbfs': mean,
        'technically_valid': not any('fora' in x or 'decodificar' in x for x in alerts),
        'warnings': alerts,
        'note': 'Auditoria técnica: não é uma garantia de aprovação, direitos autorais ou reprodução no Roblox.'
    }
