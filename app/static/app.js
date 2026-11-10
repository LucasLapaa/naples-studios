'use strict';
const $ = id => document.getElementById(id);
const liveStatuses = new Set(['fila','processando','enviando','confirmando']);
const activeStatuses = new Set([...liveStatuses,'pendente']);
const statusLabels = {fila:'Na fila',processando:'Processando',enviando:'Enviando',confirmando:'Confirmando',pendente:'Pendente',concluido:'ID gerado',interrompido:'Interrompido',erro:'Falhou'};
const safe = v => String(v ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
let csrf = '';
let currentSource = 'url';
let authorized = false;
let busy = false;
let jobs = [];
let lastJobIds = new Set();
let toastTimer;
let previewObjectUrl = null;
function clearMediaPreview(){
  $('previewCard').hidden=true;
  $('previewPlayer').pause();
  $('previewPlayer').removeAttribute('src');
  $('previewPlayer').load();
  $('previewPlayer').hidden=true;
  if(previewObjectUrl){URL.revokeObjectURL(previewObjectUrl);previewObjectUrl=null;}
}
function showMediaPreview(title, details){
  $('previewTitle').textContent=title;
  $('previewDetails').textContent=details;
  $('previewCard').hidden=false;
}

function toast(message, error=false){
  clearTimeout(toastTimer);
  const node=$('toast');node.textContent=message;node.classList.toggle('error',error);node.classList.add('show');
  toastTimer=setTimeout(()=>node.classList.remove('show'),4700);
}
async function api(path, options={}){
  const opts={credentials:'same-origin',...options};
  if(opts.method && !['GET','HEAD'].includes(opts.method.toUpperCase())){
    opts.headers={'X-CSRF-Token':csrf,...opts.headers};
  }
  const res=await fetch(path,opts);
  const type=res.headers.get('content-type')||'';
  const body=type.includes('application/json') ? await res.json() : null;
  if(!res.ok){
    if(res.status===401){authorized=false;showLogin();}
    throw new Error(typeof body?.detail==='string'?body.detail:`Não foi possível concluir (HTTP ${res.status}).`);
  }
  return body;
}
function showLogin(){
  $('appView').hidden=true;
  $('loginView').hidden=false;
  const msg=new URLSearchParams(location.search).get('login_error');
  const texts={cancelado:'Login cancelado. Autorize o acesso no Discord para continuar.',discord:'Falha ao validar a autenticação no Discord. Tente novamente.',acesso:'Sua conta Discord não está autorizada. Solicite acesso ao administrador.',config:'O Discord OAuth2 ainda não foi configurado no servidor. O proprietário precisa cadastrar a aplicação uma única vez.'};
  $('loginError').textContent=texts[msg]||'';
  $('loginError').hidden=!texts[msg];
}
function showApp(profile, config){
  authorized=true;
  $('loginView').hidden=true;
  $('appView').hidden=false;
  $('userName').textContent=profile.username;
  const avatar=$('userAvatar'); avatar.replaceChildren();
  if(profile.avatar_url && /^https:\/\/cdn\.discordapp\.com\/avatars\/\d+\/[\w]+\.png\?size=128$/.test(profile.avatar_url)){
    const image=document.createElement('img');image.src=profile.avatar_url;image.alt='Avatar de '+profile.username;
    image.referrerPolicy='no-referrer';image.onerror=()=>{avatar.textContent=(profile.username||'N').charAt(0).toUpperCase();};
    avatar.appendChild(image);
  }else avatar.textContent=(profile.username||'N').charAt(0).toUpperCase();
  const configuredTarget = config.roblox_connected ? `${config.creator_type==='group'?'Grupo':'Usuário'} Roblox #${config.creator_id}` : 'Roblox não conectado — cole sua Key em Roblox Key';
  $('uploadTargetNotice').textContent=`Destino automático: ${configuredTarget}. Altere somente em Roblox Key.`;
  $('batchTargetNotice').textContent=`Destino automático do lote: ${configuredTarget}. Altere somente em Roblox Key.`;
  if(!config.platform_links){
    $('urlHint').textContent='Links estão desativados nas configurações web. Use arquivo local ou habilite a importação no painel de administração.';
    switchSource('file');
    document.querySelector('[data-source="url"]').disabled=true;
    document.querySelector('[data-batch-source="links"]').disabled=true;
    switchBatchSource('files');
  }
}
function switchSource(type){
  currentSource=type;
  clearMediaPreview();
  $('urlSource').hidden=type!=='url';
  $('fileSource').hidden=type!=='file';
  document.querySelectorAll('.source-option').forEach(btn=>btn.classList.toggle('active',btn.dataset.source===type));
}
document.querySelectorAll('.source-option').forEach(btn=>btn.addEventListener('click',()=>{if(!btn.disabled)switchSource(btn.dataset.source);}));
$('audioFile').addEventListener('change',()=>{
  const file=$('audioFile').files[0];
  $('fileLabel').textContent=file?`${file.name} · ${(file.size/1048576).toFixed(2)} MB`:'MP3, WAV, OGG, FLAC, M4A, WEBM · Até 50 MB';
  clearMediaPreview();
  if(file){
    if(!$('title').value.trim()) $('title').value=file.name.replace(/\.[^.]+$/,'').slice(0,80);
    previewObjectUrl=URL.createObjectURL(file);
    $('previewPlayer').src=previewObjectUrl;
    $('previewPlayer').hidden=false;
    showMediaPreview(file.name, `Arquivo local · ${(file.size/1048576).toFixed(2)} MB · áudio pode ser reproduzido abaixo`);
  }
});
$('sourceUrl').addEventListener('input',clearMediaPreview);
$('previewBtn').addEventListener('click',async()=>{
  const url=$('sourceUrl').value.trim();
  if(!url){toast('Cole um link antes de solicitar a prévia.',true);return;}
  const btn=$('previewBtn');btn.disabled=true;btn.textContent='CONSULTANDO PLATAFORMA...';
  clearMediaPreview();
  try{
    const meta=await api('/api/media/preview',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url})});
    const length=meta.duration?` · ${fmtDuration(meta.duration)}`:'';
    showMediaPreview(meta.title,`${meta.platform}${length}${meta.uploader?' · '+meta.uploader:''}`);
    if(!$('title').value.trim()) $('title').value=meta.title.slice(0,80);
  }catch(e){toast(e.message,true);}
  finally{btn.disabled=false;btn.textContent='♫ Ver prévia do link';}
});
const drop=$('dropZone');
for(const event of ['dragover','dragenter'])drop.addEventListener(event,e=>{e.preventDefault();drop.classList.add('dragging');});
for(const event of ['drop','dragleave'])drop.addEventListener(event,e=>{e.preventDefault();drop.classList.remove('dragging');});
drop.addEventListener('drop',e=>{
  if(e.dataTransfer?.files?.length){$('audioFile').files=e.dataTransfer.files;$('audioFile').dispatchEvent(new Event('change'));}
});
$('logoutBtn').addEventListener('click',async()=>{
  try{await api('/api/logout',{method:'POST'});csrf='';authorized=false;showLogin();history.replaceState(null,'','/');}
  catch(e){toast(e.message,true);}
});
$('uploadForm').addEventListener('submit',async e=>{
  e.preventDefault();
  const form=new FormData();
  const title=$('title').value.trim();
  if(!$('rights').checked){toast('Confirme a autorização do áudio.',true);return;}
  form.set('title',title);
  form.set('rights_confirmed','true');
  if(currentSource==='url'){
    if(!$('sourceUrl').value.trim()){toast('Cole um link de música.',true);return;}
    form.set('source_url',$('sourceUrl').value.trim());
  }else{
    const file=$('audioFile').files[0];
    if(!file){toast('Selecione um arquivo de áudio.',true);return;}
    if(file.size>50*1024*1024){toast('Arquivo deve ter no máximo 50 MB.',true);return;}
    form.set('audio_file',file);
  }
  const btn=$('submitBtn');btn.disabled=true;btn.querySelector('span').textContent='CRIANDO TAREFA...';
  try{
    await api('/api/jobs',{method:'POST',body:form});
    toast('Áudio enviado para a fila. O progresso aparecerá no histórico.');
    $('title').value='';$('sourceUrl').value='';$('audioFile').value='';
    $('fileLabel').textContent='MP3, WAV, OGG, FLAC, M4A, WEBM · Até 50 MB';
    $('rights').checked=false;
    await fetchJobs();
    $('history').scrollIntoView({behavior:'smooth',block:'start'});
  }catch(e){toast(e.message,true);}
  finally{btn.disabled=false;btn.querySelector('span').textContent='PROCESSAR E PUBLICAR';}
});
function prettyDate(text){
  if(!text)return '—';
  const iso=String(text).trim().replace(' ','T');
  const normalized=/[Zz]$|[+-]\d{2}:\d{2}$/.test(iso)?iso:iso+'Z';
  const value=new Date(normalized);
  if(Number.isNaN(+value))return String(text);
  return new Intl.DateTimeFormat('pt-BR',{dateStyle:'short',timeStyle:'short'}).format(value);
}
function fmtDuration(value){
  if(value==null)return '';
  const n=Math.round(Number(value));return `${Math.floor(n/60)}:${String(n%60).padStart(2,'0')}`;
}
function actions(j){
  const arr=[];
  if(j.error)arr.push(`<button type="button" class="mini-btn" data-action="error" data-id="${safe(j.id)}">Ver motivo</button>`);
  if(j.asset_id)arr.push(`<button type="button" class="mini-btn" data-action="copy" data-id="${safe(j.id)}">Copiar ID</button>`);
  if(j.has_audio)arr.push(`<button type="button" class="mini-btn" data-action="audio" data-id="${safe(j.id)}">Baixar MP3</button>`);
  if(j.operation && ['erro','pendente','interrompido'].includes(j.status))arr.push(`<button type="button" class="mini-btn" data-action="verify" data-id="${safe(j.id)}">Verificar</button>`);
  if(!liveStatuses.has(j.status))arr.push(`<button type="button" class="mini-btn danger" data-action="delete" data-id="${safe(j.id)}" title="Excluir registro local">×</button>`);
  return arr.join('');
}
function renderJobs(){
  const text=$('searchJobs').value.trim().toLowerCase();
  const filter=$('filterJobs').value;
  const selected=jobs.filter(j=>{
    const hay=[j.title,j.asset_id,j.source_reference,j.creator_id].join(' ').toLowerCase();
    return (!text||hay.includes(text))&&(filter==='all'||(filter==='active'?activeStatuses.has(j.status):filter===j.status));
  });
  $('statTotal').textContent=jobs.length;
  $('statDone').textContent=jobs.filter(j=>j.status==='concluido').length;
  $('statActive').textContent=jobs.filter(j=>activeStatuses.has(j.status)).length;
  $('tableCount').textContent=`${selected.length} ${selected.length===1?'REGISTRO':'REGISTROS'}`;
  $('jobsTable').innerHTML=selected.map(j=>{
    const status= j.status==='concluido'?'success':(j.status==='erro'||j.status==='interrompido')?'error':j.status==='pendente'?'pending':'active';
    const detail=[j.source_type,j.final_duration!=null?`${fmtDuration(j.final_duration)} (2.5x)`:null].filter(Boolean).join(' · ');
    const error=j.error?`<small class="job-error" title="${safe(j.error)}">${safe(j.error)}</small>`:'';
    const stage=j.stage?`<small title="${safe(j.stage)}">${safe(j.stage)}</small>`:'';
    const id=j.asset_id?`<span class="asset-id">${safe(j.asset_id)}</span>`:'<small>Aguardando...</small>';
    return `<tr><td><strong title="${safe(j.title)}">${safe(j.title)}</strong><small>${safe(detail)}</small>${error}</td><td>${safe(j.creator_type==='group'?'Grupo':'Usuário')}<small>#${safe(j.creator_id)}</small></td><td><span class="status ${status}">${safe(statusLabels[j.status]||j.status)}</span>${stage}</td><td>${id}</td><td>${safe(prettyDate(j.created_at))}</td><td><div class="table-actions">${actions(j)}</div></td></tr>`;
  }).join('') || '<tr><td colspan="6" class="empty">Nenhuma música encontrada. Crie sua primeira publicação acima.</td></tr>';
}
async function fetchJobs(){
  if(!authorized||busy)return;
  busy=true;
  try{
    const resp=await api('/api/jobs');
    jobs=resp.jobs||[];
    for(const j of jobs){
      if(j.status==='concluido' && j.asset_id && lastJobIds.size && !lastJobIds.has(j.id))toast(`ID do Roblox gerado: ${j.asset_id}`);
    }
    lastJobIds=new Set(jobs.filter(j=>j.status==='concluido').map(j=>j.id));
    renderJobs();
    renderBatchProgress();
    renderMonitor();
    renderCollections();
  }catch(e){if(authorized)toast(e.message,true);}
  finally{busy=false;}
}
$('searchJobs').addEventListener('input',renderJobs);
$('filterJobs').addEventListener('change',renderJobs);
$('refreshBtn').addEventListener('click',()=>fetchJobs());
$('jobsTable').addEventListener('click',async e=>{
  const button=e.target.closest('button[data-action]');if(!button)return;
  const j=jobs.find(x=>x.id===button.dataset.id);if(!j)return;
  button.disabled=true;
  try{
    const action=button.dataset.action;
    if(action==='error'){
      $('errorDialogTitle').textContent='Motivo da falha · '+j.title;
      $('errorDialogText').textContent=j.error||'Erro não informado.';
      $('errorDialog').showModal();
    }else if(action==='copy'){
      await navigator.clipboard.writeText(j.asset_id);
      toast('Asset ID '+j.asset_id+' copiado.');
    }else if(action==='audio'){
      window.location.assign(`/api/jobs/${encodeURIComponent(j.id)}/audio`);
    }else if(action==='delete'){
      if(!confirm('Excluir registro e MP3 local de "'+j.title+'"? Isso NÃO exclui o asset já publicado no Roblox.'))return;
      await api(`/api/jobs/${j.id}`,{method:'DELETE'});
      toast('Registro local excluído.');await fetchJobs();
    }else if(action==='verify'){
      const resp=await api(`/api/jobs/${j.id}/verify`,{method:'POST'});
      toast(resp.asset_id?`Asset ID confirmado: ${resp.asset_id}`:'O Roblox ainda está processando este envio.');
      await fetchJobs();
    }
  }catch(e){toast(e.message,true);}
  finally{button.disabled=false;}
});
$('errorDialogClose').addEventListener('click',()=> $('errorDialog').close());
$('errorDialog').addEventListener('click',e=>{if(e.target===$('errorDialog'))$('errorDialog').close();});
$('exportBtn').addEventListener('click',()=>{
  if(!jobs.length){toast('Ainda não existem registros para exportar.');return;}
  const cols=['title','creator_type','creator_id','status','asset_id','operation','created_at','source_type','original_duration','final_duration','error'];
  const quote=v=>'"'+String(v??'').replaceAll('"','""')+'"';
  const csv=[cols.join(';'),...jobs.map(j=>cols.map(c=>quote(j[c])).join(';'))].join('\r\n');
  const blob=new Blob(['\ufeff',csv],{type:'text/csv;charset=utf-8'});
  const url=URL.createObjectURL(blob);
  const a=document.createElement('a');a.href=url;a.download='naples-studios-assets.csv';document.body.appendChild(a);a.click();a.remove();
  setTimeout(()=>URL.revokeObjectURL(url),1000);
});
document.querySelectorAll('.nav-item').forEach(link=>link.addEventListener('click',()=>{
  document.querySelectorAll('.nav-item').forEach(x=>x.classList.remove('active'));
  link.classList.add('active');
}));
async function init(){
  try{
    const result=await api('/api/session');
    csrf=result.csrf;
    showApp(result.user,result.config);
    await fetchJobs();
    await Promise.all([fetchCollections(),loadConnection()]);
    if(!result.config.roblox_connected && !sessionStorage.getItem('naples-key-tip')){sessionStorage.setItem('naples-key-tip','1');location.hash='#roblox-key';toast('Bem-vindo! Conecte sua Roblox Key para começar a publicar.');}
  }catch(e){showLogin();}
  setInterval(()=>{
    if(authorized && jobs.some(j=>activeStatuses.has(j.status)))fetchJobs();
  },4000);
}
init();

// Diagnóstico autenticado das ferramentas instaladas no backend.
$('diagnosticsBtn').addEventListener('click', async()=>{
  try{
    const d = await api('/api/system/diagnostics');
    $('errorDialogTitle').textContent = 'Diagnóstico do servidor Naples Studios';
    $('errorDialogText').textContent =
      `FFmpeg: ${d.ffmpeg}\nFFprobe: ${d.ffprobe}\nyt-dlp: ${d['yt-dlp']}\nNode: ${d.node}\n\n${d.nota}`;
    $('errorDialog').showModal();
  }catch(err){toast(err.message, true);}
});

// Naples Studios v7 — coleções, configuração pública sem segredos e moderação.
let naplesCollections = [];
let naplesCollectionItems = [];
const moderationText = {
  approved:'Aprovado', reviewing:'Em análise', rejected:'Recusado',
  unknown:'Estado não informado', unchecked:'A conferir',
  unavailable:'Consulta indisponível',unauthorized:'Sem acesso',not_found:'Não encontrado'
};
function moderationChip(state){
  const code=['approved','reviewing','rejected'].includes(state)?state:'unchecked';
  return `<span class="v7-chip ${code}">${safe(moderationText[state]||'A conferir')}</span>`;
}
async function loadConnection(){
  try{
    const d=await api('/api/roblox/connection');
    $('robloxConnectionTitle').textContent=d.configured?'Chave configurada':'Configuração pendente';
    $('robloxConnectionDetail').textContent=`Destino padrão: ${d.creator_type==='group'?'Grupo':'Usuário'} Roblox #${d.creator_id||'—'}`;
    $('keyConnectedChip').textContent=d.configured?'CONFIGURADA':'PENDENTE';
    $('disconnectRobloxBtn').hidden=!d.configured;
    if(d.configured){$('uploadTargetNotice').textContent=`Destino automático: ${d.creator_type==='group'?'Grupo':'Usuário'} Roblox #${d.creator_id}`; $('batchTargetNotice').textContent=`Destino automático do lote: ${d.creator_type==='group'?'Grupo':'Usuário'} Roblox #${d.creator_id}`;}
  }catch(e){$('robloxConnectionDetail').textContent='Não foi possível consultar a conexão.';}
}
function renderMonitor(){
  if(!$('monitorList'))return;
  const modJobs=jobs.filter(j=>j.asset_id);
  $('modApproved').textContent=modJobs.filter(j=>j.moderation_state==='approved').length;
  $('modReviewing').textContent=modJobs.filter(j=>j.moderation_state==='reviewing').length;
  $('modRejected').textContent=modJobs.filter(j=>j.moderation_state==='rejected').length;
  $('modUnchecked').textContent=modJobs.filter(j=>!['approved','reviewing','rejected'].includes(j.moderation_state)).length;
  const search=$('searchMonitor').value.trim().toLowerCase(),filter=$('monitorFilter').value;
  const found=modJobs.filter(j=>(!search||`${j.title} ${j.asset_id}`.toLowerCase().includes(search))&&
      (filter==='all'||(filter==='unchecked'?!['approved','reviewing','rejected'].includes(j.moderation_state):j.moderation_state===filter)));
  $('monitorList').innerHTML=found.map(j=>`<div class="v7-line"><div class="v7-line-main"><strong>${safe(j.title)}</strong><small>Asset ID ${safe(j.asset_id)} · ${safe(j.moderation_checked_at?'Verificado em '+prettyDate(j.moderation_checked_at):'Não consultado')}</small><small>${safe(j.moderation_message||'O ID foi criado; consulte a moderação usando o botão Verificar.')}</small></div><div class="v7-line-actions">${moderationChip(j.moderation_state||'unchecked')}<button type="button" class="mini-btn" data-monitor-copy="${safe(j.asset_id)}">Copiar ID</button></div></div>`).join('')||'<p class="v7-muted">Nenhum Asset ID encontrado no filtro atual.</p>';
}
$('searchMonitor').addEventListener('input',renderMonitor);
$('monitorFilter').addEventListener('change',renderMonitor);
$('monitorList').addEventListener('click',async e=>{
  const btn=e.target.closest('[data-monitor-copy]');if(!btn)return;
  try{await navigator.clipboard.writeText(btn.dataset.monitorCopy);toast('Asset ID copiado.');}catch{toast('Não foi possível copiar.',true);}
});
async function checkModeration(ids){
  const result=await api('/api/monitor/check',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({job_ids:ids})});
  await fetchJobs();
  const errors=result.results.filter(r=>!['approved','reviewing','rejected','unknown'].includes(r.state));
  if(errors.length){
    $('errorDialogTitle').textContent='Diagnóstico do monitor Roblox';
    $('errorDialogText').textContent=errors.map(r=>r.asset_id+': '+r.message).join('\n');
    $('errorDialog').showModal();
  }
  toast(`Consulta finalizada para ${result.results.length} asset(s).`);
  return result;
}
$('verifyMonitorBtn').addEventListener('click',async()=>{
  const valid=jobs.filter(j=>j.asset_id).slice(0,20).map(j=>j.id);
  if(!valid.length){toast('Não há Asset IDs para consultar.');return;}
  const btn=$('verifyMonitorBtn');btn.disabled=true;btn.textContent='CONSULTANDO ROBLOX...';
  try{await checkModeration(valid);}catch(e){toast(e.message,true);}finally{btn.disabled=false;btn.textContent='↻ Verificar até 20';}
});
$('testKeyBtn').addEventListener('click',async()=>{
  const valid=jobs.find(j=>j.asset_id);
  if(!valid){$('keyTestFeedback').textContent='Primeiro publique um asset com sua conta; ele será usado para testar Assets › Read.';return;}
  const btn=$('testKeyBtn');btn.disabled=true;
  try{
    const result=await checkModeration([valid.id]);
    $('keyTestFeedback').textContent=result.results[0].message;
  }catch(e){$('keyTestFeedback').textContent=e.message;}finally{btn.disabled=false;}
});
async function fetchCollections(){
  if(!authorized)return;
  try{
    const response=await api('/api/collections');
    naplesCollections=response.collections||[];naplesCollectionItems=response.items||[];
    renderCollections();
  }catch(e){toast(e.message,true);}
}
function renderCollections(){
  const sel=$('collectionSelect'),cur=sel.value;
  sel.replaceChildren();
  for(const c of naplesCollections){const opt=document.createElement('option');opt.value=c.id;opt.textContent=c.name;sel.append(opt);}
  if(naplesCollections.some(c=>c.id===cur))sel.value=cur;
  if(!naplesCollections.length){const opt=document.createElement('option');opt.textContent='Crie uma coleção primeiro';opt.value='';sel.append(opt);}
  const jobSel=$('collectionJobSelect'),prev=jobSel.value;jobSel.replaceChildren();
  for(const job of jobs){const opt=document.createElement('option');opt.value=job.id;opt.textContent=job.title+(job.asset_id?' · '+job.asset_id:'');jobSel.append(opt);}
  if(jobs.some(j=>j.id===prev))jobSel.value=prev;
  if(!jobs.length){const opt=document.createElement('option');opt.value='';opt.textContent='Nenhuma música no histórico';jobSel.append(opt);}
  $('collectionsList').innerHTML=naplesCollections.map(c=>`<div class="v7-line"><div class="v7-line-main"><strong>${safe(c.name)}</strong><small>${c.total} música(s)</small></div><div class="v7-line-actions"><button type="button" class="mini-btn" data-col-select="${safe(c.id)}">Abrir</button><button type="button" class="mini-btn danger" data-col-delete="${safe(c.id)}">Excluir</button></div></div>`).join('')||'<p class="v7-muted">Crie sua primeira coleção para organizar o histórico.</p>';
  renderCollectionItems();
}
function renderCollectionItems(){
  if(!$('selectedCollectionItems'))return;
  const cid=$('collectionSelect').value;
  const ids=new Set(naplesCollectionItems.filter(x=>x.collection_id===cid).map(x=>x.job_id));
  $('selectedCollectionItems').innerHTML=jobs.filter(j=>ids.has(j.id)).map(j=>`<div class="v7-line"><div class="v7-line-main"><strong>${safe(j.title)}</strong><small>${safe(j.asset_id||'Ainda sem Asset ID')}</small></div><button type="button" class="mini-btn danger" data-item-remove="${safe(j.id)}">Remover</button></div>`).join('')||'<p class="v7-muted">Esta coleção ainda está vazia.</p>';
}
$('collectionSelect').addEventListener('change',renderCollectionItems);
$('createCollectionForm').addEventListener('submit',async e=>{
  e.preventDefault();const name=$('newCollectionName').value.trim();if(!name)return;
  try{
    await api('/api/collections',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name})});
    $('newCollectionName').value='';await fetchCollections();toast('Coleção criada.');
  }catch(err){toast(err.message,true);}
});
$('addToCollectionForm').addEventListener('submit',async e=>{
  e.preventDefault();const cid=$('collectionSelect').value,job=$('collectionJobSelect').value;
  if(!cid||!job){toast('Selecione uma coleção e uma música.',true);return;}
  try{await api(`/api/collections/${cid}/items/${job}`,{method:'PUT'});await fetchCollections();toast('Música adicionada à coleção.');}
  catch(err){toast(err.message,true);}
});
$('collectionsList').addEventListener('click',async e=>{
  const choose=e.target.closest('[data-col-select]'),del=e.target.closest('[data-col-delete]');
  if(choose){$('collectionSelect').value=choose.dataset.colSelect;renderCollectionItems();$('selectedCollectionItems').scrollIntoView({behavior:'smooth',block:'nearest'});return;}
  if(del){if(!confirm('Excluir esta coleção? Os assets e músicas não serão excluídos.'))return;
    try{await api(`/api/collections/${del.dataset.colDelete}`,{method:'DELETE'});await fetchCollections();toast('Coleção excluída.');}
    catch(err){toast(err.message,true);}
  }
});
$('selectedCollectionItems').addEventListener('click',async e=>{
  const btn=e.target.closest('[data-item-remove]');if(!btn)return;
  try{await api(`/api/collections/${$('collectionSelect').value}/items/${btn.dataset.itemRemove}`,{method:'DELETE'});await fetchCollections();toast('Música removida da coleção.');}
  catch(err){toast(err.message,true);}
});


// Naples v8: envio em massa GRATUITO, sem planos e sem pagamento.
let batchSource = 'links';
let currentBatchId = '';
let batchFileUploading = false;
const batchMax = 20;
function switchBatchSource(source){
  batchSource=source;
  $('batchLinksBox').hidden=source!=='links';
  $('batchFilesBox').hidden=source!=='files';
  document.querySelectorAll('[data-batch-source]').forEach(btn=>btn.classList.toggle('active',btn.dataset.batchSource===source));
}
document.querySelectorAll('[data-batch-source]').forEach(btn=>btn.addEventListener('click',()=>{
  if(!btn.disabled && !batchFileUploading) switchBatchSource(btn.dataset.batchSource);
}));
$('batchFiles').addEventListener('change',()=>{
  const files=Array.from($('batchFiles').files);
  $('batchFileNames').textContent=files.length?`${files.length} selecionado(s): ${files.slice(0,5).map(f=>f.name).join(', ')}${files.length>5?'...':''}`:'Selecione músicas próprias. Cada arquivo será enviado separadamente.';
});
function parseBatchLinks(raw){
  const lines=raw.split(/\r?\n/).map(s=>s.trim()).filter(Boolean);
  if(lines.length<1||lines.length>batchMax)throw new Error('Informe de 1 a 20 links por lote.');
  return lines.map((line,i)=>{
    let title='';let url=line;
    if(line.includes(' | ')){
      const pos=line.indexOf(' | ');
      title=line.slice(0,pos).trim();url=line.slice(pos+3).trim();
      if(title.length<3||title.length>80)throw new Error(`Linha ${i+1}: título de 3 a 80 caracteres.`);
    }
    if(!/^https:\/\//i.test(url))throw new Error(`Linha ${i+1}: use uma URL HTTPS.`);
    return {url,title};
  });
}
function renderBatchProgress(){
  if(!currentBatchId)return;
  const selected=jobs.filter(j=>j.batch_id===currentBatchId);
  if(!selected.length)return;
  $('batchProgressBox').hidden=false;
  const done=selected.filter(j=>j.status==='concluido').length;
  const failed=selected.filter(j=>['erro','interrompido'].includes(j.status)).length;
  const pending=selected.filter(j=>j.status==='pendente').length;
  const finished=done+failed+pending;
  $('batchProgressText').textContent=`${finished}/${selected.length} concluídos ou aguardando confirmação · ${done} IDs · ${failed} falhas`;
  $('batchProgressBar').style.width=`${Math.round(finished/selected.length*100)}%`;
  $('batchResultRows').innerHTML=selected.map(j=>`<div class="naples-batch-result"><strong>${safe(j.title)}</strong><small>${safe(statusLabels[j.status]||j.status)}${j.asset_id?' · Asset ID '+safe(j.asset_id):''}${j.error?' · '+safe(j.error.slice(0,180)):''}</small></div>`).join('');
}
$('batchForm').addEventListener('submit',async(e)=>{
  e.preventDefault();
  if(batchFileUploading)return;
  if(!$('batchRights').checked){toast('Confirme os direitos de todas as músicas.',true);return;}
  const button=$('batchSubmitBtn');const label=button.querySelector('span');
  button.disabled=true;
  try{
    if(batchSource==='links'){
      const items=parseBatchLinks($('batchLinks').value);
      label.textContent='CRIANDO LOTE...';
      const result=await api('/api/batches/links',{
        method:'POST', headers:{'Content-Type':'application/json'},
        body:JSON.stringify({items,rights_confirmed:true})
      });
      currentBatchId=result.batch_id;
      $('batchProgressBox').hidden=false;
      toast(`${result.total} links adicionados à fila, sem custo de plano.`);
      $('batchLinks').value='';
    }else{
      const files=Array.from($('batchFiles').files);
      if(!files.length||files.length>batchMax)throw new Error('Selecione entre 1 e 20 arquivos.');
      if(files.some(f=>f.size>50*1024*1024||!f.size))throw new Error('Cada arquivo precisa ter entre 1 byte e 50 MB.');
      const id=globalThis.crypto?.randomUUID?.().replace(/-/g,'');
      if(!id)throw new Error('Navegador incompatível com geração segura de identificadores.');
      currentBatchId=id;
      batchFileUploading=true;
      let accepted=0;const failed=[];
      for(let i=0;i<files.length;i++){
        const file=files[i];
        label.textContent=`ENVIANDO ${i+1} DE ${files.length}...`;
        const payload=new FormData();
        payload.set('title',(file.name.replace(/\.[^/.]+$/,'')||`Áudio ${i+1}`).slice(0,80).padEnd(3,'_'));
        payload.set('rights_confirmed','true');
        payload.set('batch_id',id);
        payload.set('audio_file',file);
        try{await api('/api/jobs',{method:'POST',body:payload});accepted++;}
        catch(err){failed.push(`${file.name}: ${err.message}`);}
      }
      if(!accepted){currentBatchId='';throw new Error('Nenhum arquivo entrou na fila. '+failed[0]);}
      $('batchFiles').value='';
      $('batchFileNames').textContent='Selecione seus arquivos para outro lote.';
      toast(`${accepted} arquivos adicionados. ${failed.length} não enviados.`,!!failed.length);
      if(failed.length){$('batchProgressBox').hidden=false;$('batchProgressText').textContent=`${accepted} aceitos · ${failed.length} falhas de envio: ${failed.slice(0,2).join(' | ')}`;}
    }
    $('batchRights').checked=false;
    await fetchJobs();
  }catch(err){toast(err.message,true);}
  finally{batchFileUploading=false;button.disabled=false;label.textContent='ADICIONAR LOTE À FILA';}
});


// v10 — operação individual: conectar a Roblox Key sem configurar o .env ou ID manualmente.
$('robloxKeyForm').addEventListener('submit',async event=>{
  event.preventDefault();
  const button=$('saveRobloxKeyBtn');button.disabled=true;button.textContent='VALIDANDO COM ROBLOX...';
  try{
    const body={api_key:$('robloxKeyInput').value.trim()};
    if($('useRobloxGroup').checked){body.creator_type='group';body.creator_id=$('robloxGroupId').value.trim();}
    const res=await api('/api/roblox/connection',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    $('robloxKeyInput').value='';$('useRobloxGroup').checked=false;$('groupChoice').hidden=true;
    $('robloxKeyFeedback').textContent=`✓ ${res.message} Destino: ${res.creator_type==='group'?'Grupo':'Usuário'} #${res.creator_id}`;
    await loadConnection();toast('Sua Roblox Key está conectada. Pode enviar músicas!');
  }catch(err){
    $('robloxKeyFeedback').textContent=err.message;
    if(err.message.includes('grupo')){$('useRobloxGroup').checked=true;$('groupChoice').hidden=false;}
    toast(err.message,true);
  }finally{button.disabled=false;button.textContent='VALIDAR E CONECTAR ↗';}
});
$('disconnectRobloxBtn').addEventListener('click',async()=>{
  if(!confirm('Desconectar sua Roblox Key deste usuário Discord?'))return;
  try{await api('/api/roblox/connection',{method:'DELETE'});await loadConnection();toast('Chave pessoal desconectada.');}
  catch(err){toast(err.message,true);}
});

$('useRobloxGroup').addEventListener('change',event=>{
  $('groupChoice').hidden=!event.target.checked;
});
