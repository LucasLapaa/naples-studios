'use strict';
const el = id => document.getElementById(id);
let csrf = '';
let bootstrapped = false;
let requiresSetupCode = false;
let ready = false;
let secretsSet = {discord_client_secret:false, roblox_api_key:false};
let toastTimer;
function message(text, bad=false){clearTimeout(toastTimer);const node=el('settingsToast');node.textContent=text;node.classList.toggle('error',bad);node.classList.add('show');toastTimer=setTimeout(()=>node.classList.remove('show'),5200);}
async function request(path, method='GET', data){
 const opts={method,credentials:'same-origin',headers:{}};
 if(data!==undefined){opts.headers['content-type']='application/json';opts.body=JSON.stringify(data);}
 if(csrf && method!=='GET')opts.headers['x-csrf-token']=csrf;
 const r=await fetch(path,opts),body=await r.json().catch(()=>({}));
 if(!r.ok)throw new Error(typeof body.detail==='string'?body.detail:`Erro HTTP ${r.status}`);
 return body;
}
function view(id){for(const name of ['bootstrapPanel','loginPanel','configurationPanel'])el(name).hidden=name!==id;}
function updateStatus(admin){el('stateAdmin').textContent=admin?'✓':'○';el('stateAdmin').classList.toggle('ready',admin);el('stateAudio').textContent='✓';el('stateAudio').classList.add('ready');el('globalState').textContent=ready?'PRONTO PARA USO':'CONFIGURAÇÃO PENDENTE';el('globalState').classList.toggle('off',!ready);}
function setPreview(){const base=el('appBaseUrl').value.replace(/\/+$/,'');el('redirectPreview').textContent=(base||'http://127.0.0.1:8000')+'/auth/discord/callback';}
async function loadSettings(){
 const {settings,ready:ok}=await request('/api/admin/settings');ready=ok;
 for(const [key,value] of Object.entries(settings)){
   if(key.endsWith('_configured'))continue;
   const control=document.querySelector(`[name="${key}"]`);
   if(control){if(control.type==='checkbox')control.checked=!!value;else control.value=value??'';}
 }
 secretsSet.discord_client_secret=!!settings.discord_client_secret_configured;
 secretsSet.roblox_api_key=!!settings.roblox_api_key_configured;
 el('discordSecret').value='';el('robloxKey').value='';
 el('discordSecretStatus').textContent=secretsSet.discord_client_secret?'✓ Secret armazenado com segurança (deixe vazio para manter)':'Ainda não configurado';
 el('robloxKeyStatus').textContent=secretsSet.roblox_api_key?'✓ Key armazenada com segurança (deixe vazio para manter)':'Ainda não configurada';
 const discordDone=!!(settings.discord_client_id&&settings.discord_allowed_user_ids&&secretsSet.discord_client_secret),robloxDone=!!(settings.roblox_creator_id&&secretsSet.roblox_api_key);
 for(const [name,done] of [['stateDiscord',discordDone],['stateRoblox',robloxDone]]){el(name).textContent=done?'✓':'○';el(name).classList.toggle('ready',done);}
 updateStatus(true);setPreview();view('configurationPanel');
}
async function init(){try{
 const status=await request('/api/setup/status');ready=status.ready;bootstrapped=status.bootstrapped;requiresSetupCode=!!status.requires_setup_code;el('setupCodeArea').hidden=!requiresSetupCode;updateStatus(bootstrapped);
 if(!bootstrapped){view('bootstrapPanel');return;}
 try{const session=await request('/api/admin/session');csrf=session.csrf;await loadSettings();}
 catch{view('loginPanel');}
}catch(e){message('Servidor indisponível: '+e.message,true);}}
el('bootstrapForm').addEventListener('submit',async e=>{e.preventDefault();const a=el('firstPassword').value,b=el('confirmPassword').value;if(a!==b){message('As senhas não correspondem.',true);return;}try{const r=await request('/api/setup/bootstrap','POST',{password:a,setup_code:requiresSetupCode?el('setupCode').value.trim():''});csrf=r.csrf;bootstrapped=true;el('firstPassword').value='';el('confirmPassword').value='';await loadSettings();message('Administrador criado. Configure suas integrações.');}catch(x){message(x.message,true);}});
el('adminLoginForm').addEventListener('submit',async e=>{e.preventDefault();try{const r=await request('/api/admin/login','POST',{password:el('adminPassword').value});csrf=r.csrf;el('adminPassword').value='';await loadSettings();message('Acesso às configurações liberado.');}catch(x){message(x.message,true);}});
el('settingsForm').addEventListener('submit',async e=>{e.preventDefault();const body={};new FormData(el('settingsForm')).forEach((value,key)=>{body[key]=String(value).trim();});body.cookie_secure=el('cookieSecure').checked;body.enable_platform_links=el('enableLinks').checked;if(!body.discord_client_secret)delete body.discord_client_secret;if(!body.roblox_api_key)delete body.roblox_api_key;
 const btn=el('saveSettings');btn.disabled=true;el('saveState').textContent='Salvando configurações criptografadas...';
 try{const r=await request('/api/admin/settings','PUT',body);ready=r.ready;await loadSettings();el('saveState').textContent='Configurações salvas com sucesso.';message('Tudo salvo. Acesse o painel e entre com o Discord.');}
 catch(x){el('saveState').textContent='Não foi possível salvar.';message(x.message,true);}finally{btn.disabled=false;}});
el('adminLogout').addEventListener('click',async()=>{try{await request('/api/admin/logout','POST',{});csrf='';view('loginPanel');message('Sessão administrativa encerrada.');}catch(x){message(x.message,true);}});
el('changePasswordForm').addEventListener('submit',async e=>{e.preventDefault();try{await request('/api/admin/change-password','POST',{old_password:el('oldPassword').value,new_password:el('newPassword').value});csrf='';el('changePasswordForm').reset();view('loginPanel');message('Senha alterada. Entre novamente.');}catch(x){message(x.message,true);}});
el('appBaseUrl').addEventListener('input',setPreview);
for(const btn of document.querySelectorAll('[data-reveal]'))btn.addEventListener('click',()=>{const input=el(btn.dataset.reveal);input.type=input.type==='password'?'text':'password';btn.textContent=input.type==='password'?'◉':'◌';});
el('copyRedirect').addEventListener('click',async()=>{try{await navigator.clipboard.writeText(el('redirectPreview').textContent);message('Redirect URI copiada.');}catch{message('Copie manualmente a URI exibida.',true);}});
init();
