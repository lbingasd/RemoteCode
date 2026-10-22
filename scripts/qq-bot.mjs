#!/usr/bin/env node
// Native Node 22 only: no npm install, public listener, or cloud-side component.
import fs from 'node:fs';
import path from 'node:path';
import http from 'node:http';
import crypto from 'node:crypto';
import {fileURLToPath} from 'node:url';

const MAX_IMAGE = 10 * 1024 * 1024;
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
export const cancelled = () => ({outcome: {outcome: 'cancelled'}});
export function atomic(file, value) {
  fs.mkdirSync(path.dirname(file), {recursive: true, mode: 0o700});
  const temp = `${file}.${crypto.randomUUID()}.tmp`;
  fs.writeFileSync(temp, JSON.stringify(value, null, 2) + '\n', {mode: 0o600, flag: 'wx'});
  fs.renameSync(temp, file);
}
const load = file => JSON.parse(fs.readFileSync(file, 'utf8'));
const code = () => crypto.randomBytes(6).toString('hex').toUpperCase();
function failure(error) {
  // Never log request bodies, remote error strings, headers, tokens or URLs.
  return error?.safeCode || 'NETWORK_OR_SERVICE_ERROR';
}
function fail(safeCode) { const e = new Error(safeCode); e.safeCode = safeCode; throw e; }
export function pairingCommand(content) {
  return String(content || '').normalize('NFKC').replace(/[\u200B-\u200F\u202A-\u202E\u2060\uFEFF]/g, '')
    .trim().replace(/^<@!?\d+>\s*/, '').replace(/^\/\s*/, '').trim();
}

export class QQApi {
  constructor(config, fetcher = fetch) {
    this.config = config; this.fetcher = fetcher; this.token = ''; this.expires = 0;
    this.base = config.sandbox ? 'https://sandbox.api.sgroup.qq.com' : 'https://api.bot.qq.com';
    this.tokenPromise = null;
  }
  async json(url, body, headers = {}, method = 'POST') {
    const r = await this.fetcher(url, {method, redirect: 'error', headers: {'Content-Type': 'application/json', ...headers},
      ...(body === undefined ? {} : {body: JSON.stringify(body)}), signal: AbortSignal.timeout(20000)});
    if (!r.ok) fail(`QQ_HTTP_${r.status}`);
    const text = await r.text(); const result = text ? JSON.parse(text) : {};
    const errorCode = result.err_code ?? result.code;
    if (errorCode !== undefined && Number(errorCode) !== 0) fail(`QQ_CODE_${Number(errorCode) || 'UNKNOWN'}`);
    return result;
  }
  async accessToken() {
    if (Date.now() < this.expires) return this.token;
    if (!this.tokenPromise) this.tokenPromise = (async () => {
      const result = await this.json('https://api.bot.qq.com/app/getAppAccessToken',
        {appId: this.config.app_id, clientSecret: this.config.app_secret});
      if (!result.access_token || !(Number(result.expires_in) > 0)) fail('QQ_TOKEN_INVALID');
      this.token = result.access_token;
      this.expires = Date.now() + Math.max(1, Number(result.expires_in) - 45) * 1000;
      return this.token;
    })().finally(() => { this.tokenPromise = null; });
    return this.tokenPromise;
  }
  async call(route, body, method = 'POST') {
    // Retry only authentication failures; never retry a possibly delivered message.
    for (let attempt = 0; attempt < 2; attempt++) {
      try { return await this.json(this.base + route, body,
        {Authorization: `QQBot ${await this.accessToken()}`, 'X-Union-Appid': this.config.app_id}, method); }
      catch (e) { if (attempt || e.safeCode !== 'QQ_HTTP_401') throw e; this.expires = 0; }
    }
  }
  async text(user, content, msgId) {
    return this.call(`/v2/users/${encodeURIComponent(user)}/messages`,
      {msg_type: 0, content, ...(msgId ? {msg_id: msgId, msg_seq: 1} : {})});
  }
  async image(user, bytes, name, msgId) {
    const route = `/v2/users/${encodeURIComponent(user)}`;
    // Official multipart upload avoids public image URLs and ambiguous binary encoding.
    const hash = (kind, data) => crypto.createHash(kind).update(data).digest('hex');
    const prepared = await this.call(route + '/upload_prepare', {file_type: 1, file_name: name,
      file_size: String(bytes.length), md5: hash('md5', bytes), sha1: hash('sha1', bytes),
      md5_10m: hash('md5', bytes.subarray(0, 10002432))});
    this.uploadDiagnostic = {stage: 'prepare', fields: Object.keys(prepared).filter(k => /^[a-z_]{1,40}$/i.test(k)),
      upload_id_type: typeof prepared.upload_id, parts_is_array: Array.isArray(prepared.parts),
      parts_count: Array.isArray(prepared.parts) ? prepared.parts.length : null,
      has_file_info: typeof prepared.file_info === 'string',
      data_fields: prepared.data && typeof prepared.data === 'object' ? Object.keys(prepared.data).filter(k => /^[a-z_]{1,40}$/i.test(k)) : [],
      parts: Array.isArray(prepared.parts) ? prepared.parts.slice(0, 8).map(p => ({index: Number(p.index),
        index_type: typeof p.index, block_size: Number(p.block_size || prepared.block_size),
        fields: Object.keys(p).filter(k => /^[a-z_]{1,40}$/i.test(k))})) : []};
    if (!prepared.upload_id || !Array.isArray(prepared.parts)) fail('QQ_UPLOAD_SCHEMA');
    let offset = 0;
    const indexNumber = value => typeof value === 'number' || typeof value === 'string' && /^\d+$/.test(value) ? Number(value) : NaN;
    const firstIndex = indexNumber(prepared.parts[0]?.index);
    if (firstIndex !== 0 && firstIndex !== 1) fail('QQ_UPLOAD_SCHEMA');
    for (const [index, part] of prepared.parts.entries()) {
      const size = Number(part.block_size || prepared.block_size);
      const partIndex = indexNumber(part.index);
      if (partIndex !== index + firstIndex || !Number.isSafeInteger(size) || size <= 0) fail('QQ_UPLOAD_SCHEMA');
      const url = new URL(part.presigned_url);
      // API-issued COS upload URLs only; never forward the bot token to upload storage.
      if (url.protocol !== 'https:' || url.username || url.password ||
          !/(^|\.)(myqcloud\.com|qcloud\.com|qq\.com)$/.test(url.hostname)) fail('QQ_UPLOAD_HOST');
      const chunk = bytes.subarray(offset, Math.min(offset + size, bytes.length));
      if (!chunk.length) fail('QQ_UPLOAD_SCHEMA');
      const response = await this.fetcher(url, {method: 'PUT', redirect: 'error', body: chunk,
        signal: AbortSignal.timeout(30000)});
      if (!response.ok) fail(`QQ_UPLOAD_HTTP_${response.status}`);
      await this.call(route + '/upload_part_finish', {upload_id: prepared.upload_id, part_index: partIndex,
        block_size: String(chunk.length), md5: hash('md5', chunk)});
      offset += chunk.length;
    }
    if (offset !== bytes.length) fail('QQ_UPLOAD_SCHEMA');
    const uploaded = await this.call(route + '/files', {file_type: 1, file_name: name,
      upload_id: prepared.upload_id, srv_send_msg: false});
    if (!uploaded.file_info) fail('QQ_UPLOAD_SCHEMA');
    const sent = await this.call(route + '/messages', {msg_type: 7, media: {file_info: uploaded.file_info},
      ...(msgId ? {msg_id: msgId, msg_seq: 1} : {})});
    this.uploadDiagnostic.stage = 'sent';
    return sent;
  }
  async markdown(user, content, keyboard, msgId) {
    const body = {msg_type: 2, markdown: {content}};
    if (keyboard) body.keyboard = keyboard;
    if (msgId) { body.msg_id = msgId; body.msg_seq = 1; }
    return this.call(`/v2/users/${encodeURIComponent(user)}/messages`, body);
  }
  async interactionAck(interactionId, code = 0) {
    return this.call(`/interactions/${encodeURIComponent(interactionId)}`, {code}, 'PUT');
  }
}

export class Bridge {
  constructor(root, api = null, clock = Date.now) {
    this.root = root; this.dir = path.join(root, 'qq'); this.clock = clock;
    this.local = load(path.join(root, 'local.json'));
    this.config = fs.existsSync(path.join(this.dir, 'config.json')) ? load(path.join(this.dir, 'config.json')) : null;
    this.api = api || (this.config ? new QQApi(this.config) : null);
    this.binding = fs.existsSync(path.join(this.dir, 'binding.json')) ? load(path.join(this.dir, 'binding.json')) : {};
    this.online = false; this.lastError = ''; this.pending = new Map(); this.images = new Map();
    this.seen = new Map(); this.pairing = null; this.ownerLastSeen = 0;
    const pairingFile = path.join(this.dir, 'pairing.json');
    if (fs.existsSync(pairingFile)) this.pairing = load(pairingFile);
    this.eventCounts = {}; this.lastMessage = null;
  }
  audit(event, id = '') {
    fs.appendFileSync(path.join(this.dir, 'audit.jsonl'), JSON.stringify({time: new Date().toISOString(), event, id}) + '\n', {mode: 0o600});
  }
  status() { return {configured: !!this.config, online: this.online, bound: !!this.binding.user_openid,
    machine: this.local.id, pending: [...this.pending.values()].filter(p => !p.result).length,
    last_error: this.lastError, event_counts: {...this.eventCounts}, last_message: this.lastMessage,
    upload_diagnostic: this.api?.uploadDiagnostic || null,
    pairing_active: !!this.pairing && this.clock() < this.pairing.expires,
    pairing_seconds_left: this.pairing ? Math.max(0, Math.ceil((this.pairing.expires - this.clock()) / 1000)) : 0}; }
  received(type) {
    const known = ['READY', 'RESUMED', 'C2C_MESSAGE_CREATE', 'INTERACTION_CREATE', 'GROUP_AT_MESSAGE_CREATE',
      'GROUP_MESSAGE_CREATE', 'DIRECT_MESSAGE_CREATE', 'FRIEND_ADD', 'C2C_MSG_RECEIVE'];
    const name = known.includes(type) ? type : 'OTHER';
    this.eventCounts[name] = (this.eventCounts[name] || 0) + 1;
  }
  clean() {
    for (const [id, p] of this.pending) {
      if (!p.result && (this.clock() > p.expires || (!p.delivering && this.clock() - p.lease > 15000))) {
        p.result = cancelled(); this.audit('approval_expired', id);
      }
      if (this.clock() > p.expires + 60000) this.pending.delete(id);
    }
    for (const [id, image] of this.images) if (this.clock() > image.expires) this.images.delete(id);
    for (const [id, time] of this.seen) if (this.clock() - time > 86400000) this.seen.delete(id);
  }
  scrub(text) {
    for (const secret of [this.config?.app_secret, this.api?.token]) if (secret) text = text.split(secret).join('[REDACTED]');
    return text.replace(/((?:api[_-]?key|secret|token|password|authorization)\s*[=:]\s*)(?:"[^"]*"|'[^']*'|[^\s,}]+)/gi, '$1[REDACTED]');
  }
  approvalKeyboard(id) {
    const callback = (key, label, visited, style, tips) => ({id: key,
      render_data: {label, visited_label: visited, style},
      action: {type: 1, permission: {type: 2}, data: key, unsupport_tips: tips}});
    const rows = [{buttons: [
      callback(`approve:${id}`, '同意一次', '已同意', 1, '请回复 同意 审批码'),
      callback(`reject:${id}`, '拒绝一次', '已拒绝', 3, '请回复 拒绝 审批码'),
    ]}];
    return {content: {rows}};
  }
  async notifyApproval(p, title, details) {
    const lines = [`# RemoteCode · ${this.local.id}`, '',
      `**审批码** ${p.id}`, '',
      `**标题** ${title}`, '',
      `**会话** ${p.session}`, '',
      '**详情**', details, '',
      '点击下方按钮，或 5 分钟内回复：',
      `- 同意 ${p.id} / 拒绝 ${p.id}`,
      `- 查看截图 ${p.id}`, '',
      '仅允许一次；信息不足时请拒绝并在 IDE 中核实。'];
    await this.api.markdown(this.binding.user_openid, lines.join('\n'), this.approvalKeyboard(p.id));
  }
  async register(body) {
    this.clean();
    if (!this.online || !this.binding.user_openid) return {route: 'ide'};
    const params = body.params;
    if (!params?.sessionId || !Array.isArray(params.options) || !params.toolCall ||
        typeof body.connection !== 'string' || typeof body.request_key !== 'string') fail('INVALID_PERMISSION');
    if (this.pending.size >= 100) fail('TOO_MANY_PENDING');
    // Retrying a local POST after a timeout must not create a second request.
    const prior = [...this.pending.values()].find(p => p.connection === body.connection && p.request_key === body.request_key);
    if (prior) return {route: 'qq', id: prior.id};
    const id = code();
    const p = {id, connection: body.connection, request_key: body.request_key, session: params.sessionId,
      options: params.options.filter(o => ['allow_once', 'reject_once'].includes(o.kind) && typeof o.optionId === 'string'),
      expires: this.clock() + 300000, lease: this.clock(), delivering: true, result: null};
    this.pending.set(id, p);
    const details = this.scrub(JSON.stringify(params.toolCall.rawInput ?? params.toolCall.content ?? '').slice(0, 1800));
    const title = this.scrub(String(params.toolCall.title || '操作审批').slice(0, 300));
    try {
      await this.notifyApproval(p, title, details);
      p.delivering = false; p.lease = this.clock();
      this.audit('approval_sent', id);
      return {route: 'qq', id};
    } catch (e) {
      // Markdown/keyboard may be rejected or unsupported (e.g. sandbox): retry as plain text.
      try {
        const lines = [`RemoteCode · ${this.local.id}`, `审批码 ${id}`, `标题 ${title}`, `会话 ${p.session}`, `详情 ${details}`,
          '', '5 分钟内回复：', '1 同意一次', '2 拒绝一次', `或 同意 ${id} / 拒绝 ${id}`, `查看截图 ${id}`,
          '仅允许一次；信息不足时请拒绝并回 IDE 核实。'];
        await this.api.text(this.binding.user_openid, lines.join('\n'));
        p.delivering = false; p.lease = this.clock();
        this.audit('approval_sent', id);
        return {route: 'qq', id};
      } catch (e2) {
        // Delivery may have succeeded despite a timeout: invalidate before falling back to the IDE.
        p.result = cancelled(); this.lastError = failure(e2); this.audit('approval_send_failed', id);
        p.delivering = false;
        return {route: 'ide'};
      }
    }
  }
  poll(body) {
    this.clean(); const p = this.pending.get(body.id);
    if (!p || p.connection !== body.connection) return {result: cancelled()};
    p.lease = this.clock();
    return {result: p.result};
  }
  cancel(body) {
    for (const p of this.pending.values()) if (!p.result && p.connection === body.connection && (!body.session || p.session === body.session)) {
      p.result = cancelled(); this.audit('approval_cancelled', p.id);
    }
    return {ok: true};
  }
  approvalOption(p, kind) {
    return p.options.find(o => o.kind === kind) || null;
  }
  decideApproval(command) {
    const explicit = /^(同意|拒绝)[\s:：]*([A-F0-9]{12})$/i.exec(command);
    if (explicit) {
      const p = this.pending.get(explicit[2].toUpperCase());
      if (!p || p.result) return {handled: true};
      return {p, kind: explicit[1] === '同意' ? 'allow_once' : 'reject_once'};
    }
    const kind = command === '1' ? 'allow_once' : command === '2' ? 'reject_once'
      : command === '同意' ? 'allow_once' : command === '拒绝' ? 'reject_once' : null;
    if (!kind) return null;
    const pending = [...this.pending.values()].filter(p => !p.result);
    if (!pending.length) return {hint: 'none'};
    if (pending.length > 1) return {hint: 'many'};
    return {p: pending[0], kind};
  }
  async interaction(event) {
    this.clean();
    const interactionId = String(event.id || '');
    const ack = async code => {
      if (!interactionId) return;
      try { await this.api.interactionAck(interactionId, code); }
      catch (e) { this.lastError = failure(e); }
    };
    // Only inline-keyboard callbacks (type 11) require an ack; quick-menu etc. carry no button data.
    if (event.type !== 11) { await ack(0); return; }
    const user = event.user_openid;
    if (!this.binding.user_openid || user !== this.binding.user_openid) { await ack(4); return; }
    const buttonData = String(event.data?.resolved?.button_data || '');
    const match = /^(approve|reject):([A-F0-9]{12})$/i.exec(buttonData);
    if (!match) { await ack(0); return; }
    const p = this.pending.get(match[2].toUpperCase());
    if (!p || p.result) { await ack(3); return; }
    const kind = match[1].toLowerCase() === 'approve' ? 'allow_once' : 'reject_once';
    const option = this.approvalOption(p, kind);
    const verb = kind === 'allow_once' ? '同意' : '拒绝';
    p.result = option ? {outcome: {outcome: 'selected', optionId: option.optionId}} : cancelled();
    this.ownerLastSeen = this.clock();
    this.audit(option ? 'approval_decided' : 'approval_cancelled', p.id);
    await ack(0);
    try {
      await this.api.text(user, `审批 ${p.id}：${option ? verb + '一次的决定已记录，等待 ACP 领取。' : '原请求没有对应选项，已取消。'}执行结果请以 IDE 为准。`);
    } catch (e) { this.lastError = failure(e); }
  }
  async picture(body) {
    this.clean();
    if (!this.online || !this.binding.user_openid) fail('BOT_NOT_READY');
    const bytes = Buffer.from(body.data || '', 'base64');
    const png = bytes.subarray(0, 8).equals(Buffer.from([137,80,78,71,13,10,26,10]));
    const jpg = bytes[0] === 255 && bytes[1] === 216 && bytes[2] === 255;
    if (!bytes.length || bytes.length > MAX_IMAGE || (!png && !jpg)) fail('IMAGE_PNG_JPG_MAX_10MB');
    if (this.images.size >= 30) fail('TOO_MANY_IMAGES');
    const id = code();
    const image = {id, bytes, name: `${id}.${png ? 'png' : 'jpg'}`, session: String(body.session || ''),
      expires: this.clock() + 86400000};
    this.images.set(id, image);
    await this.api.image(this.binding.user_openid, bytes, image.name);
    this.audit('image_sent', id);
    return {id, sent: true};
  }
  async message(event) {
    this.clean();
    const user = event.author?.user_openid || event.author?.id;
    this.lastMessage = {received_at: new Date(this.clock()).toISOString(),
      has_sender: !!user, has_message_id: !!event.id, result: 'received'};
    if (!user || !event.id) {this.lastMessage.result = 'missing_sender_or_message_id'; return;}
    // The event ID, not message content or quoted text, determines deduplication.
    const key = `${user}:${event.id}`;
    if (this.seen.has(key)) {this.lastMessage.result = 'duplicate'; return;}
    this.seen.set(key, this.clock());
    const text = String(event.content || '').trim();
    const command = pairingCommand(event.content);
    this.lastMessage.content_kind = typeof event.content;
    this.lastMessage.text_length = text.length;
    this.lastMessage.has_binding_word = command.includes('绑定');
    const pairAttempt = /^(?:绑定[\s:：]*)?([A-F0-9]{12})$/i.exec(command);
    if (this.pairing && this.clock() < this.pairing.expires && pairAttempt?.[1].toUpperCase() === this.pairing.code) {
      for (const p of this.pending.values()) p.result = cancelled();
      this.images.clear();
      this.binding = {user_openid: user, pair_code: this.pairing.code, reply_confirmed: false}; atomic(path.join(this.dir, 'binding.json'), this.binding);
      this.pairing = null; this.audit('bound'); this.lastMessage.result = 'bound_reply_pending';
      fs.rmSync(path.join(this.dir, 'pairing.json'), {force: true});
      try {
        await this.api.text(user, `已绑定 ${this.local.id}。回复“帮助”查看指令。`, event.id);
        this.binding.reply_confirmed = true;
        this.lastMessage.result = 'bound_reply_sent'; this.audit('binding_reply_sent');
      } catch (e) {
        this.binding.reply_error = failure(e); this.lastError = failure(e);
        this.lastMessage.result = 'bound_reply_failed'; this.audit('binding_reply_failed');
      }
      atomic(path.join(this.dir, 'binding.json'), this.binding); return;
    }
    if (user !== this.binding.user_openid) {
      this.lastMessage.result = (pairAttempt || command.startsWith('绑定')) ?
        (!this.pairing ? 'pair_not_started' : this.clock() >= this.pairing.expires ? 'pair_expired' :
          !pairAttempt ? 'pair_format_invalid' : 'pair_code_mismatch') : 'unbound_sender';
      this.audit(this.lastMessage.result); return;
    }
    this.lastMessage.result = 'bound_user_command';
    this.ownerLastSeen = this.clock();
    const decision = this.decideApproval(command);
    if (decision) {
      if (decision.handled) { await this.api.text(user, '审批已处理、已取消或已过期。', event.id); return; }
      if (decision.hint === 'none') { await this.api.text(user, '当前没有待审批请求。', event.id); return; }
      if (decision.hint === 'many') { await this.api.text(user, '有多条待审批请求，请回复“同意 审批码”或“拒绝 审批码”指定。', event.id); return; }
      const p = decision.p;
      const option = this.approvalOption(p, decision.kind);
      const verb = decision.kind === 'allow_once' ? '同意' : '拒绝';
      p.result = option ? {outcome: {outcome: 'selected', optionId: option.optionId}} : cancelled();
      this.audit(option ? 'approval_decided' : 'approval_cancelled', p.id);
      await this.api.text(user, `审批 ${p.id}：${option ? verb + '一次的决定已记录，等待 ACP 领取。' : '原请求没有对应选项，已取消。'}执行结果请以 IDE 为准。`, event.id);
      return;
    }
    const screenshot = /^查看截图(?:\s+([A-F0-9]{12}))?$/i.exec(command);
    if (screenshot) {
      let image;
      if (screenshot[1]) {
        const id = screenshot[1].toUpperCase(); const approval = this.pending.get(id);
        image = this.images.get(id) || (approval && [...this.images.values()].reverse().find(i => i.session === approval.session));
      } else image = [...this.images.values()].at(-1);
      if (image) await this.api.image(user, image.bytes, image.name, event.id);
      else await this.api.text(user, '没有可用截图。需先由截图 skill 推送；此指令不会自动截取桌面。', event.id);
      return;
    }
    if (command === '状态') {
      await this.api.text(user, `${this.local.id} 在线；待审批 ${this.status().pending} 项。`, event.id); return;
    }
    await this.api.text(user, '指令：1 同意一次 / 2 拒绝一次 / 同意 审批码 / 拒绝 审批码 / 查看截图 [编号] / 状态。单条待审批时可直接回复 1/2 或 同意/拒绝。', event.id);
  }
}

export async function startLocal(bridge, token) {
  const server = http.createServer(async (req, res) => {
    const reply = (status, value) => { res.writeHead(status, {'Content-Type': 'application/json', 'Cache-Control': 'no-store'}); res.end(JSON.stringify(value)); };
    const auth = req.headers.authorization || '';
    const expected = `Bearer ${token}`;
    const digest = value => crypto.createHash('sha256').update(value).digest();
    if (req.headers.origin || !crypto.timingSafeEqual(digest(auth), digest(expected))) { reply(403, {error: 'FORBIDDEN'}); return; }
    try {
      if (req.method === 'GET' && req.url === '/status') { reply(200, bridge.status()); return; }
      if (req.method !== 'POST') { reply(404, {error: 'NOT_FOUND'}); return; }
      const chunks = []; let length = 0;
      for await (const chunk of req) { length += chunk.length; if (length > 15 * 1024 * 1024) fail('BODY_TOO_LARGE'); chunks.push(chunk); }
      const body = JSON.parse(Buffer.concat(chunks).toString());
      let result;
      if (req.url === '/permission') result = await bridge.register(body);
      else if (req.url === '/poll') result = bridge.poll(body);
      else if (req.url === '/cancel') result = bridge.cancel(body);
      else if (req.url === '/image') result = await bridge.picture(body);
      else if (req.url === '/pair') {
        if (bridge.status().pending) fail('PENDING_APPROVALS');
        bridge.pairing = {code: code(), expires: bridge.clock() + 300000}; result = bridge.pairing;
        atomic(path.join(bridge.dir, 'pairing.json'), bridge.pairing);
      } else if (req.url === '/unbind') {
        bridge.cancel({connection: body.connection});
        for (const p of bridge.pending.values()) p.result = cancelled();
        bridge.binding = {}; bridge.pairing = null; bridge.images.clear();
        fs.rmSync(path.join(bridge.dir, 'pairing.json'), {force: true});
        atomic(path.join(bridge.dir, 'binding.json'), {}); bridge.audit('unbound'); result = {ok: true};
      } else fail('NOT_FOUND');
      reply(200, result);
    } catch (e) { reply(400, {error: failure(e)}); }
  });
  server.requestTimeout = 45000; server.headersTimeout = 10000;
  await new Promise((resolve, reject) => { server.once('error', reject); server.listen(0, '127.0.0.1', resolve); });
  atomic(path.join(bridge.dir, 'endpoint.json'), {port: server.address().port, pid: process.pid});
  return server;
}

export async function gateway(bridge, stopping) {
  let session = null; let sequence = null; let backoff = 2000;
  while (!stopping.value) {
    if (!bridge.api) { await delay(2000); continue; }
    let ws; let heartbeat; let renew;
    try {
      const result = await bridge.api.call('/gateway', undefined, 'GET');
      const url = new URL(result.url);
      if (url.protocol !== 'wss:' || url.username || url.password ||
          !/(^|\.)(qq\.com|sgroup\.qq\.com)$/.test(url.hostname)) fail('QQ_GATEWAY_HOST');
      const token = await bridge.api.accessToken();
      ws = new WebSocket(url); let ack = true;
      const done = new Promise(resolve => { ws.addEventListener('close', e => {
        if (!stopping.value) bridge.lastError = `QQ_WS_${e.code}`;
        if ([4006,4007,4009].includes(e.code)) { session = null; sequence = null; }
        resolve();
      }, {once: true}); });
      ws.addEventListener('error', () => { bridge.lastError = 'QQ_WEBSOCKET_ERROR'; ws.close(); });
      let serial = Promise.resolve();
      ws.addEventListener('message', event => {
        let packet;
        try { packet = JSON.parse(event.data); } catch { ws.close(); return; }
        if (packet.op === 10) {
          const interval = Math.max(1000, Number(packet.d?.heartbeat_interval) || 45000);
          ws.send(JSON.stringify(session ? {op: 6, d: {token: `QQBot ${token}`, session_id: session, seq: sequence}} :
            {op: 2, d: {token: `QQBot ${token}`, intents: (1 << 25) | (1 << 26), shard: [0, 1], properties: {}}}));
          heartbeat = setInterval(() => {
            if (!ack || stopping.value) { ws.close(); return; }
            ack = false; ws.send(JSON.stringify({op: 1, d: sequence}));
          }, interval);
          renew = setTimeout(() => ws.close(), Math.max(1000, bridge.api.expires - Date.now()));
        } else if (packet.op === 11) ack = true;
        else if (packet.op === 7 || packet.op === 9) { if (packet.op === 9) {session = null; sequence = null;} ws.close(); }
        else if (packet.op === 0) {
          bridge.received(packet.t);
          if (packet.t === 'READY') { session = packet.d.session_id; bridge.online = true; bridge.lastError = ''; backoff = 2000; }
          if (packet.t === 'RESUMED') {bridge.online = true; bridge.lastError = ''; backoff = 2000;}
          // Serial handling preserves dispatch order; commit resume sequence only after handling.
          serial = serial.then(async () => {
            if (packet.t === 'C2C_MESSAGE_CREATE') await bridge.message(packet.d);
            if (packet.t === 'INTERACTION_CREATE') await bridge.interaction(packet.d);
            if (packet.s !== undefined) sequence = packet.s;
          }).catch(e => {bridge.lastError = failure(e);});
        }
      });
      const connectionTimeout = setTimeout(() => { if (!bridge.online) ws.close(); }, 30000);
      await done; clearTimeout(connectionTimeout); await serial;
    } catch (e) { bridge.lastError = failure(e); }
    finally { clearInterval(heartbeat); clearTimeout(renew); bridge.online = false; ws?.close(); }
    if (!stopping.value) { console.error(`QQ reconnect: ${bridge.lastError || 'DISCONNECTED'}`); await delay(backoff); backoff = Math.min(backoff * 2, 60000); }
  }
}

async function main() {
  const root = path.resolve(process.argv[2]);
  const dir = path.join(root, 'qq');
  // A TCP reservation prevents a second local instance from receiving the same bot events.
  const lock = http.createServer();
  const localPort = Number(load(path.join(root, 'local.json')).local_port);
  if (!Number.isInteger(localPort) || localPort < 1024 || localPort > 64535) fail('LOCAL_PORT_INVALID');
  await new Promise((resolve, reject) => {lock.once('error', reject); lock.listen(localPort + 1000, '127.0.0.1', resolve);});
  const token = fs.readFileSync(path.join(dir, 'local-token'), 'utf8').trim();
  const bridge = new Bridge(root); const server = await startLocal(bridge, token);
  const sweep = setInterval(() => bridge.clean(), 1000);
  const stopping = {value: false};
  for (const signal of ['SIGTERM', 'SIGINT']) process.on(signal, () => {
    stopping.value = true; bridge.online = false; clearInterval(sweep);
    server.close(); lock.close(); process.exit(0);
  });
  console.error(bridge.config ? 'QQ service started; connecting.' : 'QQ service started; waiting for local credential configuration.');
  await gateway(bridge, stopping);
}
if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main().catch(e => {console.error(`QQ service: ${failure(e)}`); process.exitCode = 1;});
}
