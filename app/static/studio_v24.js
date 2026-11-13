'use strict';
/* Interface v24: navegação SPA por páginas, usando os endpoints antigos de áudio. */
(() => {
  const $ = id => document.getElementById(id);
  const pages = new Set(['dashboard','studio','batch','images','history','collections','roblox-key','monitor','quality']);
  let csrf = '';
  let enabled = false;
  let imagePolling = null;
  let imagePreviewUrl = null;
  let uploading = false;
  let previousImageJobs = [];
  const pageNames = {dashboard:'VISÃO GERAL',studio:'ENVIAR MÚSICA',batch:'UPLOAD EM MASSA',images:'UPLOAD DE IMAGEM',history:'HISTÓRICO',collections:'COLEÇÕES','roblox-key':'ROBLOX KEY',monitor:'MONITOR DE IDS',quality:'QUALIDADE DO ÁUDIO'};

  function route(){
    const page = (location.hash || '#dashboard').slice(1);
    const active = pages.has(page) ? page : 'dashboard';
    document.querySelectorAll('[data-v24-page]').forEach(node => {
      node.hidden = node.dataset.v24Page !== active;
    });
    document.querySelectorAll('#appView .nav-item').forEach(a => {
      const on = a.getAttribute('href') === '#'+active;
      a.classList.toggle('active',on);
      if (on) a.setAttribute('aria-current','page'); else a.removeAttribute('aria-current');
    });
    document.querySelectorAll('#appView .v7-mobile-nav a').forEach(a => {
      a.classList.toggle('active', a.getAttribute('href') === '#'+active);
    });
    const label = document.querySelector('#appView .topbar-location strong');
    if (label) label.textContent = 'NAPLES STUDIOS · '+pageNames[active];
    if (active === 'images' && enabled) loadImages();
    // Navegação de página sem scroll para uma seção escondida.
    window.scrollTo({top:0,behavior:'instant'});
  }
  addEventListener('hashchange',route);
  route();

  function start(config){
    csrf = config.csrf;
    enabled = true;
    const b = config.billing || {};
    $('v24PlanName').textContent = b.owner_lifetime || b.lifetime ? 'Vitalício' : (b.active ? 'Ativo' : 'Sem plano');
    $('v24PlanDetail').textContent = b.owner_lifetime || b.lifetime ? 'Acesso sem mensalidade' : (b.active ? 'Acesso liberado' : 'Verifique sua assinatura');
    $('v24KeyStatus').textContent = config.config?.roblox_connected ? 'Conectada' : 'Falta';
    route();
  }

  function recent(jobs){
    const list=$('v24RecentJobs');
    if (!list) return;
    list.replaceChildren();
    if (!jobs.length){
      const p=document.createElement('p');p.textContent='Você ainda não publicou nenhuma música.';list.appendChild(p);return;
    }
    jobs.slice(0,5).forEach(job=>{
      const row=document.createElement('div');row.className='v24-recent-row';
      const title=document.createElement('strong');title.textContent=job.title||'Áudio sem título';
      const id=document.createElement('span');id.textContent=job.asset_id||'—';
      const status=document.createElement('span');status.className='v24-state';status.textContent=({concluido:'✓ ID gerado',erro:'Falhou',processando:'Processando',fila:'Na fila',pendente:'Pendente'})[job.status]||job.status;
      const date=document.createElement('span');
      const d=Date.parse(String(job.created_at||'').replace(' ','T')+'Z');
      date.textContent=Number.isFinite(d)?new Date(d).toLocaleDateString('pt-BR'):'—';
      row.append(title,id,status,date);list.appendChild(row);
    });
  }

  async function request(path,options={}){
    const opts={credentials:'same-origin',...options};
    if(opts.method && !['GET','HEAD'].includes(opts.method.toUpperCase())){
      opts.headers={...opts.headers,'X-CSRF-Token':csrf};
    }
    const resp=await fetch(path,opts);
    const payload=await resp.json().catch(()=>({}));
    if(!resp.ok) throw new Error(typeof payload.detail==='string'?payload.detail:`Falha HTTP ${resp.status}`);
    return payload;
  }
  function feedback(txt,bad=false){
    const el=$('imageUploadFeedback');el.textContent=txt;
    el.style.color=bad?'#ff9ba8':'#b1dfcb';
  }
  function el(tag,cls,text){
    const n=document.createElement(tag);
    if(cls)n.className=cls;
    if(text!==undefined)n.textContent=String(text);
    return n;
  }

  function renderImages(jobs){
    previousImageJobs=jobs;
    const container=$('imageJobList');container.replaceChildren();
    if(!jobs.length){container.appendChild(el('p','v7-muted','Nenhuma imagem enviada ainda.'));return;}
    jobs.forEach(job=>{
      const row=el('div','v24-image-job');
      const title=el('div');
      title.append(el('strong',null,job.title),el('small',null,`${job.asset_type} · ${job.file_name} · ${job.created_at||''}`));
      if(job.error)title.appendChild(el('small',null,job.error));
      const state=el('span','v24-image-status',({concluido:'✓ ID gerado',erro:'Falhou',interrompido:'Interrompido',pendente:'Pendente',confirmando:'Confirmando',enviando:'Enviando',fila:'Na fila'})[job.status]||job.status);
      const action=el('div');
      if(job.asset_id){
        const button=el('button',null,'Copiar ID');button.type='button';
        button.addEventListener('click',async()=>{
          try{await navigator.clipboard.writeText(String(job.asset_id));feedback(`Asset ID ${job.asset_id} copiado.`);}
          catch{feedback('Não foi possível copiar. ID: '+job.asset_id,true);}
        });action.append(button);
        title.appendChild(el('small',null,'rbxassetid://'+job.asset_id));
      }else if(job.operation && ['pendente','erro','interrompido'].includes(job.status)){
        const button=el('button',null,'Verificar ID');button.type='button';
        button.addEventListener('click',async()=>{
          button.disabled=true;
          try{await request(`/api/images/${encodeURIComponent(job.id)}/verify`,{method:'POST'});await loadImages();}
          catch(err){feedback(err.message,true);}
          finally{button.disabled=false;}
        });action.append(button);
      }
      row.append(title,state,action);container.append(row);
    });
  }
  async function loadImages(){
    if(!enabled || uploading && !csrf)return;
    try{
      const data=await request('/api/images/jobs');renderImages(data.jobs||[]);
    }catch(err){feedback(err.message,true);}
  }
  $('imageRefresh').addEventListener('click',loadImages);
  const imageFile=$('imageFile');
  imageFile.addEventListener('change',()=>{
    if(imagePreviewUrl){URL.revokeObjectURL(imagePreviewUrl);imagePreviewUrl=null;}
    const file=imageFile.files?.[0];
    $('imagePreviewBox').hidden=!file;
    $('imageFilename').textContent=file?`${file.name} · ${(file.size/1048576).toFixed(2)} MB`:'Até 10 MB · menor que 8000×8000 pixels';
    if(!file)return;
    imagePreviewUrl=URL.createObjectURL(file);
    $('imagePreview').src=imagePreviewUrl;
    if(!$('imageTitle').value.trim())$('imageTitle').value=file.name.replace(/\.[^.]+$/,'').slice(0,80);
  });
  $('imageUploadForm').addEventListener('submit',async e=>{
    e.preventDefault();
    if(uploading)return;
    const file=imageFile.files?.[0];
    if(!file){feedback('Selecione uma imagem PNG ou JPG.',true);return;}
    if(file.size===0 || file.size>10*1024*1024){feedback('Tamanho máximo: 10 MB.',true);return;}
    if(!/\.(png|jpe?g)$/i.test(file.name)){feedback('Utilize PNG ou JPG/JPEG.',true);return;}
    const title=$('imageTitle').value.trim();
    if(title.length<3||title.length>80){feedback('O título deve ter de 3 a 80 caracteres.',true);return;}
    const data=new FormData();data.set('title',title);data.set('asset_type',$('imageType').value);data.set('image_file',file);
    uploading=true;
    const btn=$('imageSubmit');btn.disabled=true;
    feedback('Enviando imagem para o servidor...');
    try{
      const result=await request('/api/images',{method:'POST',body:data});
      feedback('Imagem recebida e enviada para a fila. O ID aparecerá no histórico após a confirmação do Roblox.');
      $('imageUploadForm').reset();$('imagePreviewBox').hidden=true;$('imageFilename').textContent='Até 10 MB · menor que 8000×8000 pixels';
      if(imagePreviewUrl){URL.revokeObjectURL(imagePreviewUrl);imagePreviewUrl=null;}
      await loadImages();
    }catch(err){feedback(err.message,true);}
    finally{uploading=false;btn.disabled=false;}
  });
  imagePolling=setInterval(()=>{
    if(enabled && !document.hidden && location.hash==='#images' && previousImageJobs.some(j=>['fila','enviando','confirmando','pendente'].includes(j.status)))loadImages();
  },6500);
  window.NaplesV24={start,recent};
})();
