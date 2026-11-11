'use strict';
/* Naples Studios v23: checkout Pix - sem confiar no navegador para liberar assinatura. */
(() => {
  const $ = id => document.getElementById(id);
  let csrfToken = '';
  let refreshTimer = null;
  let busy = false;

  async function call(path, method='GET', data=null){
    const opts = {method, credentials:'same-origin', headers:{}};
    if(method !== 'GET') opts.headers['X-CSRF-Token'] = csrfToken;
    if(data !== null){ opts.headers['Content-Type']='application/json'; opts.body=JSON.stringify(data); }
    const r = await fetch(path, opts);
    const payload = await r.json().catch(()=>({}));
    if(!r.ok) throw new Error(typeof payload.detail==='string'?payload.detail:`Falha HTTP ${r.status}`);
    return payload;
  }
  function message(text, err=false){
    const node=$('billingMessage');
    node.textContent=text;
    node.classList.toggle('bad',err);
    node.hidden=false;
  }
  function displayPayment(p){
    if(!p || !p.payment_id || !['pending','in_process','creating'].includes(p.state)){
      $('billingPayment').hidden=true;
      return;
    }
    $('billingPayment').hidden=false;
    const code = $('billingPixCode');
    code.value=p.qr_code||'';
    code.hidden=!p.qr_code;
    $('billingCopyBtn').hidden=!p.qr_code;
    const img=$('billingQrImg');
    if(p.qr_base64 && /^[a-zA-Z0-9+/=]+$/.test(p.qr_base64) && p.qr_base64.length<2000000){
      img.src='data:image/png;base64,'+p.qr_base64;
      img.hidden=false;
    }else{img.removeAttribute('src');img.hidden=true;}
    const link=$('billingTicket');
    try{
      const url=new URL(p.ticket_url||'');
      if(url.protocol==='https:' && ['www.mercadopago.com.br','mercadopago.com.br'].includes(url.hostname)
        && /^\/(sandbox\/)?payments\//.test(url.pathname)){
        link.href=url.href;link.hidden=false;
      }else link.hidden=true;
    }catch{link.hidden=true;}
    if(p.expires_at){$('billingDue').textContent='Validade estimada do Pix: '+new Date(p.expires_at*1000).toLocaleString('pt-BR');}
    $('billingCreateBtn').textContent='Reutilizar Pix pendente';
  }
  async function refresh(checkProvider=false){
    if(busy)return;
    busy=true;
    try{
      const resp=await call(checkProvider?'/api/billing/check':'/api/billing/status',checkProvider?'POST':'GET');
      if(resp.subscription?.active){
        message('Pagamento confirmado! Liberando seu acesso...');
        clearInterval(refreshTimer);
        window.location.reload();
        return;
      }
      displayPayment(resp.payment);
      if(!resp.subscription?.configured){
        message('O proprietário precisa configurar a integração do Mercado Pago no Railway.',true);
        $('billingCreateBtn').disabled=true;
      }else if(!checkProvider){
        message(resp.payment?.payment_id?'Aguardando confirmação do pagamento.':'Gere seu Pix para liberar 30 dias de acesso.');
      }
      if(checkProvider) message('Aguardando pagamento. O acesso só será liberado após confirmação do Mercado Pago.');
    }catch(e){message(e.message,true);}
    finally{busy=false;}
  }
  function show(subscription,csrf){
    csrfToken=csrf;
    $('loginView').hidden=true;
    $('appView').hidden=true;
    $('billingView').hidden=false;
    $('billingPayment').hidden=true;
    $('billingMessage').hidden=true;
    if(!subscription.configured){
      $('billingCreateBtn').disabled=true;
      message('O pagamento ainda não foi configurado. Entre em contato com o administrador.',true);
    }
    refresh();
    if(refreshTimer)clearInterval(refreshTimer);
    refreshTimer=setInterval(()=>{if(!document.hidden)refresh(true);}, 25000);
  }
  $('billingForm').addEventListener('submit',async e=>{
    e.preventDefault();
    const email=$('billingEmail').value.trim();
    const cpf=$('billingCpf').value.trim();
    $('billingCreateBtn').disabled=true;
    message('Gerando cobrança Pix segura...');
    try{
      const r=await call('/api/billing/pix','POST',{email,cpf});
      displayPayment(r.payment);
      message('Pix emitido. Pague pelo QR Code ou Copia e Cola. A liberação será automática após confirmação.');
    }catch(e){message(e.message,true);}
    finally{$('billingCreateBtn').disabled=false;}
  });
  $('billingCopyBtn').addEventListener('click',async()=>{
    try{await navigator.clipboard.writeText($('billingPixCode').value);message('Código Pix copiado.');}
    catch{ $('billingPixCode').focus();$('billingPixCode').select();message('Selecione e copie o código Pix.'); }
  });
  $('billingCheckBtn').addEventListener('click',()=>refresh(true));
  $('billingLogoutBtn').addEventListener('click',async()=>{
    try{
      await call('/api/logout','POST');
      clearInterval(refreshTimer);
      location.assign('/');
    }catch(e){message(e.message,true);}
  });
  window.NaplesBilling={show};
})();
