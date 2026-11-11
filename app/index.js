'use strict';

require('dotenv').config();
const {
  Client, Events, GatewayIntentBits, REST, Routes,
  SlashCommandBuilder, ChannelType, PermissionFlagsBits,
  MessageFlags, ActionRowBuilder, ButtonBuilder, ButtonStyle,
  EmbedBuilder
} = require('discord.js');
const store = require('./src/database');
const design = require('./src/design');
const { transcriptAttachment } = require('./src/transcript');
const { TYPES, ticketName, shortText, ticketRoleIds } = require('./src/utility');
const { newPixService, buildCommand: buildPixCommand } = require('./src/pix');

const { BOT_TOKEN, CLIENT_ID, GUILD_ID } = process.env;
console.log('[Naples] BUILD v1.0.6 - /pix Mercado Pago + /imagem + tickets');
if (!BOT_TOKEN || !CLIENT_ID || !GUILD_ID ||
    BOT_TOKEN.startsWith('COLE_') || !/^\d{17,20}$/.test(CLIENT_ID) || !/^\d{17,20}$/.test(GUILD_ID)) {
  console.error('Configure BOT_TOKEN, CLIENT_ID e GUILD_ID no .env ou nas Variables do Railway.');
  process.exit(1);
}

const client = new Client({
  intents: [GatewayIntentBits.Guilds, GatewayIntentBits.GuildMessages, GatewayIntentBits.MessageContent]
});
const opening = new Set();
const lastOpened = new Map();
const EPHEMERAL = MessageFlags.Ephemeral;

function createCommands() {
  const configure = new SlashCommandBuilder()
    .setName('configurar').setDescription('Configura os setores do Naples Tickets (administradores)')
    .setDefaultMemberPermissions(PermissionFlagsBits.Administrator)
    .addRoleOption(o => o.setName('cargo_suporte').setDescription('Atendentes de suporte'))
    .addRoleOption(o => o.setName('cargo_vendas').setDescription('Atendentes de vendas'))
    .addRoleOption(o => o.setName('cargo_duvidas').setDescription('Atendentes de dúvidas'))
    .addRoleOption(o => o.setName('cargo_staff').setDescription('Staff: acesso a todos os tickets'))
    .addRoleOption(o => o.setName('cargo_admin').setDescription('Admin: acesso a todos os tickets'))
    .addRoleOption(o => o.setName('cargo_owner').setDescription('Owner: acesso a todos os tickets'))
    .addChannelOption(o => o.setName('canal_logs').setDescription('Canal privado para auditoria').addChannelTypes(ChannelType.GuildText))
    .addChannelOption(o => o.setName('categoria_suporte').setDescription('Categoria de tickets de suporte').addChannelTypes(ChannelType.GuildCategory))
    .addChannelOption(o => o.setName('categoria_vendas').setDescription('Categoria de tickets de vendas').addChannelTypes(ChannelType.GuildCategory))
    .addChannelOption(o => o.setName('categoria_duvidas').setDescription('Categoria de tickets de dúvidas').addChannelTypes(ChannelType.GuildCategory));

  const panel = new SlashCommandBuilder()
    .setName('painel').setDescription('Publica o painel de abertura de tickets')
    .setDefaultMemberPermissions(PermissionFlagsBits.Administrator)
    .addChannelOption(o => o.setName('canal').setDescription('Canal onde publicar o painel').addChannelTypes(ChannelType.GuildText));

  // /imagem aceita arquivo enviado pelo Discord OU uma URL pública da imagem.
  // O link é opcional e aparece como endereço clicável junto à mensagem.
  const imagePost = new SlashCommandBuilder()
    .setName('imagem').setDescription('Publica imagem e/ou link no canal escolhido (administradores)')
    .setDefaultMemberPermissions(PermissionFlagsBits.Administrator)
    .addChannelOption(o => o.setName('canal').setDescription('Canal que receberá a publicação')
      .addChannelTypes(ChannelType.GuildText).setRequired(true))
    .addAttachmentOption(o => o.setName('arquivo').setDescription('Anexe PNG, JPG, WEBP ou GIF (opcional)'))
    .addStringOption(o => o.setName('imagem_url').setDescription('URL pública e direta da imagem, alternativa ao arquivo')
      .setMaxLength(1500))
    .addStringOption(o => o.setName('link').setDescription('Link clicável para aparecer na publicação (opcional)')
      .setMaxLength(1500))
    .addStringOption(o => o.setName('legenda').setDescription('Texto que acompanha a imagem ou link (opcional)')
      .setMaxLength(1000));

  const ticket = new SlashCommandBuilder()
    .setName('ticket').setDescription('Ferramentas para o atendimento atual')
    .addSubcommand(s => s.setName('info').setDescription('Mostra os dados do ticket'))
    .addSubcommand(s => s.setName('transcript').setDescription('Exporta a conversa para TXT'))
    .addSubcommand(s => s.setName('fechar').setDescription('Encerra o atendimento atual'))
    .addSubcommand(s => s.setName('reabrir').setDescription('Reabre um atendimento fechado (equipe)'))
    .addSubcommand(s => s.setName('adicionar').setDescription('Adiciona alguém ao canal (equipe)')
      .addUserOption(o => o.setName('usuario').setDescription('Membro a adicionar').setRequired(true)))
    .addSubcommand(s => s.setName('remover').setDescription('Remove um convidado do canal (equipe)')
      .addUserOption(o => o.setName('usuario').setDescription('Membro a remover').setRequired(true)))
    .addSubcommand(s => s.setName('renomear').setDescription('Renomeia o canal (equipe)')
      .addStringOption(o => o.setName('nome').setDescription('Assunto curto para o nome do canal').setRequired(true).setMinLength(2).setMaxLength(35)));

  const privacy = new SlashCommandBuilder()
    .setName('privacidade')
    .setDescription('Aplica os cargos atuais nos tickets já criados (remove acessos adicionais)')
    .setDefaultMemberPermissions(PermissionFlagsBits.Administrator);

  return [configure, panel, imagePost, ticket, privacy, buildPixCommand()].map(c => c.toJSON());
}

async function registerCommands() {
  const rest = new REST({ version: '10' }).setToken(BOT_TOKEN);
  await rest.put(Routes.applicationGuildCommands(CLIENT_ID, GUILD_ID), { body: createCommands() });
  console.log('[Naples] Comandos registrados neste servidor.');
}

function isAdmin(interaction) {
  return !!interaction.memberPermissions?.has(PermissionFlagsBits.Administrator);
}
function isStaff(interaction, t) {
  if (isAdmin(interaction)) return true;
  const conf = store.getSettings(interaction.guildId);
  const roleIds = ticketRoleIds(conf, t.type);
  const memberRoles = interaction.member?.roles;
  return roleIds.some(roleId =>
    memberRoles?.cache?.has(roleId) || (Array.isArray(memberRoles) && memberRoles.includes(roleId))
  );
}
function canView(interaction, t) {
  return t.owner_id === interaction.user.id || isStaff(interaction, t);
}
function guildTicket(interaction) {
  if (!interaction.guild || !interaction.channelId) return null;
  const t = store.getTicketByChannel(interaction.channelId);
  return t && t.guild_id === interaction.guildId && t.status !== 'deleted' ? t : null;
}
function safeError(err, context) {
  console.error(`[Naples] ${context}:`, err);
}
async function replyError(interaction, message) {
  const data = { content: `⚠️ ${message}`, flags: EPHEMERAL };
  if (interaction.deferred || interaction.replied) {
    return interaction.followUp(data).catch(err => safeError(err, 'Resposta de erro'));
  }
  return interaction.reply(data).catch(err => safeError(err, 'Resposta de erro'));
}

// Em interações de guild não armazenadas no cache, interaction.guild pode ser null.
// O comando precisa do objeto real do servidor para criar canais e categorias.
async function resolveGuild(interaction) {
  if (!interaction.guildId || interaction.guildId !== GUILD_ID) return null;
  if (interaction.guild) return interaction.guild;
  try {
    return await client.guilds.fetch(interaction.guildId);
  } catch (err) {
    console.warn('[Naples] Servidor indisponível para o bot:', interaction.guildId,
      '| Confira se a aplicação foi instalada em Guild Install com escopo bot.');
    return null;
  }
}

const GUILD_MISSING_MESSAGE =
  'Não consegui acessar a Naples Community. Verifique se o Naples Tickets está instalado ' +
  '**no servidor** (Guild Install), com os escopos `bot` e `applications.commands`, ' +
  'e confira se `GUILD_ID` é o ID da Naples Community. ' +
  'Instalar apenas o aplicativo/comando na conta não adiciona o bot ao servidor.';

async function buildTicketOverwrites(guild, ownerId, type, settings, status = 'open') {
  const baseAllow = [
    PermissionFlagsBits.ViewChannel, PermissionFlagsBits.SendMessages,
    PermissionFlagsBits.ReadMessageHistory, PermissionFlagsBits.EmbedLinks,
    PermissionFlagsBits.AttachFiles
  ];
  const entries = [
    { id: guild.roles.everyone.id, deny: [PermissionFlagsBits.ViewChannel] },
    { id: client.user.id, allow: [...baseAllow, PermissionFlagsBits.ManageChannels] },
    {
      id: ownerId,
      allow: status === 'closed' ? baseAllow.filter(p => p !== PermissionFlagsBits.SendMessages) : baseAllow,
      ...(status === 'closed' ? { deny: [PermissionFlagsBits.SendMessages] } : {})
    }
  ];
  for (const roleId of ticketRoleIds(settings, type)) {
    const role = guild.roles.cache.get(roleId) || await guild.roles.fetch(roleId).catch(() => null);
    if (!role) {
      console.warn('[Naples] Cargo configurado não encontrado:', roleId);
      continue;
    }
    entries.push({ id: roleId, allow: baseAllow });
  }
  return entries;
}

async function syncExistingTicketPermissions(guild, settings) {
  let updated = 0, skipped = 0, errors = 0;
  for (const ticket of store.listExistingTickets(guild.id)) {
    try {
      const channel = await guild.channels.fetch(ticket.channel_id).catch(() => null);
      if (!channel || channel.type !== ChannelType.GuildText) { skipped++; continue; }
      const overwrites = await buildTicketOverwrites(guild, ticket.owner_id, ticket.type, settings, ticket.status);
      // Modo privacidade estrito: remove acessos de cargos e pessoas não listados.
      await channel.permissionOverwrites.set(overwrites, 'Naples Tickets | sincronização de privacidade');
      updated++;
    } catch (error) {
      errors++;
      safeError(error, `Sincronizar permissões do ticket ${ticket.number}`);
    }
  }
  return { updated, skipped, errors };
}

async function onPrivacy(interaction) {
  if (!isAdmin(interaction)) return replyError(interaction, 'Somente administradores podem sincronizar os tickets.');
  await interaction.deferReply({ flags: EPHEMERAL });
  const guild = await resolveGuild(interaction);
  if (!guild) return interaction.editReply(`⚠️ ${GUILD_MISSING_MESSAGE}`);
  const settings = store.getSettings(guild.id);
  if (!settings.configured) return interaction.editReply('Execute `/configurar` primeiro.');
  if (!['role_staff', 'role_admin', 'role_owner'].every(k => settings[k])) {
    return interaction.editReply('⚠️ Configure primeiro os três cargos `cargo_staff`, `cargo_admin` e `cargo_owner` em `/configurar`.');
  }
  const result = await syncExistingTicketPermissions(guild, settings);
  return interaction.editReply(`🔒 **Permissões sincronizadas.**\n✅ ${result.updated} tickets atualizados\n` +
    `⏭️ ${result.skipped} canais não encontrados\n❌ ${result.errors} falhas (veja os logs).\n\n` +
    'Os canais agora permitem o solicitante, o bot e os cargos Staff, Admin e Owner. ' +
    'Participantes extras adicionados anteriormente perderam o acesso. ' +
    'Membros com permissão **Administrador** no servidor continuam tendo acesso por regra do Discord.');
}

async function makeCategoriesAndLogs(guild, config) {
  const updates = {};
  const categories = {
    suporte: 'NAPLES • SUPORTE',
    vendas: 'NAPLES • VENDAS',
    duvidas: 'NAPLES • DÚVIDAS'
  };
  for (const [type, name] of Object.entries(categories)) {
    const column = TYPES[type].categoryKey;
    const previous = config[column];
    const found = previous ? await guild.channels.fetch(previous).catch(() => null) : null;
    if (found?.type === ChannelType.GuildCategory) continue;
    const created = await guild.channels.create({ name, type: ChannelType.GuildCategory, reason: 'Configuração inicial Naples Tickets' });
    updates[column] = created.id;
  }
  const existingLog = config.log_channel ? await guild.channels.fetch(config.log_channel).catch(() => null) : null;
  if (!existingLog || existingLog.type !== ChannelType.GuildText) {
    const created = await guild.channels.create({
      name: 'naples-logs', type: ChannelType.GuildText,
      permissionOverwrites: [
        { id: guild.roles.everyone.id, deny: [PermissionFlagsBits.ViewChannel] },
        { id: client.user.id, allow: [
          PermissionFlagsBits.ViewChannel, PermissionFlagsBits.SendMessages,
          PermissionFlagsBits.ReadMessageHistory, PermissionFlagsBits.EmbedLinks,
          PermissionFlagsBits.AttachFiles
        ] }
      ],
      reason: 'Logs privados do Naples Tickets'
    });
    updates.log_channel = created.id;
  }
  return updates;
}

async function onConfigure(interaction) {
  if (!isAdmin(interaction)) return replyError(interaction, 'Apenas administradores podem configurar o bot.');
  await interaction.deferReply({ flags: EPHEMERAL });
  const guild = await resolveGuild(interaction);
  if (!guild) return interaction.editReply(`⚠️ ${GUILD_MISSING_MESSAGE}`);
  const existing = store.getSettings(interaction.guildId);
  const updates = {};
  for (const type of Object.keys(TYPES)) {
    const role = interaction.options.getRole(`cargo_${type}`);
    const category = interaction.options.getChannel(`categoria_${type}`);
    if (role) {
      if (role.id === interaction.guildId || role.managed) {
        return interaction.editReply('Escolha um cargo comum de atendimento, não @everyone nem cargos gerenciados por integrações.');
      }
      updates[TYPES[type].roleKey] = role.id;
    }
    if (category) updates[TYPES[type].categoryKey] = category.id;
  }
  for (const [field, key] of [
    ['cargo_staff', 'role_staff'],
    ['cargo_admin', 'role_admin'],
    ['cargo_owner', 'role_owner']
  ]) {
    const role = interaction.options.getRole(field);
    if (!role) continue;
    if (role.id === interaction.guildId || role.managed) {
      return interaction.editReply(`O cargo ${field} precisa ser um cargo comum do servidor, não @everyone nem cargo de integração.`);
    }
    updates[key] = role.id;
  }
  const log = interaction.options.getChannel('canal_logs');
  if (log) updates.log_channel = log.id;
  // Usa primeiro as escolhas recém-enviadas; cria apenas o que ainda estiver ausente.
  const auto = await makeCategoriesAndLogs(guild, { ...existing, ...updates });
  Object.assign(updates, auto, { configured: 1 });
  const saved = store.saveSettings(interaction.guildId, updates);
  const body = Object.entries(TYPES).map(([type, def]) =>
    `${def.emoji} **${def.label}**: <#${saved[def.categoryKey]}> | Equipe: ${saved[def.roleKey] ? `<@&${saved[def.roleKey]}>` : '**não definida**'}`
  ).join('\n');
  return interaction.editReply({
    content: `✅ **Naples Tickets configurado!**\n\n${body}\n📑 Logs: <#${saved.log_channel}>\n\n` +
      `**Acesso geral a todos os tickets:**\n` +
      `🛡️ Staff: ${saved.role_staff ? `<@&${saved.role_staff}>` : '**não definido**'}\n` +
      `⚙️ Admin: ${saved.role_admin ? `<@&${saved.role_admin}>` : '**não definido**'}\n` +
      `👑 Owner: ${saved.role_owner ? `<@&${saved.role_owner}>` : '**não definido**'}\n\n` +
      '**Próximo passo:** use `/painel` e, para tickets antigos, `/privacidade`. ' +
      'Depois de definir cargos gerais, os cargos específicos de departamento deixam de conceder acesso automaticamente.',
    allowedMentions: { parse: [] }
  });
}

async function onPanel(interaction) {
  if (!isAdmin(interaction)) return replyError(interaction, 'Somente administradores podem publicar o painel.');
  const guild = await resolveGuild(interaction);
  if (!guild) return replyError(interaction, GUILD_MISSING_MESSAGE);
  const config = store.getSettings(interaction.guildId);
  if (!config.configured) return replyError(interaction, 'Execute `/configurar` antes de publicar o painel.');
  const dest = interaction.options.getChannel('canal') ||
    await guild.channels.fetch(interaction.channelId).catch(() => null);
  if (!dest || dest.type !== ChannelType.GuildText) return replyError(interaction, 'Escolha um canal de texto.');
  await interaction.deferReply({ flags: EPHEMERAL });
  const posted = await dest.send({ ...design.panel(), allowedMentions: { parse: [] } });
  return interaction.editReply(`✅ Painel publicado em <#${dest.id}>: ${posted.url}`);
}

// Valida links sem buscar conteúdo remoto. Assim o bot não acessa URLs arbitrárias.
function publicHttpUrl(value, label) {
  const raw = (value || '').trim();
  if (!raw) return null;
  if (/\s/.test(raw)) throw new Error(`${label}: não inclua espaços ou quebras de linha no link.`);
  let parsed;
  try { parsed = new URL(raw); }
  catch { throw new Error(`${label}: informe um endereço completo, por exemplo https://site.com/imagem.png.`); }
  if (!['https:', 'http:'].includes(parsed.protocol) || !parsed.hostname || parsed.username || parsed.password)
    throw new Error(`${label}: use um link HTTP/HTTPS público e sem usuário/senha.`);
  return parsed.href;
}

async function onImagePost(interaction) {
  if (!isAdmin(interaction)) return replyError(interaction, 'Somente administradores podem publicar imagens e links.');
  const guild = await resolveGuild(interaction);
  if (!guild) return replyError(interaction, GUILD_MISSING_MESSAGE);

  const selected = interaction.options.getChannel('canal', true);
  const image = interaction.options.getAttachment('arquivo');
  const caption = (interaction.options.getString('legenda') || '').trim();
  let imageUrl, link;
  try {
    imageUrl = publicHttpUrl(interaction.options.getString('imagem_url'), 'Imagem URL');
    link = publicHttpUrl(interaction.options.getString('link'), 'Link');
  } catch (err) {
    return replyError(interaction, err.message);
  }

  if (!image && !imageUrl && !link)
    return replyError(interaction, 'Preencha `arquivo`, `imagem_url` ou `link`. É necessário informar pelo menos um deles.');
  if (image && imageUrl)
    return replyError(interaction, 'Escolha apenas uma fonte da imagem: `arquivo` OU `imagem_url`. O campo `link` pode acompanhar qualquer uma.');

  // Texto simples com URL clicável. Não marca usuários ou @everyone.
  const content = [caption, link ? `🔗 ${link}` : ''].filter(Boolean).join('\n');
  if (content.length > 2000)
    return replyError(interaction, 'A legenda e o link ultrapassam 2.000 caracteres. Reduza um dos campos.');

  const destination = await guild.channels.fetch(selected.id).catch(() => null);
  if (!destination || destination.type !== ChannelType.GuildText)
    return replyError(interaction, 'Escolha um canal de texto da Naples Community.');

  let extension = null;
  if (image) {
    extension = image.name?.split('.').pop()?.toLowerCase() || '';
    const allowed = ['png', 'jpg', 'jpeg', 'gif', 'webp'];
    const validContent = image.contentType
      ? image.contentType.startsWith('image/') : allowed.includes(extension);
    if (!validContent || !allowed.includes(extension))
      return replyError(interaction, 'O `arquivo` deve ser PNG, JPG, JPEG, WEBP ou GIF.');
  }

  const me = guild.members.me || await guild.members.fetchMe().catch(() => null);
  const permissions = me && destination.permissionsFor(me);
  const needed = [PermissionFlagsBits.ViewChannel, PermissionFlagsBits.SendMessages];
  if (image) needed.push(PermissionFlagsBits.AttachFiles);
  if (imageUrl) needed.push(PermissionFlagsBits.EmbedLinks);
  if (!permissions?.has(needed))
    return replyError(interaction, 'Faltam permissões para publicar nesse canal. Verifique: Ver Canal, Enviar Mensagens e, conforme o caso, Anexar Arquivos/Inserir Links.');

  await interaction.deferReply({ flags: EPHEMERAL });
  try {
    const payload = { allowedMentions: { parse: [] } };
    if (content) payload.content = content;
    if (image) {
      // Envia a imagem escolhida como arquivo na mensagem do bot.
      payload.files = [{ attachment: image.url, name: `naples-imagem.${extension}` }];
    } else if (imageUrl) {
      // A imagem URL aparece no corpo da mensagem como imagem incorporada.
      // O Discord resolve a URL; o processo Node.js não faz downloads externos.
      payload.embeds = [new EmbedBuilder().setColor(0x26D36C).setImage(imageUrl)];
    }

    const sent = await destination.send(payload);
    return interaction.editReply(`✅ Publicação enviada em <#${destination.id}>! ${sent.url}`);
  } catch (err) {
    safeError(err, 'Publicar imagem/link');
    return interaction.editReply('❌ Não consegui publicar. Confira as permissões e o link/arquivo. Em `imagem_url`, use uma URL pública direta de imagem.');
  }
}

async function onOpenButton(interaction, type) {
  if (!TYPES[type]) return replyError(interaction, 'Departamento inválido.');
  const guild = await resolveGuild(interaction);
  if (!guild) return replyError(interaction, GUILD_MISSING_MESSAGE);
  const settings = store.getSettings(interaction.guildId);
  if (!settings.configured) return replyError(interaction, 'Os setores ainda não foram configurados.');
  const existing = store.getOpenTicket(interaction.guildId, interaction.user.id);
  if (existing) {
    const channel = await guild.channels.fetch(existing.channel_id).catch(() => null);
    if (channel) return replyError(interaction, `Você já tem um ticket aberto em <#${channel.id}>.`);
    store.markDeleted(existing.id); // limpeza caso alguém tenha excluído o canal manualmente
  }
  await interaction.showModal(design.form(type));
}

async function onFormSubmit(interaction, type) {
  if (!TYPES[type]) return replyError(interaction, 'Categoria inválida.');
  const key = `${interaction.guildId}:${interaction.user.id}`;
  if (opening.has(key)) return replyError(interaction, 'Já estamos criando seu ticket.');
  if (Date.now() - (lastOpened.get(key) || 0) < 30_000) return replyError(interaction, 'Aguarde 30 segundos antes de abrir outro ticket.');
  opening.add(key);
  let createdChannel;
  let savedTicket;
  try {
    await interaction.deferReply({ flags: EPHEMERAL });
    const guild = await resolveGuild(interaction);
    if (!guild) return interaction.editReply(`⚠️ ${GUILD_MISSING_MESSAGE}`);
    const settings = store.getSettings(guild.id);
    if (!settings.configured) return interaction.editReply('O bot ainda não foi configurado.');
    const old = store.getOpenTicket(guild.id, interaction.user.id);
    if (old) {
      const channel = await guild.channels.fetch(old.channel_id).catch(() => null);
      if (channel) return interaction.editReply(`Você já tem um atendimento aberto em <#${channel.id}>.`);
      store.markDeleted(old.id);
    }
    const definition = TYPES[type];
    const number = store.nextNumber(guild.id);
    const topic = interaction.fields.getTextInputValue('topic');
    const details = interaction.fields.getTextInputValue('details');
    const categoryId = settings[definition.categoryKey];
    const overwrites = await buildTicketOverwrites(guild, interaction.user.id, type, settings);
    const announceRole = settings.role_staff || (ticketRoleIds(settings, type)[0] || null);
    const canMentionRole = announceRole && overwrites.some(entry => entry.id === announceRole);
    createdChannel = await guild.channels.create({
      name: ticketName(number, type),
      type: ChannelType.GuildText,
      parent: categoryId || undefined,
      permissionOverwrites: overwrites,
      reason: `Naples Tickets | ${type} | ${interaction.user.id}`
    });
    savedTicket = store.addTicket({
      guild_id: guild.id, number, channel_id: createdChannel.id,
      owner_id: interaction.user.id, type, topic, details
    });
    const hasRole = !!canMentionRole;
    const mention = `<@${interaction.user.id}>${hasRole ? ` • <@&${announceRole}>` : ''}`;
    const message = await createdChannel.send({
      content: mention,
      embeds: [design.ticketCard(savedTicket)],
      components: design.ticketButtons('open'),
      allowedMentions: { users: [interaction.user.id], roles: hasRole ? [announceRole] : [] }
    });
    store.setControls(savedTicket.id, message.id);
    lastOpened.set(key, Date.now());
    await interaction.editReply(`✅ Ticket **#${String(number).padStart(4, '0')}** criado! Acesse <#${createdChannel.id}>.`);
    await sendLog(guild, `🟢 Ticket #${number} aberto (${definition.label}) por <@${interaction.user.id}>.`, null);
  } catch (err) {
    safeError(err, 'Criar ticket');
    if (savedTicket) store.markDeleted(savedTicket.id);
    if (createdChannel) await createdChannel.delete('Falha na abertura de ticket').catch(() => {});
    await interaction.editReply('❌ Não foi possível criar o ticket. Confira as permissões do bot (Gerenciar canais e Gerenciar cargos) e tente novamente.').catch(() => {});
  } finally {
    opening.delete(key);
  }
}

async function sendLog(guild, text, attachment) {
  const config = store.getSettings(guild.id);
  if (!config.log_channel) return;
  try {
    const log = await guild.channels.fetch(config.log_channel);
    if (!log?.isTextBased()) return;
    await log.send({
      embeds: [new EmbedBuilder().setColor(design.GREEN).setDescription(shortText(text, 3800)).setTimestamp()],
      files: attachment ? [attachment] : [],
      allowedMentions: { parse: [] }
    });
  } catch (err) { safeError(err, 'Enviar log'); }
}

async function refreshControls(channel, ticket) {
  const payload = {
    embeds: [design.ticketCard(ticket)],
    components: design.ticketButtons(ticket.status, !!ticket.claimed_by),
    allowedMentions: { parse: [] }
  };
  try {
    if (ticket.controls_message_id) {
      const old = await channel.messages.fetch(ticket.controls_message_id);
      await old.edit(payload);
      return;
    }
  } catch (_) { /* Mensagem apagada manualmente: publicar outra. */ }
  const newer = await channel.send(payload);
  store.setControls(ticket.id, newer.id);
}

async function onClaim(interaction, action) {
  const t = guildTicket(interaction);
  if (!t) return replyError(interaction, 'Este canal não é um ticket ativo do Naples Tickets.');
  if (t.status !== 'open') return replyError(interaction, 'Este atendimento está fechado.');
  if (!isStaff(interaction, t)) return replyError(interaction, 'Apenas a equipe responsável pode assumir tickets.');
  await interaction.deferReply({ flags: EPHEMERAL });
  if (action === 'claim') {
    if (!store.claim(t.id, interaction.user.id)) return interaction.editReply('Este ticket já foi assumido por outro atendente.');
    await interaction.channel.send({ content: `🤝 <@${interaction.user.id}> assumiu o atendimento.`, allowedMentions: { users: [interaction.user.id] } });
  } else {
    if (!store.release(t.id, interaction.user.id, isAdmin(interaction))) {
      return interaction.editReply('Apenas quem assumiu o ticket ou um administrador pode liberá-lo.');
    }
    await interaction.channel.send({ content: '🟡 Atendimento liberado para a equipe.' });
  }
  await refreshControls(interaction.channel, store.getTicketById(t.id));
  await interaction.editReply('✅ Responsável atualizado.');
}

async function sendRating(t, guild) {
  try {
    const owner = await client.users.fetch(t.owner_id);
    await owner.send({
      embeds: [new EmbedBuilder().setColor(design.GREEN)
        .setTitle('⭐ Como foi o atendimento?')
        .setDescription(`Seu ticket **#${String(t.number).padStart(4, '0')}** na **${guild.name}** foi encerrado.\nAvalie o atendimento de 1 a 5 estrelas.`)
        .setFooter({ text: 'Naples Tickets • Sua avaliação será registrada' })],
      components: design.ratingButtons(t.id)
    });
  } catch (_) {
    // DM pode estar bloqueada; não impede o encerramento.
  }
}

async function onClose(interaction) {
  const t = guildTicket(interaction);
  if (!t) return replyError(interaction, 'Use esta opção em um canal de ticket.');
  if (!canView(interaction, t)) return replyError(interaction, 'Você não pode encerrar este atendimento.');
  if (t.status !== 'open') return replyError(interaction, 'O ticket já está fechado.');
  await interaction.deferReply({ flags: EPHEMERAL });
  // Alterar permissão antes de registrar o fechamento evita canal aberto com status fechado.
  await interaction.channel.permissionOverwrites.edit(t.owner_id, { SendMessages: false }, { reason: 'Ticket encerrado' });
  if (!store.close(t.id)) return interaction.editReply('O ticket já foi encerrado.');
  const closed = store.getTicketById(t.id);
  await interaction.channel.setName(ticketName(t.number, t.type, 'fechado')).catch(() => {});
  await refreshControls(interaction.channel, closed);
  await interaction.channel.send({ content: `🔒 Ticket encerrado por <@${interaction.user.id}>. Para reabrir, contate a equipe.`, allowedMentions: { users: [interaction.user.id] } });
  let attachment = null;
  try { attachment = await transcriptAttachment(interaction.channel, closed); }
  catch (err) { safeError(err, 'Transcrição no encerramento'); }
  await sendLog(interaction.guild, `🔒 Ticket #${t.number} (${TYPES[t.type].label}) encerrado por <@${interaction.user.id}>. Proprietário: <@${t.owner_id}>.`, attachment);
  await sendRating(closed, interaction.guild);
  await interaction.editReply('✅ Ticket encerrado. O histórico foi encaminhado aos logs (quando configurados).');
}

async function onReopen(interaction) {
  const t = guildTicket(interaction);
  if (!t) return replyError(interaction, 'Use esta opção em um ticket.');
  if (!isStaff(interaction, t)) return replyError(interaction, 'Apenas a equipe pode reabrir tickets.');
  if (t.status !== 'closed') return replyError(interaction, 'Este ticket não está fechado.');
  await interaction.deferReply({ flags: EPHEMERAL });
  const another = store.getOpenTicket(t.guild_id, t.owner_id);
  if (another && another.id !== t.id) return interaction.editReply(`O solicitante já tem outro ticket aberto: <#${another.channel_id}>.`);
  await interaction.channel.permissionOverwrites.edit(t.owner_id, { SendMessages: true }, { reason: 'Ticket reaberto' });
  if (!store.reopen(t.id)) return interaction.editReply('O ticket já foi reaberto.');
  const reopened = store.getTicketById(t.id);
  await interaction.channel.setName(ticketName(t.number, t.type)).catch(() => {});
  await refreshControls(interaction.channel, reopened);
  await interaction.channel.send({ content: `🔓 Atendimento reaberto por <@${interaction.user.id}>.`, allowedMentions: { users: [interaction.user.id] } });
  await sendLog(interaction.guild, `🔓 Ticket #${t.number} reaberto por <@${interaction.user.id}>.`);
  await interaction.editReply('✅ Ticket reaberto.');
}

async function onTranscript(interaction) {
  const t = guildTicket(interaction);
  if (!t) return replyError(interaction, 'Use esta opção em um ticket.');
  if (!canView(interaction, t)) return replyError(interaction, 'Você não tem acesso ao histórico deste ticket.');
  await interaction.deferReply({ flags: EPHEMERAL });
  const file = await transcriptAttachment(interaction.channel, t);
  return interaction.editReply({ content: '📄 Aqui está a transcrição do ticket:', files: [file] });
}

async function onDelete(interaction, confirm = false, expectedId = null) {
  const t = guildTicket(interaction);
  if (!t) return replyError(interaction, 'Ticket não encontrado.');
  if (expectedId !== null && Number(expectedId) !== t.id) return replyError(interaction, 'Confirmação de ticket diferente.');
  if (!isStaff(interaction, t)) return replyError(interaction, 'Apenas a equipe pode excluir tickets.');
  if (t.status !== 'closed') return replyError(interaction, 'Feche o ticket antes de excluí-lo.');
  if (!confirm) {
    const row = new ActionRowBuilder().addComponents(
      new ButtonBuilder().setCustomId(`naples:confirm-delete:${t.id}`).setStyle(ButtonStyle.Danger).setLabel('Confirmar exclusão'),
      new ButtonBuilder().setCustomId('naples:cancel-delete').setStyle(ButtonStyle.Secondary).setLabel('Cancelar')
    );
    return interaction.reply({ content: '⚠️ **Atenção:** o canal será excluído definitivamente. Confirma?', components: [row], flags: EPHEMERAL });
  }
  await interaction.deferReply({ flags: EPHEMERAL });
  let attachment = null;
  try { attachment = await transcriptAttachment(interaction.channel, t); }
  catch (err) { safeError(err, 'Transcrição antes de excluir'); }
  await sendLog(interaction.guild, `🗑️ Ticket #${t.number} excluído por <@${interaction.user.id}>.`, attachment);
  await interaction.channel.delete(`Naples Tickets | excluído por ${interaction.user.id}`);
  store.markDeleted(t.id);
  await interaction.editReply('✅ Canal de ticket excluído.');
}

async function onRating(interaction, id, starsRaw) {
  const ticketId = Number(id);
  const stars = Number(starsRaw);
  const t = store.getTicketById(ticketId);
  if (!t || !Number.isInteger(stars) || stars < 1 || stars > 5) return replyError(interaction, 'Avaliação inválida.');
  if (t.owner_id !== interaction.user.id) return replyError(interaction, 'Somente o solicitante pode avaliar esse ticket.');
  if (!store.rate(ticketId, interaction.user.id, stars)) return replyError(interaction, 'Este ticket já foi avaliado ou está aberto.');
  await interaction.update({
    content: `✅ Avaliação registrada: **${stars}/5 estrelas**. Obrigado!`,
    embeds: [], components: []
  });
  const guild = client.guilds.cache.get(t.guild_id);
  if (guild) await sendLog(guild, `⭐ Ticket #${t.number} recebeu avaliação **${stars}/5**.`);
}

async function onTicketCommand(interaction) {
  const t = guildTicket(interaction);
  if (!t) return replyError(interaction, 'Execute este comando dentro de um canal de ticket ativo.');
  const sub = interaction.options.getSubcommand();
  if (sub === 'info') {
    if (!canView(interaction, t)) return replyError(interaction, 'Acesso negado.');
    return interaction.reply({ embeds: [design.ticketCard(t)], flags: EPHEMERAL });
  }
  if (sub === 'transcript') return onTranscript(interaction);
  if (sub === 'fechar') return onClose(interaction);
  if (sub === 'reabrir') return onReopen(interaction);
  if (!isStaff(interaction, t)) return replyError(interaction, 'Apenas a equipe pode usar este comando.');
  if (t.status !== 'open') return replyError(interaction, 'Reabra o ticket para fazer esta alteração.');

  if (sub === 'adicionar' || sub === 'remover') {
    const user = interaction.options.getUser('usuario', true);
    if (sub === 'remover' && (user.id === t.owner_id || user.id === client.user.id)) {
      return replyError(interaction, 'Não é possível remover o autor ou o bot do ticket.');
    }
    await interaction.deferReply({ flags: EPHEMERAL });
    if (sub === 'adicionar') {
      await interaction.channel.permissionOverwrites.edit(user.id, {
        ViewChannel: true, SendMessages: true, ReadMessageHistory: true,
        AttachFiles: true, EmbedLinks: true
      });
    } else {
      // Bloqueia acesso direto; membros da equipe podem continuar acessando via cargo.
      await interaction.channel.permissionOverwrites.delete(user.id).catch(() => {});
      if (user.id !== t.owner_id) {
        await interaction.channel.permissionOverwrites.edit(user.id, { ViewChannel: false });
      }
    }
    return interaction.editReply(`✅ ${user} ${sub === 'adicionar' ? 'adicionado ao' : 'removido do'} ticket.`);
  }
  if (sub === 'renomear') {
    const desired = interaction.options.getString('nome', true);
    await interaction.deferReply({ flags: EPHEMERAL });
    const name = ticketName(t.number, desired);
    await interaction.channel.setName(name);
    return interaction.editReply(`✅ Canal renomeado para **${name}**.`);
  }
}

// Integração separada para não modificar funções dos tickets/imagens.
const pixService = newPixService({
  client, store, serverId: GUILD_ID, getTicket: guildTicket,
  isStaff, isAdmin, sendLog
});
const pixHttpServer = pixService.startHttpServer();

client.on(Events.InteractionCreate, async interaction => {
  try {
    // Avaliações acontecem por DM; por isso são processadas antes da checagem de servidor.
    if (interaction.isButton() && interaction.customId.startsWith('naples:rate:')) {
      const [, , id, stars] = interaction.customId.split(':');
      return await onRating(interaction, id, stars);
    }
    if (!interaction.inGuild() || interaction.guildId !== GUILD_ID) return;
    if (interaction.isChatInputCommand()) {
      if (interaction.commandName === 'configurar') return await onConfigure(interaction);
      if (interaction.commandName === 'painel') return await onPanel(interaction);
      if (interaction.commandName === 'imagem') return await onImagePost(interaction);
      if (interaction.commandName === 'pix') return await pixService.showPixModal(interaction);
      if (interaction.commandName === 'ticket') return await onTicketCommand(interaction);
      if (interaction.commandName === 'privacidade') return await onPrivacy(interaction);
    }
    if (interaction.isModalSubmit() && interaction.customId.startsWith('naples:pix:')) {
      return await pixService.submitPixModal(interaction);
    }
    if (interaction.isButton() && interaction.customId.startsWith('naples:pixcheck:')) {
      return await pixService.checkPixButton(interaction);
    }
    if (interaction.isModalSubmit() && interaction.customId.startsWith('naples:form:')) {
      return await onFormSubmit(interaction, interaction.customId.split(':')[2]);
    }
    if (!interaction.isButton() || !interaction.customId.startsWith('naples:')) return;
    const action = interaction.customId.split(':')[1];
    if (action === 'open') return await onOpenButton(interaction, interaction.customId.split(':')[2]);
    if (action === 'claim' || action === 'release') return await onClaim(interaction, action);
    if (action === 'close') return await onClose(interaction);
    if (action === 'reopen') return await onReopen(interaction);
    if (action === 'transcript') return await onTranscript(interaction);
    if (action === 'delete') return await onDelete(interaction);
    if (action === 'confirm-delete') return await onDelete(interaction, true, interaction.customId.split(':')[2]);
    if (action === 'cancel-delete') return await interaction.update({ content: 'Exclusão cancelada.', components: [] });
  } catch (err) {
    safeError(err, 'Interação');
    await replyError(interaction, 'Ocorreu um erro inesperado. Confira os Logs do Railway.');
  }
});

client.once(Events.ClientReady, ready => {
  pixService.recoverPixNotifications().catch(e => console.error('[Naples/Pix] Recuperar:', e.message));
  console.log(`[Naples] Online como ${ready.user.tag} | Servidor: ${GUILD_ID}`);
  console.log('[Naples] SQLite:', process.env.DATABASE_PATH || process.env.RAILWAY_VOLUME_MOUNT_PATH || './data/naples.sqlite');
  if (ready.guilds.cache.has(GUILD_ID)) {
    console.log('[Naples] ✅ Bot conectado à Naples Community (Guild Install confirmado).');
  } else {
    console.warn('[Naples] ⚠️ O bot não encontrou GUILD_ID entre os servidores conectados. ' +
      'Confirme a instalação no servidor com escopo bot, e o GUILD_ID do Railway.');
  }
});

async function boot() {
  await registerCommands();
  await client.login(BOT_TOKEN);
}
boot().catch(err => { safeError(err, 'Inicialização'); pixHttpServer.close(); client.destroy(); store.db.close(); process.exitCode = 1; });

process.on('SIGINT', () => { pixHttpServer.close(); client.destroy(); store.db.close(); process.exit(0); });
process.on('SIGTERM', () => { pixHttpServer.close(); client.destroy(); store.db.close(); process.exit(0); });
