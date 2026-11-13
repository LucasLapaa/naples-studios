'use strict';
/* Naples Studios v25 — administração Discord protegida pelo backend.
   Apenas a sessão autorizada recebe dados. Nada sensível fica no HTML. */
(() => {
  const $ = id => document.getElementById(id);
  const panel = $('naplesAdminPanel');
  if (!panel) return;
  let csrf = '';
  let owner = false;
  let page = 1;
  let query = '';
  let loading = false;

  const labels = {
    banido: 'Banido', vitalicio: 'Vitalício', '30dias': '30 dias',
    pago: 'Pix ativo', liberado: 'Acesso liberado', sem_acesso: 'Sem acesso'
  };
  const fmt = ts => ts ? new Date(Number(ts)*1000).toLocaleDateString('pt-BR') : '—';
  function feedback(msg, error=false){
    const el = $('adminFeedback');
    el.textContent=msg;el.classList.toggle('bad',error);
  }
  function make(tag, cls, value){
    const el=document.createElement(tag);
    if(cls)el.className=cls;
    if(value!==undefined)el.textContent=String(value);
    return el;
  }
  function button(label, action, user, kind=''){
    const b=make('button','naples-admin-btn '+(action==='ban'?'danger':''),label);
    b.type='button';b.dataset.id=user.discord_id;b.dataset.action=action;
    if(kind)b.dataset.kind=kind;
    if(user.discord_id==='1143357188111339643' && action!=='view')b.disabled=true;
    return b;
  }
  function render(list){
    const target=$('adminUsersList');target.replaceChildren();
    if(!list.length){target.append(make('p','naples-admin-empty','Nenhum usuário encontrado.'));return;}
    for(const u of list){
      const item=make('article','naples-admin-user');
      const identity=make('div','naples-admin-identity');
      const avatar=make('div','naples-admin-avatar',String(u.username||'U').charAt(0).toUpperCase());
      if(u.avatar_url && /^https:\/\/cdn\.discordapp\.com\/avatars\/\d+\/[\w]+\.png\?size=128$/.test(u.avatar_url)){
        const img=make('img');img.alt='';img.referrerPolicy='no-referrer';img.src=u.avatar_url;
        img.onerror=()=>img.remove();avatar.replaceChildren(img);
      }
      const texts=make('div');texts.append(make('strong',null,u.username||'Discord'),make('small',null,'ID: '+u.discord_id));
      identity.append(avatar,texts);
      const badges=make('div','naples-admin-badges');
      badges.append(make('span','naples-admin-tag '+(u.suspended?'warn':'good'),labels[u.status]||u.status));
      const paidText=u.paid ? (u.payment_active?'Pix aprovado · ativo':'Pix aprovado · vencido') : 'Sem Pix confirmado';
      badges.append(make('span','naples-admin-tag',paidText));
      const details=make('div','naples-admin-meta');
      details.append(make('span',null,'Primeiro login: '+(u.created_at||'—')),
        make('span',null,'Último login: '+(u.last_login||'—')),
        make('span',null,u.lifetime?'Sem vencimento':('Vencimento: '+fmt(u.expires_at))));
      const actions=make('div','naples-admin-actions');
      actions.append(button('Liberar 30 dias','grant',u,'30days'),
        button('Dar vitalício','grant',u,'lifetime'),
        button('Remover liberação','grant',u,'revoke'),
        button(u.suspended?'Desbanir':'Banir','ban',u));
      item.append(identity,badges,details,actions);target.append(item);
    }
  }
  function showEvents(events){
    const box=$('adminAudit');box.replaceChildren();
    if(!events?.length){box.append(make('p',null,'Nenhuma alteração administrativa registrada.'));return;}
    const actions={grant_lifetime:'Liberou vitalício',grant_30days:'Liberou 30 dias',grant_revoke:'Removeu liberação',ban:'Baniu',unban:'Desbaniu'};
    for(const e of events){
      box.append(make('div','naples-admin-event',
        `${fmt(e.created_at)} · ${actions[e.action]||e.action} · ${e.subject_discord_id}`));
    }
  }
  async function request(path, options={}){
    const opts={credentials:'same-origin',...options};
    if(opts.method && opts.method!=='GET')opts.headers={...opts.headers,'Content-Type':'application/json','X-CSRF-Token':csrf};
    const res=await fetch(path,opts);
    const data=await res.json().catch(()=>({}));
    if(!res.ok)throw new Error(data.detail||`Erro HTTP ${res.status}`);
    return data;
  }
  async function load(){
    if(!owner || loading)return;
    loading=true;feedback('Carregando usuários...');
    try{
      const d=await request('/api/admin-panel/users?'+new URLSearchParams({q:query,page:String(page)}));
      $('adminTotal').textContent=d.total;
      $('adminCountShown').textContent=d.users.length;
      $('adminBanned').textContent=d.users.filter(u=>u.suspended).length;
      $('adminPageNumber').textContent=`Página ${d.page} de ${d.pages}`;
      $('adminPrev').disabled=d.page<=1;
      $('adminNext').disabled=d.page>=d.pages;
      render(d.users);showEvents(d.events);
      feedback('Exibindo '+d.users.length+' usuário(s). Banimentos e assinaturas são validados pelo servidor.');
    }catch(err){feedback(err.message,true);}
    finally{loading=false;}
  }
  function start(config){
    csrf=config.csrf||'';owner=config.isAdmin===true;
    panel.hidden=!owner;
    if(owner && location.hash==='#admin-users')load();
  }
  window.addEventListener('hashchange',()=>{
    if(owner && location.hash==='#admin-users')load();
  });
  $('adminSearchForm').addEventListener('submit',e=>{
    e.preventDefault();query=$('adminSearch').value.trim();page=1;load();
  });
  $('adminRefresh').addEventListener('click',load);
  $('adminPrev').addEventListener('click',()=>{if(page>1){page--;load();}});
  $('adminNext').addEventListener('click',()=>{page++;load();});
  $('adminUsersList').addEventListener('click',async e=>{
    const b=e.target.closest('button[data-action]');if(!b||!owner)return;
    const id=b.dataset.id;
    const action=b.dataset.action;
    const kind=b.dataset.kind;
    const label=action==='ban' ? (b.textContent==='Banir'?'BANIR permanentemente':'DESBANIR'):
      (kind==='30days'?'liberar 30 dias':kind==='lifetime'?'liberar acesso vitalício':'remover a liberação gratuita');
    if(!window.confirm(`Deseja ${label} para o Discord ID ${id}?`))return;
    b.disabled=true;
    try{
      if(action==='grant'){
        await request(`/api/admin-panel/users/${encodeURIComponent(id)}/access`,
          {method:'PUT',body:JSON.stringify({grant_type:kind})});
      }else if(action==='ban'){
        await request(`/api/admin-panel/users/${encodeURIComponent(id)}/ban`,
          {method:'PUT',body:JSON.stringify({suspended:b.textContent==='Banir'})});
      }
      await load();feedback('Permissão atualizada com sucesso.');
    }catch(err){feedback(err.message,true);}
    finally{b.disabled=false;}
  });
  window.NaplesAdmin={start};
})();
