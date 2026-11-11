# Naples Studios - atualização controlada das dependências (novembro/2026)
# Node 26 LTS e Python 3.14 em Debian trixie (FFmpeg do repositório estável).
FROM node:26-trixie-slim AS node_runtime
FROM python:3.14-slim-trixie

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app

# Node é necessário para executar o EJS do yt-dlp.
COPY --from=node_runtime /usr/local/bin/node /usr/local/bin/node

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg ca-certificates libstdc++6 libatomic1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./requirements.txt
RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir -r requirements.txt \
    && python -m pip check \
    && python -c "from importlib.metadata import version; print('yt-dlp:', version('yt-dlp')); print('yt-dlp-ejs:', version('yt-dlp-ejs'))" \
    && node --version \
    && yt-dlp --version \
    && ffmpeg -hide_banner -version | head -n 1

COPY app ./app
# Falhe no BUILD, e não só após iniciar o Railway, se um módulo do app faltar.
RUN mkdir -p /app/data/exports /app/data/tmp \
    && python -c "from app.main import app; print('Naples Studios: import do backend OK')"

EXPOSE 8000
# O servidor permanece atrás do HTTPS gerenciado pelo Railway/Vercel.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--proxy-headers", "--forwarded-allow-ips", "*"]
