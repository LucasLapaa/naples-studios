'use strict';
const $ = id => document.getElementById(id);
const liveStatuses = new Set(['fila','processando','enviando','confirmando']);
const activeStatuses = new Set([...liveStatuses,'pendente']);
const statusLabels = {fila:'Na fila',processando:'Processando',enviando:'Enviando',confirmando:'Confirmando',pendente:'Pendente',concluido:'Concluído',interrompido:'Interrompido',erro:'Falhou'};
const safe = v => String(v ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
let csrf = '';
let currentSource = 'url';
let authorized = false;
let busy = false;
let jobs = [];
let lastJobIds = new Set();
let toastTimer;

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
  const texts={cancelado:'Login cancelado. Autorize o acesso no Discord para continuar.',discord:'Falha ao validar a autenticação no Discord. Tente novamente.',acesso:'Sua conta Discord não está autorizada. Solicite acesso ao administrador.'};
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
  $('creatorId').value=config.creator_id||'';
  $('creatorType').value=config.creator_type||'user';
  if(!config.platform_links){
    $('urlHint').textContent='Links estão desativados nas configurações web. Use arquivo local ou habilite a importação no painel de administração.';
    switchSource('file');
    document.querySelector('[data-source="url"]').disabled=true;
  }
}
function switchSource(type){
  currentSource=type;
  $('urlSource').hidden=type!=='url';
  $('fileSource').hidden=type!=='file';
  document.querySelectorAll('.source-option').forEach(btn=>btn.classList.toggle('active',btn.dataset.source===type));
}
document.querySelectorAll('.source-option').forEach(btn=>btn.addEventListener('click',()=>{if(!btn.disabled)switchSource(btn.dataset.source);}));
$('audioFile').addEventListener('change',()=>{
  const file=$('audioFile').files[0];
  $('fileLabel').textContent=file?`${file.name} · ${(file.size/1048576).toFixed(2)} MB`:'MP3, WAV, OGG, FLAC, M4A, WEBM · Até 50 MB';
  if(file && !$('title').value.trim()) $('title').value=file.name.replace(/\.[^.]+$/,'').slice(0,80);
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
  const cid=$('creatorId').value.trim();
  if(!/^[1-9]\d*$/.test(cid)){toast('Digite um ID numérico válido de usuário ou grupo Roblox.',true);return;}
  if(!$('rights').checked){toast('Confirme a autorização do áudio.',true);return;}
  form.set('title',title);
  form.set('creator_id',cid);
  form.set('creator_type',$('creatorType').value);
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
  const value=new Date(text.replace(' ','T')+'Z');
  if(Number.isNaN(+value))return text;
  return new Intl.DateTimeFormat('pt-BR',{dateStyle:'short',timeStyle:'short'}).format(value);
}
function fmtDuration(value){
  if(value==null)return '';
  const n=Math.round(Number(value));return `${Math.floor(n/60)}:${String(n%60).padStart(2,'0')}`;
}
function actions(j){
  const arr=[];
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
    if(action==='copy'){
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
  try{const status=await api('/api/setup/status');if(!status.bootstrapped||!status.ready){location.replace('/configuracoes');return;}}catch(e){console.warn('Falha ao consultar status inicial');}
  try{
    const result=await api('/api/session');
    csrf=result.csrf;
    showApp(result.user,result.config);
    await fetchJobs();
  }catch(e){showLogin();}
  setInterval(()=>{
    if(authorized && jobs.some(j=>activeStatuses.has(j.status)))fetchJobs();
  },4000);
}
init();
