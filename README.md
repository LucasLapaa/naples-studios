# Deploy GitHub + Vercel + Railway

**Leia primeiro:** [DEPLOY_GITHUB_VERCEL_RAILWAY.md](DEPLOY_GITHUB_VERCEL_RAILWAY.md).

---

# NAPLES STUDIOS — v3.0 WEB CONFIG

Ferramenta interna/local para edição de áudio (2.5×) e criação de assets Roblox Open Cloud, com autenticação OAuth2 do Discord e **configuração inicial e manutenção inteiramente via navegador**. Frontend independente em HTML/CSS/JS; backend Python/FastAPI; banco SQLite; `yt-dlp` + FFmpeg.

## Para iniciar

**Windows**: execute `run_windows.bat` depois de instalar Python, FFmpeg/FFprobe e Node.js 22+. Acesse **http://127.0.0.1:8000/configuracoes**.

**Linux/macOS**:

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
```

**Docker**: `docker compose up --build` e acesse **http://127.0.0.1:8000/configuracoes**. No Docker ou via proxy, o bootstrap inicial exige um código temporário mostrado no log do servidor. Ele não é uma credencial Discord/Roblox.

Não há `.env` obrigatório. O único ajuste opcional externo é `NAPLES_DATA_DIR` para mover a pasta de dados do servidor. Configurações antigas de ambiente ainda podem ser lidas como fallback durante migração, mas as configurações feitas pelo navegador têm prioridade.

## Guia em 3 etapas

1. Primeira abertura: configure a **senha administrativa local** (mínimo 12 caracteres), ou digite o código de ativação que o servidor imprimir ao usar Docker/proxy. A senha tem PBKDF2-HMAC-SHA256 com salt aleatório e não é armazenada em claro.
2. No formulário, informe URL base, Discord Client ID, Discord Client Secret, IDs de usuários Discord autorizados, Roblox API Key, criador Roblox e permissão de links. O sistema cria automaticamente `SESSION_SECRET` e salva os outros segredos criptografados (Fernet). Nenhum token é mostrado no HTML.
3. Cadastre no Discord Developer Portal a URL de retorno gerada no painel (`/auth/discord/callback`), salve e entre com Discord no painel principal.

Para alterar as opções, use **Configurações / Roblox Key** na barra lateral (exige a senha administrativa). Existe opção de trocar a senha. As sessões de administrador expiram após 2 horas, e as de Discord após 12 horas. Mutação exige token CSRF.

## Principais recursos

- Discord OAuth2 `identify` com `state` de uso único, sessão via cookie HttpOnly e allowlist de IDs. Operadores só veem seu próprio histórico.
- Importação via `yt-dlp` para YouTube e SoundCloud ou arquivo local (links validados e `--no-playlist`, sem cookies/login).
- FFmpeg acelera **tempo e tom** com `aresample=44100,asetrate=110250,aresample=44100`, MP3 estéreo a 160 kbps e até 7 min/20 MB no resultado.
- Roblox Open Cloud POST `/assets/v1/assets` com `x-api-key` em backend e consulta GET `/assets/v1/operations/{id}`. A operação assíncrona pode retornar ID ou ficar pendente.
- Histórico SQLite, status autoatualizado, cópia do ID, download do MP3, consulta de operações pendentes e exportação CSV.

## Dados e segurança

Todas as configurações persistentes ficam em `data/rbxwave.sqlite3`, criptografadas com chave Fernet da pasta `data/.naples-master.key` (permissão 0600 em sistemas POSIX). A chave e o banco devem ser guardados **juntos em backups privados**. Não publique `data/` ou nenhum segredo no Git.

A primeira configuração direta em localhost dispensa PIN. Se o servidor for acessado por rede/proxy ou Docker, exige um código temporário de bootstrap (gerado automaticamente, mostrado no log, invalidado após o setup). Password login tem limitação de tentativas por IP por processo; o projeto local deve usar um único worker e TLS/proxy reverso confiável antes de qualquer expansão.

A integração com Discord é autenticação do operador; **não** concede permissões Roblox. A API Key precisa ser válida e ter permissão sobre Assets e o usuário/grupo de destino. A plataforma pode moderar/rejeitar/cobrar limites de publicação. Não há garantias de aceitação.

### Credenciais / URLs externas usadas
- Discord OAuth: https://discord.com/developers/applications
- Roblox Open Cloud: https://create.roblox.com/docs/cloud/guides/usage-assets
- Roblox Creator Hub: https://create.roblox.com/dashboard/credentials

## Testes

```sh
python -m pytest -q tests
```

Os testes verificam configuração inicial pelo navegador, login administrativo, alteração criptografada, proteção CSRF e Origin, persistência e alteração de senhas, OAuth2 Discord simulado, upload/poll Roblox simulado, FFmpeg real e isolamento de usuários. **Integrações reais com Discord, Roblox e links externos exigem suas credenciais e validação manual.**
