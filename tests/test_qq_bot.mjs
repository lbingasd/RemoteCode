import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {spawn} from 'node:child_process';
import {fileURLToPath} from 'node:url';
import {Bridge, QQApi, atomic, startLocal, gateway, pairingCommand} from '../scripts/qq-bot.mjs';

function fixture(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'remotecode-qq-'));
  fs.mkdirSync(path.join(root, 'qq'));
  atomic(path.join(root, 'local.json'), {id: 'mac1', local_port: 18080});
  atomic(path.join(root, 'qq/config.json'), {app_id: '12345678', app_secret: 'TEST_SECRET'});
  atomic(path.join(root, 'qq/binding.json'), {user_openid: 'owner'});
  const sent = []; const api = {text: async (...args) => {sent.push(['text', ...args]);},
    image: async (...args) => {sent.push(['image', ...args]);},
    markdown: async (...args) => {sent.push(['markdown', ...args]);},
    interactionAck: async (...args) => {sent.push(['interactionAck', ...args]);}};
  let now = Date.now();
  const bridge = new Bridge(root, api, () => now); bridge.online = true;
  t.after(() => fs.rmSync(root, {recursive: true, force: true}));
  const body = {connection: 'connection1', request_key: '12', params: {sessionId: 'session1',
    toolCall: {title: 'Run test', rawInput: {command: 'npm test'}},
    options: [{kind: 'allow_always', optionId: 'NEVER'}, {kind: 'allow_once', optionId: 'YES'},
      {kind: 'reject_once', optionId: 'NO'}]}};
  return {root, bridge, api, sent, body, advance: ms => {now += ms;}};
}
const event = (content, id = 'message1', user = 'owner') => ({id, content, author: {user_openid: user}});

test('exact user and pending option determine approval; duplicate events cannot act twice', async t => {
  const {bridge, body} = fixture(t); const {id} = await bridge.register(body);
  await bridge.message(event('同意 ' + id, 'bad', 'stranger'));
  assert.equal(bridge.poll({id, connection: body.connection}).result, null);
  await bridge.message(event('同意 ' + id));
  assert.deepEqual(bridge.poll({id, connection: body.connection}).result,
    {outcome: {outcome: 'selected', optionId: 'YES'}});
  await bridge.message(event('拒绝 ' + id));
  assert.equal(bridge.pending.get(id).result.outcome.optionId, 'YES');
  bridge.cancel({connection: body.connection});
  assert.equal(bridge.poll({id, connection: body.connection}).result.outcome.optionId, 'YES');
  const audit = fs.readFileSync(path.join(bridge.dir, 'audit.jsonl'), 'utf8');
  assert.equal(audit.includes('approval_cancelled'), false);
});
test('expiry, lost proxy lease, cancellation, and wrong connection cannot approve', async t => {
  for (const kind of ['expiry', 'lease', 'cancel', 'connection']) {
    const {bridge, body, advance} = fixture(t); const {id} = await bridge.register(body);
    if (kind === 'expiry') advance(300001);
    if (kind === 'lease') advance(15001);
    if (kind === 'cancel') bridge.cancel({connection: body.connection, session: 'session1'});
    if (kind === 'connection') {
      assert.equal(bridge.poll({id, connection: 'other'}).result.outcome.outcome, 'cancelled'); continue;
    }
    await bridge.message(event('同意 ' + id));
    assert.equal(bridge.poll({id, connection: body.connection}).result.outcome.outcome, 'cancelled');
  }
});
test('no permanent grants or free-form approval, and refusal selects supplied option', async t => {
  const {bridge, body} = fixture(t); const {id} = await bridge.register(body);
  await bridge.message(event('好的，同意 ' + id, 'freeform'));
  assert.equal(bridge.pending.get(id).result, null);
  await bridge.message(event('拒绝 ' + id));
  assert.equal(bridge.pending.get(id).result.outcome.optionId, 'NO');
  body.request_key = '13'; body.params.options = [{kind: 'allow_always', optionId: 'NEVER'}];
  const next = await bridge.register(body);
  await bridge.message(event('同意 ' + next.id, 'second'));
  assert.equal(bridge.pending.get(next.id).result.outcome.outcome, 'cancelled');
});
test('shortcut 1/2 and bare 同意/拒绝 decide exactly one pending approval', async t => {
  for (const [command, optionId] of [['1', 'YES'], ['2', 'NO'], ['同意', 'YES'], ['拒绝', 'NO']]) {
    await t.test(command, async t => {
      const {bridge, body} = fixture(t);
      const {id} = await bridge.register(body);
      await bridge.message(event(command));
      assert.deepEqual(bridge.pending.get(id).result, {outcome: {outcome: 'selected', optionId}});
    });
  }
});
test('shortcuts refuse to act on zero or multiple pending approvals; explicit code stays precise', async t => {
  const {bridge, body, sent} = fixture(t);
  await bridge.message(event('1', 'm1'));
  assert.match(sent.at(-1)[2], /没有待审批/);
  const first = await bridge.register(body);
  const second = await bridge.register({...body, connection: 'connection2', request_key: '22'});
  await bridge.message(event('1', 'm2'));
  assert.match(sent.at(-1)[2], /多条待审批/);
  await bridge.message(event('同意', 'm3'));
  assert.equal(bridge.pending.get(first.id).result, null);
  assert.equal(bridge.pending.get(second.id).result, null);
  await bridge.message(event('同意 ' + first.id, 'm4'));
  assert.equal(bridge.pending.get(first.id).result.outcome.optionId, 'YES');
  assert.equal(bridge.pending.get(second.id).result, null);
});
test('approval accepts colon and full-width normalization', async t => {
  const formats = [id => `同意：${id}`, id => `同意:${id}`, id => `同意${id}`, id => `同意　${id.toLowerCase()}`];
  for (const format of formats) {
    await t.test(format('CODE'), async t => {
      const {bridge, body} = fixture(t);
      const {id} = await bridge.register(body);
      await bridge.message(event(format(id)));
      assert.equal(bridge.pending.get(id).result.outcome.optionId, 'YES');
    });
  }
});
test('offline/unbound uses IDE; uncertain delivery invalidates remote approval', async t => {
  const {bridge, body, api} = fixture(t);
  bridge.online = false; assert.equal((await bridge.register(body)).route, 'ide');
  bridge.online = true; bridge.binding = {}; assert.equal((await bridge.register(body)).route, 'ide');
  bridge.binding = {user_openid: 'owner'};
  api.markdown = async () => {throw new Error('SECRET_URL');};
  api.text = async () => {throw new Error('SECRET_URL');};
  assert.equal((await bridge.register(body)).route, 'ide');
  assert.equal([...bridge.pending.values()][0].result.outcome.outcome, 'cancelled');
  assert.equal(bridge.lastError.includes('SECRET_URL'), false);
});
test('registration retries are idempotent and IDs differ for independent requests', async t => {
  const {bridge, body, sent} = fixture(t);
  const first = await bridge.register(body); const second = await bridge.register(body);
  assert.equal(first.id, second.id); assert.equal(sent.length, 1);
  const third = await bridge.register({...body, connection: 'other'});
  assert.notEqual(third.id, first.id);
});
test('slow notification delivery does not expire a proxy before it can poll', async t => {
  const {bridge, body, api, advance} = fixture(t);
  api.markdown = async () => {advance(20000); bridge.clean();};
  const result = await bridge.register(body);
  assert.equal(result.route, 'qq');
  assert.equal(bridge.poll({id: result.id, connection: body.connection}).result, null);
});
test('binding requires unexpired local challenge, never auto-binds first sender', async t => {
  const {bridge, root, advance} = fixture(t); bridge.binding = {};
  await bridge.message(event('你好', 'first', 'intruder')); assert.deepEqual(bridge.binding, {});
  bridge.pairing = {code: 'ABCDEF123456', expires: bridge.clock() + 300000};
  await bridge.message(event('绑定 ABCDEF123456', 'pair', 'owner'));
  assert.equal(JSON.parse(fs.readFileSync(path.join(root, 'qq/binding.json'))).user_openid, 'owner');
  assert.equal(bridge.pairing, null);
  bridge.pairing = {code: 'ABCDEF123456', expires: bridge.clock()}; advance(1);
  await bridge.message(event('绑定 ABCDEF123456', 'expired', 'intruder'));
  assert.equal(bridge.binding.user_openid, 'owner');
});
test('pair diagnostics expose outcomes without exposing codes, sender or message content', async t => {
  const {bridge} = fixture(t); bridge.binding = {};
  bridge.pairing = {code: 'ABCDEF123456', expires: bridge.clock() + 300000};
  await bridge.message(event('绑定 FFFFFFFFFFFF'));
  assert.equal(bridge.status().last_message.result, 'pair_code_mismatch');
  await bridge.message({id: 'pair-id', content: '绑定：abcdef123456', author: {id: 'OPENID_SECRET'}});
  assert.equal(bridge.binding.user_openid, 'OPENID_SECRET');
  assert.equal(bridge.status().last_message.result, 'bound_reply_sent');
  bridge.received('C2C_MESSAGE_CREATE');
  const status = JSON.stringify(bridge.status());
  for (const secret of ['ABCDEF123456', 'FFFFFFFFFFFF', 'OPENID_SECRET', 'TEST_SECRET']) assert.equal(status.includes(secret), false);
  assert.equal(bridge.status().event_counts.C2C_MESSAGE_CREATE, 1);
});
test('pairing accepts copied formatting, slash/mention prefixes and exact bare code', async t => {
  const cases = ['绑定 ABCDEF123456', '/绑定 abcdef123456', '<@!12345678> /绑定：abcdef123456',
    '绑定\u200b　ＡＢＣＤＥＦ１２３４５６', 'abcdef123456'];
  for (let i = 0; i < cases.length; i++) {
    const {bridge} = fixture(t); bridge.binding = {};
    bridge.pairing = {code: 'ABCDEF123456', expires: bridge.clock() + 300000};
    await bridge.message(event(cases[i], `case-${i}`, 'owner'));
    assert.equal(bridge.binding.user_openid, 'owner');
    assert.equal(bridge.binding.reply_confirmed, true);
  }
  assert.equal(pairingCommand('请执行绑定 ABCDEF123456'), '请执行绑定 ABCDEF123456');
});
test('binding failure is reported independently of accepted user identity', async t => {
  const {bridge, api, root} = fixture(t); bridge.binding = {};
  bridge.pairing = {code: 'ABCDEF123456', expires: bridge.clock() + 300000};
  api.text = async () => {const error = new Error('DO_NOT_ECHO'); error.safeCode = 'QQ_CODE_40034105'; throw error;};
  await bridge.message(event('绑定 ABCDEF123456'));
  assert.equal(bridge.binding.user_openid, 'owner');
  assert.equal(bridge.binding.reply_confirmed, false);
  assert.equal(bridge.status().last_message.result, 'bound_reply_failed');
  assert.equal(bridge.status().last_error, 'QQ_CODE_40034105');
  assert.equal(JSON.parse(fs.readFileSync(path.join(root, 'qq/binding.json'))).reply_error, 'QQ_CODE_40034105');
});
test('active challenge survives restart and is removed after binding', async t => {
  const {bridge, root, api} = fixture(t);
  const server = await startLocal(bridge, 'TEST_LOCAL_TOKEN');
  t.after(() => new Promise(resolve => server.close(resolve)));
  const response = await fetch(`http://127.0.0.1:${server.address().port}/pair`,
    {method: 'POST', headers: {Authorization: 'Bearer TEST_LOCAL_TOKEN'}, body: '{}'});
  const challenge = await response.json();
  const restarted = new Bridge(root, api); restarted.online = true;
  assert.equal(restarted.pairing.code, challenge.code);
  await restarted.message(event('绑定 ' + challenge.code));
  assert.equal(restarted.binding.user_openid, 'owner');
  assert.equal(fs.existsSync(path.join(root, 'qq/pairing.json')), false);
  assert.equal(new Bridge(root, api).pairing, null);
});
test('image delivery/retrieval associates session and expires; arbitrary data rejected', async t => {
  const {bridge, body, sent, advance} = fixture(t);
  const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aGxkAAAAASUVORK5CYII=', 'base64');
  const image = await bridge.picture({data: png.toString('base64'), session: 'session1'});
  const permission = await bridge.register(body);
  await bridge.message(event('查看截图 ' + permission.id, 'picture'));
  assert.equal(sent.at(-1)[0], 'image'); assert.equal(sent.at(-1)[3], `${image.id}.png`);
  await assert.rejects(bridge.picture({data: Buffer.from('not an image').toString('base64')}));
  advance(86400001); await bridge.message(event('查看截图 ' + image.id, 'expired-picture'));
  assert.equal(sent.at(-1)[0], 'text'); assert.match(sent.at(-1)[2], /没有可用截图/);
});
test('approval sends structured markdown with tap buttons and no link; text fallback has no link', async t => {
  const {bridge, body, sent} = fixture(t);
  bridge.local.url = 'https://1.2.3.4/m/mac1/';
  const {id} = await bridge.register(body);
  const [kind, user, content, keyboard] = sent.at(-1);
  assert.equal(kind, 'markdown'); assert.equal(user, 'owner');
  assert.equal(content.startsWith('# RemoteCode · mac1'), true);
  assert.ok(content.includes(`**审批码** ${id}`));
  assert.ok(content.includes('**标题** `Run test`'));
  assert.ok(content.includes('**会话** session1'));
  assert.ok(content.includes('**详情**'));
  assert.ok(content.includes('- `command`: `npm test`'));
  assert.equal(content.includes('{"command"'), false);
  assert.equal(content.includes('查看截图'), false);
  assert.ok(content.includes(`- 同意 ${id} / 拒绝 ${id}`));
  assert.equal(content.includes('https://1.2.3.4'), false);
  assert.equal(keyboard.content.rows.length, 1);
  assert.deepEqual(keyboard.content.rows[0].buttons.map(b => b.action.data), [`approve:${id}`, `reject:${id}`]);
  const {bridge: b2, body: body2, sent: sent2, api: api2} = fixture(t);
  b2.local.url = 'https://1.2.3.4/m/mac1/';
  api2.markdown = async () => {throw new Error('UNSUPPORTED');};
  assert.equal((await b2.register(body2)).route, 'qq');
  assert.equal(sent2.at(-1)[0], 'text');
  assert.equal(sent2.at(-1)[2].includes('https://1.2.3.4'), false);
});
test('sensitive rawInput keys are fully redacted and markdown syntax is neutralized', async t => {
  const {bridge, sent} = fixture(t);
  const body = {connection: 'c1', request_key: 'r1', params: {sessionId: 's1',
    toolCall: {title: 't', rawInput: {command: '# [phish](http://evil)\n**bold**', token: 'secret phrase'}},
    options: [{kind: 'allow_once', optionId: 'YES'}]}};
  await bridge.register(body);
  const content = sent.at(-1)[2];
  assert.ok(content.includes('- `command`: `# [phish](http://evil) **bold**`'));
  assert.ok(content.includes('`[REDACTED]`'));
  assert.equal(content.includes('secret phrase'), false);
});
test('image delivery sends only the image, with no trailing text or link', async t => {
  const {bridge, sent} = fixture(t);
  bridge.local.url = 'https://1.2.3.4/m/mac1/';
  const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aGxkAAAAASUVORK5CYII=', 'base64');
  const image = await bridge.picture({data: png.toString('base64'), session: 'session1'});
  assert.equal(sent.length, 1);
  assert.equal(sent[0][0], 'image');
  assert.equal(sent[0][3], `${image.id}.png`);
});
test('keyboard button callbacks decide the matching approval and ack the interaction', async t => {
  const {bridge, body, sent} = fixture(t);
  const {id} = await bridge.register(body);
  await bridge.interaction({id: 'i1', type: 11, user_openid: 'owner', data: {resolved: {button_data: `approve:${id}`}}});
  assert.equal(bridge.pending.get(id).result.outcome.optionId, 'YES');
  assert.deepEqual(sent.find(s => s[0] === 'interactionAck').slice(1), ['i1', 0]);
  assert.match(sent.at(-1)[2], /决定已记录/);
  const {id: id2} = await bridge.register({...body, connection: 'connection2', request_key: '22'});
  await bridge.interaction({id: 'i2', type: 11, user_openid: 'owner', data: {resolved: {button_data: `reject:${id2}`}}});
  assert.equal(bridge.pending.get(id2).result.outcome.optionId, 'NO');
});
test('keyboard callbacks refuse unbound users, malformed data and duplicates', async t => {
  const {bridge, body, sent} = fixture(t);
  const {id} = await bridge.register(body);
  await bridge.interaction({id: 'i3', type: 11, user_openid: 'stranger', data: {resolved: {button_data: `approve:${id}`}}});
  assert.equal(bridge.pending.get(id).result, null);
  assert.deepEqual(sent.find(s => s[0] === 'interactionAck' && s[1] === 'i3').slice(1), ['i3', 4]);
  await bridge.interaction({id: 'i4', type: 11, user_openid: 'owner', data: {resolved: {button_data: 'garbage'}}});
  assert.equal(bridge.pending.get(id).result, null);
  assert.deepEqual(sent.find(s => s[0] === 'interactionAck' && s[1] === 'i4').slice(1), ['i4', 0]);
  await bridge.interaction({id: 'i5', type: 11, user_openid: 'owner', data: {resolved: {button_data: `approve:${id}`}}});
  assert.equal(bridge.pending.get(id).result.outcome.optionId, 'YES');
  await bridge.interaction({id: 'i6', type: 11, user_openid: 'owner', data: {resolved: {button_data: `approve:${id}`}}});
  assert.deepEqual(sent.find(s => s[0] === 'interactionAck' && s[1] === 'i6').slice(1), ['i6', 3]);
});
test('loopback API authenticates, denies browser origins, invalidates on unbind', async t => {
  const {bridge} = fixture(t); const server = await startLocal(bridge, 'TEST_LOCAL_TOKEN');
  t.after(() => new Promise(resolve => server.close(resolve)));
  const base = `http://127.0.0.1:${server.address().port}`;
  assert.equal((await fetch(base + '/status')).status, 403);
  assert.equal((await fetch(base + '/status', {headers: {Authorization: 'Bearer TEST_LOCAL_TOKEN', Origin: 'https://evil.test'}})).status, 403);
  const headers = {Authorization: 'Bearer TEST_LOCAL_TOKEN'};
  assert.equal((await (await fetch(base + '/status', {headers})).json()).online, true);
  const paired = await (await fetch(base + '/pair', {method: 'POST', headers, body: '{}'})).json();
  assert.match(paired.code, /^[A-F0-9]{12}$/);
  await fetch(base + '/unbind', {method: 'POST', headers, body: '{}'});
  assert.equal(bridge.binding.user_openid, undefined);
});
test('token single-flight refresh checks business errors even on HTTP 200', async () => {
  let calls = 0;
  const api = new QQApi({app_id: 'id', app_secret: 'secret'}, async () => {
    calls++; return new Response(JSON.stringify({access_token: 'TOKEN', expires_in: '7200'}));
  });
  assert.deepEqual(await Promise.all([api.accessToken(), api.accessToken()]), ['TOKEN','TOKEN']);
  assert.equal(calls, 1);
  const bad = new QQApi({app_id: 'id', app_secret: 'secret'}, async () => new Response(JSON.stringify({code: 100016, message: 'SECRET'})));
  await assert.rejects(bad.accessToken(), /QQ_CODE_100016/);
  const errCode = new QQApi({app_id: 'id', app_secret: 'secret'}, async () => new Response(JSON.stringify({err_code: 40034105, message: 'SECRET'})));
  await assert.rejects(errCode.accessToken(), /QQ_CODE_40034105/);
});
test('multipart image upload never exposes bot auth to storage and validates all parts', async () => {
  const requests = []; const bytes = Buffer.from('12345');
  const api = new QQApi({app_id: 'id', app_secret: 'secret'}, async (url, options) => {
    requests.push({url: String(url), options});
    let result = {};
    if (String(url).endsWith('getAppAccessToken')) result = {access_token: 'TOKEN', expires_in: 7200};
    else if (String(url).endsWith('upload_prepare')) result = {upload_id: 'u', block_size: '3',
      parts: [{index: 0, presigned_url: 'https://upload.cos.myqcloud.com/0'}, {index: 1, presigned_url: 'https://upload.cos.myqcloud.com/1'}]};
    else if (String(url).endsWith('/files')) result = {file_info: 'FILE_INFO'};
    else if (String(url).endsWith('/messages')) result = {id: 'sent'};
    return new Response(JSON.stringify(result));
  });
  await api.image('owner', bytes, 'test.png');
  const puts = requests.filter(r => r.options.method === 'PUT');
  assert.equal(puts.length, 2); assert.equal(puts[0].options.headers, undefined);
  assert.equal(Buffer.concat(puts.map(r => r.options.body)).toString(), '12345');
  assert.equal(JSON.parse(requests.at(-1).options.body).media.file_info, 'FILE_INFO');
});
test('multipart upload accepts server 1-based numeric/string indices and acknowledges the same index', async () => {
  for (const index of [1, '1', '0']) {
    const finished = [];
    const api = new QQApi({app_id: 'id', app_secret: 'secret'}, async (url, options) => {
      const route = String(url);
      let result = {};
      if (route.endsWith('getAppAccessToken')) result = {access_token: 'TOKEN', expires_in: 7200};
      else if (route.endsWith('upload_prepare')) result = {upload_id: 'u', block_size: '3', parts: [{index, presigned_url: 'https://upload.cos.myqcloud.com/part', block_size: '3'}]};
      else if (route.endsWith('upload_part_finish')) finished.push(JSON.parse(options.body));
      else if (route.endsWith('/files')) result = {file_info: 'FILE_INFO'};
      else if (route.endsWith('/messages')) result = {id: 'sent'};
      return new Response(JSON.stringify(result));
    });
    await api.image('owner', Buffer.from('123'), 'test.png');
    assert.equal(finished[0].part_index, Number(index));
    assert.equal(api.uploadDiagnostic.stage, 'sent');
  }
});

test('gateway identifies C2C intent and dispatches messages after READY', async t => {
  const {bridge, api, sent} = fixture(t); bridge.online = false;
  api.call = async () => ({url: 'wss://api.bot.qq.com/websocket/'});
  api.accessToken = async () => 'TEST_TOKEN'; api.expires = Date.now() + 100000;
  const stopping = {value: false}; const NativeSocket = globalThis.WebSocket;
  const frames = [];
  class FakeSocket extends EventTarget {
    constructor() {super(); setImmediate(() => this.receive({op: 10, d: {heartbeat_interval: 45000}}));}
    receive(frame) {this.dispatchEvent(new MessageEvent('message', {data: JSON.stringify(frame)}));}
    send(text) {
      const frame = JSON.parse(text); frames.push(frame);
      if (frame.op === 2) setImmediate(() => {
        this.receive({op: 0, t: 'READY', s: 1, d: {session_id: 'gateway-session'}});
        this.receive({op: 0, t: 'C2C_MESSAGE_CREATE', s: 2, d: event('状态')});
        setImmediate(() => {stopping.value = true; this.close();});
      });
    }
    close() {const e = new Event('close'); e.code = 1000; this.dispatchEvent(e);}
  }
  globalThis.WebSocket = FakeSocket;
  try {await gateway(bridge, stopping);} finally {globalThis.WebSocket = NativeSocket;}
  assert.equal(frames[0].op, 2); assert.equal(frames[0].d.intents, (1 << 25) | (1 << 26));
  assert.equal(frames[0].d.token, 'QQBot TEST_TOKEN');
  assert.match(sent.at(-1)[2], /在线/); assert.equal(bridge.online, false);
});

test('real stdio proxy + local HTTP + QQ reply round trip', {timeout: 15000}, async t => {
  const {root, bridge} = fixture(t);
  fs.writeFileSync(path.join(root, 'qq/local-token'), 'TOKEN');
  const server = await startLocal(bridge, 'TOKEN');
  t.after(() => new Promise(resolve => server.close(resolve)));
  const fake = path.join(root, 'fake-agent.py');
  fs.writeFileSync(fake, `import json,sys
for line in sys.stdin:
 m=json.loads(line)
 if m.get('method')=='session/prompt':
  print(json.dumps({'jsonrpc':'2.0','id':77,'method':'session/request_permission','params':{'sessionId':'session1','toolCall':{'title':'test command'},'options':[{'kind':'allow_once','optionId':'actual-permission'}]}}),flush=True)
 elif m.get('id')==77:
  print(json.dumps({'jsonrpc':'2.0','id':2,'result':m['result']}),flush=True)
`);
  const proxy = fileURLToPath(new URL('../scripts/acp_qq_proxy.py', import.meta.url));
  const child = spawn('python3', [proxy, '--root', root, '--', 'python3', fake], {stdio: ['pipe','pipe','pipe']});
  t.after(() => {child.stdin.end(); child.kill();});
  child.stdin.write(JSON.stringify({jsonrpc: '2.0', id: 2, method: 'session/prompt'}) + '\n');
  const output = new Promise((resolve, reject) => {
    let buffer = '';
    child.stdout.on('data', data => {buffer += data; if (buffer.includes('\n')) resolve(JSON.parse(buffer.split('\n')[0]));});
    child.once('error', reject); child.once('exit', status => {if (status) reject(new Error('Proxy exited'));});
  });
  for (let i = 0; i < 100 && !bridge.pending.size; i++) await new Promise(r => setTimeout(r, 25));
  const pending = [...bridge.pending.values()][0]; assert.ok(pending);
  await bridge.message(event('同意 ' + pending.id));
  const result = await output;
  assert.equal(result.id, 2); assert.equal(result.result.outcome.optionId, 'actual-permission');
  assert.equal(bridge.pending.get(pending.id).result.outcome.outcome, 'selected');
});
