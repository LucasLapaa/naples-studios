# Naples Studios v3 — Configuração 100% pela web

## Iniciar no Windows

1. Instale **Python 3.11+** (Add Python to PATH), **FFmpeg** (incluindo `ffprobe`) e **Node.js 22+** (ajuda o `yt-dlp` em links do YouTube).
2. Extraia o projeto e execute **`run_windows.bat`**. Ele instala as dependências Python automaticamente.
3. Acesse **http://127.0.0.1:8000/configuracoes**. **NÃO** precisa copiar, editar ou criar `.env`.
4. Na primeira visita, crie a senha administrativa (mínimo 12 caracteres). Depois disso, preencha o formulário de integrações:
   - URL base do site (`http://127.0.0.1:8000` para usar só no computador).
   - Discord Client ID e Client Secret, e seu ID pessoal do Discord para permitir login.
   - Roblox Open Cloud API Key, tipo e ID do criador (usuário ou grupo).
   - Ative/desative importação autorizada de links YouTube e SoundCloud.
5. Copie a **Discord Redirect URI** gerada pelo formulário e cadastre-a em https://discord.com/developers/applications → OAuth2 → Redirects.
6. Clique em **Salvar configurações**. Depois acesse **http://127.0.0.1:8000** → Entrar com o Discord.
7. Informe o áudio, escolha o destino e confirme os direitos de uso. Ao final, copie o Asset ID criado pelo Roblox.

## Iniciar no Docker

Use `docker compose up --build`. Abra http://127.0.0.1:8000/configuracoes.
No Docker, na primeira instalação pode ser solicitado um **código temporário de ativação**: ele aparece no terminal/log do servidor, na linha `Código de ativação`. Esse código comprova que você controla o servidor, mas todos os dados de integração são cadastrados no navegador. Não publique esse código.

## Onde tudo é salvo?

- `data/rbxwave.sqlite3`: dados e configurações criptografadas, sessões e histórico.
- `data/.naples-master.key`: chave de criptografia local do servidor; **não compartilhe**. Sem essa chave, segredos criptografados no banco não podem ser recuperados.
- `data/exports/`: áudios MP3 já processados.

**Faça backup da pasta `data` inteira.** O Client Secret do Discord e a API Key do Roblox nunca retornam nas respostas dos endpoints de configuração; o painel informa apenas se estão cadastrados.

## Precisa saber

- **API Key não é opcional**: o Roblox precisa autorizar Assets Read/Write e a conta/grupo de destino. Uma chave inválida resulta em erro.
- **Uploads são submetidos à moderação/cotas do Roblox.** Um ID novo não garante que o áudio seja liberado para todas as experiências.
- Aceleração de **tempo e tom em 2.5×** pelo FFmpeg permite compensação com **PlaybackSpeed = 0.4** no Roblox.
- Links YouTube/SoundCloud funcionam apenas quando o conteúdo é público, tecnicamente disponível e você tem direitos e autorização da plataforma. Não há bypass de DRM nem uso de cookies de terceiros.
- **Para acesso externo, use HTTPS, proxy reverso e proteção adicional de rede.** O projeto está configurado para localhost (um único worker) e não deve ser exposto diretamente na internet.
