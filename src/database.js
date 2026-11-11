'use strict';

const fs = require('node:fs');
const path = require('node:path');
const Database = require('better-sqlite3');

const defaultPath = path.join(process.env.RAILWAY_VOLUME_MOUNT_PATH || path.join(process.cwd(), 'data'), 'naples.sqlite');
const file = process.env.DATABASE_PATH || defaultPath;
fs.mkdirSync(path.dirname(path.resolve(file)), { recursive: true });
const db = new Database(file);
db.pragma('journal_mode = WAL');
db.pragma('busy_timeout = 5000');
db.exec(`
  CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id TEXT PRIMARY KEY,
    configured INTEGER NOT NULL DEFAULT 0,
    counter INTEGER NOT NULL DEFAULT 0,
    role_suporte TEXT, role_vendas TEXT, role_duvidas TEXT,
    category_suporte TEXT, category_vendas TEXT, category_duvidas TEXT,
    log_channel TEXT
  );
  CREATE TABLE IF NOT EXISTS tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id TEXT NOT NULL,
    number INTEGER NOT NULL,
    channel_id TEXT NOT NULL UNIQUE,
    owner_id TEXT NOT NULL,
    type TEXT NOT NULL,
    topic TEXT NOT NULL,
    details TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    claimed_by TEXT,
    controls_message_id TEXT,
    created_at TEXT NOT NULL,
    closed_at TEXT,
    deleted_at TEXT,
    rating INTEGER,
    UNIQUE (guild_id, number)
  );
  CREATE INDEX IF NOT EXISTS idx_tickets_owner ON tickets (guild_id, owner_id, status);
  CREATE INDEX IF NOT EXISTS idx_tickets_channel ON tickets (channel_id);
`);

// Atualização segura: mantém as configurações e tickets já existentes.
const existingSettingsColumns = new Set(db.pragma('table_info(guild_settings)').map(c => c.name));
for (const column of ['role_staff', 'role_admin', 'role_owner']) {
  if (!existingSettingsColumns.has(column)) {
    db.exec(`ALTER TABLE guild_settings ADD COLUMN ${column} TEXT`);
  }
}

const selectSetting = db.prepare('SELECT * FROM guild_settings WHERE guild_id = ?');
const insertSetting = db.prepare('INSERT OR IGNORE INTO guild_settings (guild_id) VALUES (?)');
const settingsColumns = new Set([
  'configured', 'role_suporte', 'role_vendas', 'role_duvidas',
  'role_staff', 'role_admin', 'role_owner',
  'category_suporte', 'category_vendas', 'category_duvidas', 'log_channel'
]);

function getSettings(guildId) {
  insertSetting.run(guildId);
  return selectSetting.get(guildId);
}
function saveSettings(guildId, updates) {
  getSettings(guildId);
  const keys = Object.keys(updates).filter(k => settingsColumns.has(k));
  if (!keys.length) return getSettings(guildId);
  const q = `UPDATE guild_settings SET ${keys.map(k => `${k} = ?`).join(', ')} WHERE guild_id = ?`;
  db.prepare(q).run(...keys.map(k => updates[k]), guildId);
  return getSettings(guildId);
}
const nextNumber = db.transaction(guildId => {
  getSettings(guildId);
  db.prepare('UPDATE guild_settings SET counter = counter + 1 WHERE guild_id = ?').run(guildId);
  return selectSetting.get(guildId).counter;
});

function addTicket(data) {
  const now = new Date().toISOString();
  const result = db.prepare(`
    INSERT INTO tickets (guild_id, number, channel_id, owner_id, type, topic, details, created_at)
    VALUES (@guild_id, @number, @channel_id, @owner_id, @type, @topic, @details, @created_at)
  `).run({ ...data, created_at: now });
  return getTicketById(result.lastInsertRowid);
}
function getTicketById(id) { return db.prepare('SELECT * FROM tickets WHERE id = ?').get(id); }
function getTicketByChannel(id) { return db.prepare('SELECT * FROM tickets WHERE channel_id = ?').get(id); }
function getOpenTicket(guildId, ownerId) {
  return db.prepare(`SELECT * FROM tickets WHERE guild_id = ? AND owner_id = ? AND status = 'open' ORDER BY id DESC LIMIT 1`).get(guildId, ownerId);
}
function listExistingTickets(guildId) {
  return db.prepare(`SELECT * FROM tickets WHERE guild_id = ? AND status IN ('open', 'closed') ORDER BY id`)
    .all(guildId);
}
function setControls(id, messageId) {
  db.prepare('UPDATE tickets SET controls_message_id = ? WHERE id = ?').run(messageId, id);
}
function claim(id, userId) {
  return db.prepare(`UPDATE tickets SET claimed_by = ? WHERE id = ? AND status = 'open' AND claimed_by IS NULL`).run(userId, id).changes > 0;
}
function release(id, userId, isAdmin) {
  const statement = isAdmin
    ? `UPDATE tickets SET claimed_by = NULL WHERE id = ? AND status = 'open'`
    : `UPDATE tickets SET claimed_by = NULL WHERE id = ? AND status = 'open' AND claimed_by = ?`;
  return db.prepare(statement).run(...(isAdmin ? [id] : [id, userId])).changes > 0;
}
function close(id) {
  return db.prepare(`UPDATE tickets SET status = 'closed', closed_at = ? WHERE id = ? AND status = 'open'`)
    .run(new Date().toISOString(), id).changes > 0;
}
function reopen(id) {
  return db.prepare(`UPDATE tickets SET status = 'open', closed_at = NULL, rating = NULL WHERE id = ? AND status = 'closed'`)
    .run(id).changes > 0;
}
function markDeleted(id) {
  return db.prepare(`UPDATE tickets SET status = 'deleted', deleted_at = ? WHERE id = ? AND status != 'deleted'`)
    .run(new Date().toISOString(), id).changes > 0;
}
function rate(id, userId, stars) {
  return db.prepare(`UPDATE tickets SET rating = ? WHERE id = ? AND owner_id = ? AND status IN ('closed','deleted') AND rating IS NULL`)
    .run(stars, id, userId).changes > 0;
}

// PIX: guarda apenas IDs e valores, sem credenciais, e-mail ou dados bancários.
db.exec(`
  CREATE TABLE IF NOT EXISTS pix_payments (
    payment_id TEXT PRIMARY KEY,
    guild_id TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    buyer_id TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    external_reference TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'pending',
    message_id TEXT,
    created_at TEXT NOT NULL,
    approved_at TEXT,
    notified_at TEXT
  );
  CREATE INDEX IF NOT EXISTS idx_pix_guild ON pix_payments(guild_id, channel_id);
`);
const insertPix = db.prepare(`
  INSERT INTO pix_payments
  (payment_id, guild_id, channel_id, buyer_id, amount_cents, external_reference, created_at)
  VALUES (@payment_id, @guild_id, @channel_id, @buyer_id, @amount_cents, @external_reference, @created_at)
`);
function addPix(record) { insertPix.run({ ...record, created_at: new Date().toISOString() }); }
function getPix(id) { return db.prepare('SELECT * FROM pix_payments WHERE payment_id=?').get(String(id)); }
function setPixMessage(id, messageId) {
  db.prepare('UPDATE pix_payments SET message_id=? WHERE payment_id=?').run(messageId, String(id));
}
function markPixApproved(id) {
  db.prepare(`UPDATE pix_payments SET status='approved', approved_at=COALESCE(approved_at,?) WHERE payment_id=?`)
    .run(new Date().toISOString(), String(id));
}
function updatePixStatus(id, status) {
  db.prepare(`UPDATE pix_payments SET status=? WHERE payment_id=? AND status <> 'approved'`).run(status, String(id));
}
function markPixNotified(id) {
  db.prepare(`UPDATE pix_payments SET notified_at=COALESCE(notified_at,?) WHERE payment_id=?`)
    .run(new Date().toISOString(), String(id));
}
function pendingPixNotifications() {
  return db.prepare(`SELECT payment_id FROM pix_payments WHERE status='approved' AND notified_at IS NULL`).all();
}

module.exports = {
  db, getSettings, saveSettings, nextNumber, addTicket, getTicketById,
  getTicketByChannel, getOpenTicket, setControls, claim, release, close,
  reopen, markDeleted, rate, listExistingTickets,
  addPix, getPix, setPixMessage, markPixApproved, updatePixStatus, markPixNotified, pendingPixNotifications
};
