'use strict';
const crypto = require('node:crypto');

function amountToCents(value) {
  if (typeof value !== 'number' || !Number.isFinite(value)) return null;
  const cents = Math.round(value * 100);
  if (cents < 100 || cents > 1000000 || Math.abs(value * 100 - cents) > 0.00001) return null;
  return cents;
}
function brl(cents) {
  return (cents / 100).toLocaleString('pt-BR', { style: 'currency', currency: 'BRL' });
}
function safeDescription(text) {
  return String(text || 'Pedido Naples Community').trim().slice(0, 180);
}
function parseSignature(header) {
  const result = {};
  for (const part of String(header || '').split(',')) {
    const n = part.indexOf('=');
    if (n > 0) result[part.slice(0, n).trim()] = part.slice(n + 1).trim();
  }
  return result;
}
function validateWebhook({ secret, signature, requestId, dataId, now = Date.now() }) {
  if (!secret || !requestId || !/^\d{1,30}$/.test(String(dataId))) return false;
  const { ts, v1 } = parseSignature(signature);
  if (!/^\d{10,16}$/.test(ts || '') || !/^[a-fA-F0-9]{64}$/.test(v1 || '')) return false;
  const tsMillis = Number(ts.length === 10 ? Number(ts) * 1000 : ts);
  if (!Number.isFinite(tsMillis) || Math.abs(now - tsMillis) > 10 * 60 * 1000) return false;
  const template = `id:${String(dataId).toLowerCase()};request-id:${requestId};ts:${ts};`;
  const expected = crypto.createHmac('sha256', secret).update(template).digest();
  const received = Buffer.from(v1, 'hex');
  return expected.length === received.length && crypto.timingSafeEqual(expected, received);
}
function isMatchingPayment(payment, record) {
  if (!payment || !record) return false;
  return String(payment.id) === String(record.payment_id)
    && payment.external_reference === record.external_reference
    && payment.payment_method_id === 'pix'
    && payment.currency_id === 'BRL'
    && Number.isFinite(Number(payment.transaction_amount))
    && Math.round(Number(payment.transaction_amount) * 100) === record.amount_cents;
}
module.exports = { amountToCents, brl, safeDescription, validateWebhook, isMatchingPayment };
