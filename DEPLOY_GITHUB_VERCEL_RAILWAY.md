# Naples Studios — publicação via GitHub + Railway + Vercel

Este pacote é uma adaptação do Naples Studios v3 para o cenário em que o usuário acessa o endereço `*.vercel.app`, mas o processamento real roda em container Docker no Railway com volume persistente. O Vercel atua como **proxy público** para o backend completo — não executa FFmpeg. Sem o servidor Railway e o volume, a aplicação não funciona.

## 0. Arquitetura e requisitos

- GitHub **privado**: repositório do código. O ZIP **não** deve ser enviado inteiro: extraia os arquivos.
- Railway: container FastAPI com Python/FFmpeg/Node.js/yt-dlp; uma instância e um volume persistente montado em `/app/data`.
- Vercel: projeto separado apontando à subpasta `vercel-gateway` do **mesmo repositório**, com rewrite de todas as URLs para o backend Railway. O `vercel.json` contém o **endereço público** do backend, nunca chaves.
- A configuração Discord e Roblox é feita pela interface `/configuracoes` depois da publicação.
- Para uploads **grandes de arquivos locais** e downloads de MP3 grandes, prefira o domínio direto do Railway, pois serviços proxy têm limites próprios. URLs de vídeos suportados criam tarefas assíncronas e são mais adequadas a esta montagem.

## 1. Criar repositório GitHub

1. Acesse https://github.com/new, crie o repositório `naples-studios` e marque **Private**.
2. Extraia este ZIP, abra a pasta `Naples_Studios_Deploy_Vercel_Railway` e envie seu conteúdo, mantendo `app/`, `vercel-gateway/`, `Dockerfile`, `requirements.txt`, etc. na raiz do repositório. O arquivo ZIP não deve ser o único arquivo enviado.
3. Se preferir Git:
   ```bash
   git init
   git add .
   git commit -m "Naples Studios - deploy Railway e Vercel"
   git branch -M main
   git remote add origin https://github.com/SEU_USUARIO/naples-studios.git
   git push -u origin main
   ```
4. Não publique `data/`, `.env` ou qualquer segredo. O `.gitignore` e o `.dockerignore` impedem a inclusão acidental dos dados mais comuns, mas confira `git status` antes de enviar.

## 2. Railway (obrigatório; ANTES da Vercel)

1. Vá para https://railway.com/ e faça login com GitHub.
2. **New Project → Deploy from GitHub Repo**; selecione `naples-studios` e autorize o acesso.
3. Garanta o uso do **Dockerfile na raiz** (builder `Dockerfile`), não Python/Nixpacks. O Dockerfile inclui Python, FFmpeg e Node.js.
4. Em `Variables`, adicione a única variável necessária para persistência explícita:
   - `NAPLES_DATA_DIR` = `/app/data`
   - opcionalmente `PORT` = `8000` se necessário para configurar o roteamento público.
   Não insira a API Key Roblox ou Discord nessa seção; elas serão preenchidas pela web.
5. No serviço, clique em **Add Volume / Volume** e defina **Mount Path: `/app/data`**. SEM ESTE VOLUME o SQLite, a chave de criptografia e o histórico podem desaparecer em redeploy.
6. Configure apenas **uma réplica**, por causa de SQLite e da fila em memória. Em caso de reinício, o usuário pode precisar reenviar uma tarefa interrompida. Operações Roblox já criadas podem ser verificadas no painel.
7. Em **Settings → Networking → Public Networking → Generate Domain**, crie uma URL pública HTTPS, como `naples-x.up.railway.app` (a URL real varia). Configure a porta `8000` se o Railway solicitar.
8. Abra `https://SEU-DOMINIO-RAILWAY/healthz`: deve mostrar `{"ok":true}`. Se não mostrar, confira **Deploy Logs / Runtime Logs**.
9. Em **Runtime Logs**, copie o `Código de ativação` impresso pelo Naples Studios; será solicitado no primeiro acesso remoto. Nunca publique o código em um commit.

## 3. Vercel

1. **Antes de implantar**, no GitHub edite `vercel-gateway/vercel.json` e substitua `https://TROQUE-PELO-SEU-DOMINIO.up.railway.app` pela URL do Railway, sem barra final (mantenha `/:path*` após a barra).
2. Abra https://vercel.com/new e conecte o repositório GitHub `naples-studios`.
3. Em **Root Directory**, selecione `vercel-gateway` (não a raiz). Em **Framework Preset**, selecione `Other`; o diretório de saída padrão pode ser `public`, sem Build Command.
4. Clique em **Deploy**. A Vercel disponibilizará um endereço `https://SEU-PROJETO.vercel.app`.
5. Acesse `https://SEU-PROJETO.vercel.app/healthz` e depois `/configuracoes`. O site deve encaminhar essas requisições ao Railway sem alterar a URL no navegador.
6. Se a tela de configuração mostrar 403 no primeiro cadastro, confirme uso de **HTTPS** e que `uvicorn` está com proxy headers (já configurado neste Dockerfile); use o código dos logs.

## 4. Instalação inicial 100% pela web

1. Abra `https://SEU-PROJETO.vercel.app/configuracoes`.
2. Informe o **Código de ativação** dos logs Railway e crie uma senha administrativa de 12+ caracteres.
3. Faça login no painel de configurações e preencha:
   - **URL base**: `https://SEU-PROJETO.vercel.app` (não a URL do Railway; não adicionar `/` no final).
   - **Cookies seguros**: **Ativado**.
   - **Discord Client ID / Client Secret**: obtidos em https://discord.com/developers/applications.
   - **Discord Allowed User IDs**: seu ID Discord (modo desenvolvedor → copiar ID).
   - **Roblox API Key**: criada em https://create.roblox.com/dashboard/credentials com permissões de Assets Read e Write e acesso ao criador correspondente.
   - **Roblox Creator ID** e **Creator Type** (`user` ou `group`).
   - **Ativar links**: Sim, se quiser importar vídeos de plataformas quando permitido.
4. Salve. A interface exibirá a URI de retorno do Discord, que deverá ser exatamente:
   `https://SEU-PROJETO.vercel.app/auth/discord/callback`
5. No Discord Developer Portal → aplicativo → OAuth2 → Redirects, adicione essa URL **exata** e salve.
6. Volte ao Naples Studios em `https://SEU-PROJETO.vercel.app` → **Entrar com Discord**.

## 5. Roblox: atenção ao acesso

- API Key precisa ser criada pelo **titular autorizado** do usuário ou grupo. Um ID digitado não dá permissão por si só.
- Na configuração da chave, se for usar uma lista de IPs, ela precisa conter os IPs de saída **do Railway**, não o IP da Vercel. IP estático de saída é recurso de plano elegível (Railway Pro). Como alternativa, pode desativar a restrição de IP da chave durante o teste, limitando cuidadosamente suas permissões. Nunca publique a chave.
- A API pode devolver uma operação pendente antes do Asset ID; use **Verificar** e aguarde a moderação. Os áudios do Roblox têm limites oficiais de formato, tamanho, duração e cota.

## 6. Teste ponta a ponta

1. `...vercel.app/healthz` deve responder `{"ok":true}`.
2. `...vercel.app/configuracoes`: login admin e leitura de configurações devem funcionar.
3. `...vercel.app`: Discord login deve retornar ao domínio Vercel (sem ir para um domínio `railway.app`).
4. Faça um teste com áudio de domínio público **curto e autorizado** via link; espere concluir, copie ID.
5. Teste o som no Roblox Studio com `SoundId = "rbxassetid://SEU_ASSET_ID"` e `PlaybackSpeed = 0.4`. Acesso ao áudio depende das permissões e moderação do Roblox.
6. Faça um teste de **arquivo local** e download de MP3. Se o proxy Vercel impedir arquivos grandes, use diretamente o endereço Railway para esse fluxo, respeitando as configurações de sessão/callback (ou faça uma arquitetura com frontend/API no mesmo domínio por subdomínios e upload direto). Não é possível garantir grandes uploads pelo proxy sem testar na conta implantada.
7. Reinicie o serviço Railway e confirme se o histórico permanece. Se desaparecer, o volume não foi montado corretamente.

## 7. Limitações reais antes de produção para múltiplos usuários

- Tarefas são executadas **em memória no processo web** com fila de 2. Para alta disponibilidade, migrar para uma fila durável (Redis/RQ/Celery) e workers dedicados.
- **SQLite** no volume único é adequado a esta configuração de uma instância. Para múltiplos servidores, migrar para PostgreSQL.
- **Vercel** como proxy não executa a conversão e possui timeout de proxy. Alguns arquivos grandes também podem exigir upload/download direto ao backend e um domínio próprio.
- Importação via `yt-dlp` pode falhar se o site exigir autenticação, bloqueio anti-automação ou restringir acesso. Não há garantias de funcionar com todo link.
- Roblox pode recusar arquivos por direitos, moderação, limites e cotas. Aprovação não pode ser garantida.
- O botão de configuração não cria as credenciais no Discord nem no Roblox; você cria as credenciais nas plataformas e cola os valores no painel.

## Links oficiais

- Vercel rewrites: https://vercel.com/docs/routing/rewrites
- Vercel deployment Git: https://vercel.com/docs/git
- Railway GitHub: https://docs.railway.com/quick-start
- Railway volumes: https://docs.railway.com/volumes
- Roblox Open Cloud Assets: https://create.roblox.com/docs/cloud/guides/usage-assets
