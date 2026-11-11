'use strict';

const crypto = require('node:crypto');
const http = require('node:http');
const {
  SlashCommandBuilder, ChannelType, PermissionFlagsBits, MessageFlags,
  TextInputBuilder, TextInputStyle, ModalBuilder, ActionRowBuilder,
  ButtonBuilder, ButtonStyle, EmbedBuilder, AttachmentBuilder
} = require('discord.js');
const { amountToCents, brl, safeDescription, validateWebhook, isMatchingPayment } = require('./pix-utils');

const EPHEMERAL = MessageFlags.Ephemeral;
const mpApi = 'https://api.mercadopago.com/v1/payments';

function buildCommand() {
  return new SlashCommandBuilder()
    .setName('pix').setDescription('Gera cobrança Pix pelo Mercado Pago e confirma no ticket')
    .addNumberOption(o => o.setName('valor').setDescription('Valor em reais, por exemplo 50 ou 49.90')
      .setMinValue(1).setMaxValue(10000).setRequired(true))
    .addUserOption(o => o.setName('cliente').setDescription('Comprador (opcional dentro do ticket)'));
}

function newPixService({ client, store, serverId, getTicket, isStaff, isAdmin, sendLog }) {
  const inflight = new Set();
  const checking = new Map();
  const publicBase = String(process.env.PUBLIC_BASE_URL || '').trim().replace(/\/$/, '');
  const accessToken = String(process.env.MP_ACCESS_TOKEN || '').trim();
  const secret = String(process.env.MP_WEBHOOK_SECRET || '').trim();
  const endpoint = `${publicBase}/mercadopago/webhook`;

  function configured() {
    return !!(accessToken && secret && /^https:\/\/[^\s/]+(?:\:\d+)?$/i.test(publicBase));
  }

  async function mpRequest(url, options = {}) {
    const response = await fetch(url, {
      ...options,
      headers: {
        'Authorization': `Bearer ${accessToken}`,
        'Accept': 'application/json',
        ...(options.body ? { 'Content-Type': 'application/json' } : {}),
        ...(options.headers || {})
      },
      signal: AbortSignal.timeout(14000)
    });
    let data = {};
    try { data = await response.json(); } catch { /* body absent */ }
    if (!response.ok) {
      const errorCode = data.error || data.status || 'unknown';
      const err = new Error(`Mercado Pago HTTP ${response.status} (${String(errorCode).slice(0, 60)})`);
      err.httpStatus = response.status;
      throw err;
    }
    return data;
  }

  function authorized(interaction) {
    if (isAdmin(interaction)) return true;
    const t = getTicket(interaction);
    return Boolean(t && isStaff(interaction, t));
  }

  async function showPixModal(interaction) {
    if (!configured()) {
      return interaction.reply({ content: '⚠️ Pix ainda não configurado: adicione MP_ACCESS_TOKEN, MP_WEBHOOK_SECRET e PUBLIC_BASE_URL no Railway.', flags: EPHEMERAL });
    }
    if (!authorized(interaction)) return interaction.reply({ content: '🔒 Somente a equipe de atendimento pode gerar cobranças Pix.', flags: EPHEMERAL });
    if (!interaction.channel || interaction.channel.type !== ChannelType.GuildText) {
      return interaction.reply({ content: 'Use `/pix` em um canal de texto do servidor.', flags: EPHEMERAL });
    }
    const cents = amountToCents(interaction.options.getNumber('valor', true));
    if (cents === null) return interaction.reply({ content: 'Informe um valor entre R$ 1,00 e R$ 10.000,00, com até duas casas decimais.', flags: EPHEMERAL });
    const ticket = getTicket(interaction);
    if (ticket?.status === 'closed') return interaction.reply({ content: 'Reabra o ticket antes de gerar a cobrança.', flags: EPHEMERAL });
    const buyer = interaction.options.getUser('cliente')?.id || ticket?.owner_id;
    if (!buyer) return interaction.reply({ content: 'Fora de um ticket, selecione o usuário no campo `cliente`.', flags: EPHEMERAL });
    const modal = new ModalBuilder().setCustomId(`naples:pix:${cents}:${buyer}`).setTitle(`Pix ${brl(cents)}`.slice(0, 45));
    const email = new TextInputBuilder().setCustomId('buyer_email')
      .setLabel('E-mail do comprador (Mercado Pago)')
      .setStyle(TextInputStyle.Short).setRequired(true).setMaxLength(254)
      .setPlaceholder('comprador@exemplo.com');
    const description = new TextInputBuilder().setCustomId('description')
      .setLabel('Descrição da cobrança (opcional)').setStyle(TextInputStyle.Short)
      .setRequired(false).setMaxLength(140).setPlaceholder('Ex.: Mapa de baile personalizado');
    modal.addComponents(new ActionRowBuilder().addComponents(email), new ActionRowBuilder().addComponents(description));
    return interaction.showModal(modal);
  }

  async function submitPixModal(interaction) {
    if (!configured()) return interaction.reply({ content: 'Pix não configurado no Railway.', flags: EPHEMERAL });
    if (!authorized(interaction)) return interaction.reply({ content: 'Apenas a equipe pode gerar cobranças Pix.', flags: EPHEMERAL });
    const parts = interaction.customId.split(':');
    const cents = Number(parts[2]);
    const buyerId = parts[3];
    if (!Number.isInteger(cents) || cents < 100 || cents > 1000000 || !/^\d{17,20}$/.test(buyerId || '')) {
      return interaction.reply({ content: 'Dados da cobrança inválidos. Execute `/pix` novamente.', flags: EPHEMERAL });
    }
    const email = interaction.fields.getTextInputValue('buyer_email').trim();
    const description = safeDescription(interaction.fields.getTextInputValue('description'));
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email) || email.length > 254) {
      return interaction.reply({ content: 'Digite um e-mail válido do comprador.', flags: EPHEMERAL });
    }
    if (!interaction.channel || interaction.channel.type !== ChannelType.GuildText) {
      return interaction.reply({ content: 'Canal de atendimento inválido.', flags: EPHEMERAL });
    }
    const ticket = getTicket(interaction);
    if (ticket?.status === 'closed') return interaction.reply({ content: 'Reabra o ticket antes de gerar a cobrança.', flags: EPHEMERAL });
    const key = `${interaction.channelId}:${interaction.user.id}`;
    if (inflight.has(key)) return interaction.reply({ content: 'Já existe uma geração de cobrança em andamento. Aguarde a resposta.', flags: EPHEMERAL });
    inflight.add(key);
    await interaction.deferReply({ flags: EPHEMERAL });
    try {
      const chargeId = crypto.randomUUID();
      const externalReference = `naples-${chargeId}`;
      const payment = await mpRequest(mpApi, {
        method: 'POST',
        headers: { 'X-Idempotency-Key': chargeId },
        body: JSON.stringify({
          transaction_amount: cents / 100,
          description,
          payment_method_id: 'pix',
          payer: { email },
          external_reference: externalReference,
          notification_url: endpoint
        })
      });
      const paymentId = String(payment?.id || '');
      const details = payment.point_of_interaction?.transaction_data;
      if (!/^\d{1,30}$/.test(paymentId)) throw new Error('Mercado Pago não retornou ID da cobrança.');
      // Grava primeiro, para permitir receber webhook mesmo que a postagem falhe.
      store.addPix({ payment_id: paymentId, guild_id: interaction.guildId,
        channel_id: interaction.channelId, buyer_id: buyerId,
        amount_cents: cents, external_reference: externalReference });
      if (!details?.qr_code) {
        return interaction.editReply(`⚠️ O Mercado Pago criou a cobrança #${paymentId}, mas não retornou o código Pix. Confira os dados da conta e evite criar outra cobrança antes de verificar esta.`);
      }
      const pixCode = details.qr_code;
      const base64 = details.qr_code_base64;
      const embed = new EmbedBuilder().setColor(0x27E775)
        .setTitle('💚 NAPLES COMMUNITY • PAGAMENTO PIX')
        .setDescription(`👤 **Cliente:** <@${buyerId}>\n💳 **Método:** Pix via Mercado Pago\n💰 **Valor:** **${brl(cents)}**\n🛍️ **Descrição:** ${description}\n\n**Abra seu aplicativo bancário e escaneie o QR Code.**\n` +
          'Ou utilize o código Pix Copia e Cola abaixo. A confirmação será enviada neste canal automaticamente após a aprovação pelo Mercado Pago.')
        .addFields({ name: 'Identificação', value: `\`${paymentId}\``, inline: true },
          { name: 'Situação', value: '🟡 Aguardando pagamento', inline: true })
        .setFooter({ text: 'Não envie comprovantes como prova de aprovação; aguarde a confirmação oficial.' })
        .setTimestamp();
      const files = [];
      if (base64) {
        const bytes = Buffer.from(base64, 'base64');
        if (bytes.length > 0 && bytes.length <= 2_000_000) {
          const isJpeg = bytes[0] === 0xff && bytes[1] === 0xd8;
          const imageName = `pix-${paymentId}.${isJpeg ? 'jpg' : 'png'}`;
          files.push(new AttachmentBuilder(bytes, { name: imageName }));
          embed.setImage(`attachment://${imageName}`);
        }
      }
      if (!files.length) {
        // URL de pagamento oficial como opção de reserva caso a imagem falte.
        embed.setDescription(embed.data.description + '\n\n⚠️ QR em imagem indisponível. Utilize o link de pagamento oficial abaixo.');
      }
      const rows = [];
      const check = new ButtonBuilder().setCustomId(`naples:pixcheck:${paymentId}`)
        .setStyle(ButtonStyle.Secondary).setEmoji('🔄').setLabel('Verificar pagamento');
      const row = new ActionRowBuilder().addComponents(check);
      const ticketUrl = details.ticket_url;
      if (typeof ticketUrl === 'string' && /^https:\/\/(?:[a-z0-9-]+\.)*mercadopago\.com\.br\//i.test(ticketUrl)) {
        row.addComponents(new ButtonBuilder().setLabel('Abrir Pix').setStyle(ButtonStyle.Link).setURL(ticketUrl));
      }
      rows.push(row);
      const codeText = pixCode.length < 1750 ? `📋 **Pix Copia e Cola:**\n\`\`\`\n${pixCode}\n\`\`\`` : '📋 Código Pix muito longo: use o QR Code ou o botão Abrir Pix.';
      const sent = await interaction.channel.send({
        content: `<@${buyerId}>\n${codeText}`,
        embeds: [embed], files, components: rows,
        allowedMentions: { users: [buyerId], roles: [], parse: [] }
      });
      store.setPixMessage(paymentId, sent.id);
      await interaction.editReply(`✅ Cobrança Pix de **${brl(cents)}** publicada neste canal. ID: ${paymentId}.`);
      // Se o Pix foi aprovado muito rápido, confirma imediatamente.
      if (payment.status === 'approved') await checkPayment(paymentId);
    } catch (err) {
      console.error('[Naples/Pix] Falha ao gerar Pix:', err.message);
      await interaction.editReply(`❌ Não foi possível publicar a cobrança. ${err.message}\nConfira MP_ACCESS_TOKEN, permissões do canal e a habilitação do Pix no Mercado Pago.`)
        .catch(() => {});
    } finally {
      inflight.delete(key);
    }
  }

  async function fetchPayment(id) {
    return mpRequest(`${mpApi}/${encodeURIComponent(String(id))}`);
  }

  async function notifyApproved(record) {
    if (inflight.has(`notify:${record.payment_id}`)) return;
    inflight.add(`notify:${record.payment_id}`);
    try {
      const fresh = store.getPix(record.payment_id);
      if (!fresh || fresh.notified_at) return;
      const guild = client.guilds.cache.get(fresh.guild_id) || await client.guilds.fetch(fresh.guild_id).catch(() => null);
      if (!guild) return;
      const channel = await guild.channels.fetch(fresh.channel_id).catch(() => null);
      if (!channel?.isTextBased()) return;
      const embed = new EmbedBuilder().setColor(0x28E97B)
        .setTitle('✅ PAGAMENTO PIX APROVADO!')
        .setDescription(`💚 **Pagamento confirmado pelo Mercado Pago.**\n\n👤 Cliente: <@${fresh.buyer_id}>\n💰 Valor recebido: **${brl(fresh.amount_cents)}**\n🧾 Pagamento: \`${fresh.payment_id}\`\n\nObrigado pela preferência! A equipe já pode dar continuidade ao seu pedido.`)
        .setTimestamp();
      await channel.send({ content: `✅ <@${fresh.buyer_id}> • **Pix confirmado!**`, embeds: [embed],
        allowedMentions: { users: [fresh.buyer_id], roles: [], parse: [] } });
      store.markPixNotified(fresh.payment_id);
      await sendLog(guild, `✅ Pix aprovado: ${brl(fresh.amount_cents)} | Pagamento ${fresh.payment_id} | Canal <#${fresh.channel_id}>.`)
        .catch(e => console.error('[Naples/Pix] Log de pagamento:', e.message));
    } finally {
      inflight.delete(`notify:${record.payment_id}`);
    }
  }

  async function checkPayment(paymentId) {
    const record = store.getPix(paymentId);
    if (!record) return { status: 'unknown' };
    const fromMercadoPago = await fetchPayment(record.payment_id);
    if (!isMatchingPayment(fromMercadoPago, record)) {
      console.error('[Naples/Pix] Cobrança inconsistente - ID:', record.payment_id);
      return { status: 'mismatch' };
    }
    const status = String(fromMercadoPago.status || 'pending');
    if (status === 'approved') {
      store.markPixApproved(paymentId);
      await notifyApproved(record);
    } else {
      store.updatePixStatus(paymentId, status);
    }
    return { status };
  }

  async function checkPixButton(interaction) {
    const paymentId = interaction.customId.split(':')[2];
    const record = store.getPix(paymentId);
    if (!record || record.guild_id !== interaction.guildId || record.channel_id !== interaction.channelId) {
      return interaction.reply({ content: 'Cobrança não encontrada neste canal.', flags: EPHEMERAL });
    }
    if (interaction.user.id !== record.buyer_id && !authorized(interaction)) {
      return interaction.reply({ content: 'Somente o comprador ou a equipe pode consultar esta cobrança.', flags: EPHEMERAL });
    }
    const key = `${paymentId}:${interaction.user.id}`;
    if (Date.now() - (checking.get(key) || 0) < 10000) {
      return interaction.reply({ content: 'Aguarde 10 segundos antes de consultar novamente.', flags: EPHEMERAL });
    }
    checking.set(key, Date.now());
    await interaction.deferReply({ flags: EPHEMERAL });
    try {
      const result = await checkPayment(paymentId);
      const message = result.status === 'approved' ? '✅ O Mercado Pago confirmou o pagamento.'
        : result.status === 'pending' || result.status === 'in_process'
          ? '🟡 O Mercado Pago ainda está aguardando a confirmação.'
          : result.status === 'rejected' || result.status === 'cancelled'
            ? `❌ Pagamento com status: ${result.status}.`
            : result.status === 'mismatch' ? '⚠️ Os dados do pagamento não conferem. Contate a administração.'
              : `Situação atual: ${result.status}`;
      return interaction.editReply(message);
    } catch (err) {
      console.error('[Naples/Pix] Consulta falhou:', err.message);
      return interaction.editReply('Não foi possível consultar o Mercado Pago agora. Tente novamente em instantes.');
    }
  }

  async function onWebhook(req, res) {
    const url = new URL(req.url || '/', 'http://localhost');
    const id = url.searchParams.get('data.id') || url.searchParams.get('id');
    const requestId = String(req.headers['x-request-id'] || '');
    const signature = String(req.headers['x-signature'] || '');
    if (!validateWebhook({ secret, signature, requestId, dataId: id })) {
      res.writeHead(401); return res.end('invalid signature');
    }
    let body = '';
    try {
      for await (const chunk of req) {
        body += chunk;
        if (body.length > 65536) { res.writeHead(413); return res.end('too large'); }
      }
      const parsed = JSON.parse(body || '{}');
      if (parsed.type !== 'payment' || String(parsed.data?.id || '') !== String(id)) {
        res.writeHead(200); return res.end('ignored');
      }
      const record = store.getPix(id);
      if (record) await checkPayment(id);
      // Mesmo que não exista registro próprio, respondemos 200 para não criar retries desnecessários.
      res.writeHead(200); return res.end('ok');
    } catch (err) {
      console.error('[Naples/Pix] Erro no webhook:', err.message);
      res.writeHead(500); return res.end('retry');
    }
  }

  function startHttpServer() {
    const port = Number(process.env.PORT || 3000);
    const server = http.createServer((req, res) => {
      const pathname = new URL(req.url || '/', 'http://localhost').pathname;
      if (req.method === 'GET' && pathname === '/health') {
        res.writeHead(200, { 'Content-Type': 'text/plain; charset=utf-8' });
        return res.end('Naples Tickets online');
      }
      if (req.method === 'POST' && pathname === '/mercadopago/webhook') {
        return onWebhook(req, res);
      }
      res.writeHead(404); res.end('Not Found');
    });
    server.listen(port, '0.0.0.0', () => console.log(`[Naples/Pix] Webhook HTTP escutando na porta ${port}`));
    server.on('error', err => console.error('[Naples/Pix] HTTP:', err.message));
    if (!configured()) console.warn('[Naples/Pix] /pix desativado até definir MP_ACCESS_TOKEN, MP_WEBHOOK_SECRET e PUBLIC_BASE_URL.');
    return server;
  }

  async function recoverPixNotifications() {
    if (!configured()) return;
    for (const { payment_id } of store.pendingPixNotifications()) {
      try { await checkPayment(payment_id); }
      catch (err) { console.error('[Naples/Pix] Recuperação pendente:', payment_id, err.message); }
    }
  }

  return { buildCommand, showPixModal, submitPixModal, checkPixButton, startHttpServer, recoverPixNotifications };
}
module.exports = { newPixService, buildCommand };
