@echo off
setlocal
cd /d "%~dp0"
title Naples Studios - Web Setup
where py >nul 2>nul
if errorlevel 1 (
  echo ERRO: Python nao encontrado. Instale Python 3.11+ marcando Add to PATH.
  pause
  exit /b 1
)
if not exist .venv (
  echo Preparando ambiente Python...
  py -3 -m venv .venv
)
call .venv\Scripts\activate.bat
python -m pip install -r requirements.txt
if errorlevel 1 ( echo Erro ao instalar dependencias. & pause & exit /b 1 )
where ffmpeg >nul 2>nul
if errorlevel 1 echo AVISO: FFmpeg nao encontrado. Instale para processar audio.
where ffprobe >nul 2>nul
if errorlevel 1 echo AVISO: FFprobe nao encontrado. FFmpeg completo necessario.
where node >nul 2>nul
if errorlevel 1 echo AVISO: Node.js 22+ recomendado para links do YouTube.
echo.
echo =====================================================
echo  NAPLES STUDIOS  -  CONFIGURACAO 100%% PELO NAVEGADOR
 echo =====================================================
echo  Abra: http://127.0.0.1:8000/configuracoes
 echo  Depois: http://127.0.0.1:8000/
echo  O primeiro acesso nao exige nenhum .env
 echo  Nao feche esta janela enquanto usar o painel.
echo =====================================================
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
pause
