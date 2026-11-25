import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { readFile, readdir, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';
import { planChatCalls, testControl } from '../../../../support/e2e_control/browser_client.mjs';

const artifacts = process.env.BOXTEAM_BROWSER_ARTIFACT_ROOT;
assert(artifacts, '必须指定正式产物目录');
const result = { phase: 'bootstrap', pageErrors: [], receipts: [],
  boundary: '真实 Workbench/Gateway/Backend，Provider 为原 model_stream 精确身份回放' };
let browser, context, page;
const gates = new Set();
try {
  browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH,
    headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  await context.tracing.start({ screenshots: true, snapshots: false, sources: false });
  page = await context.newPage();
  page.on('pageerror', error => result.pageErrors.push(String(error)));
  page.setDefaultTimeout(15000);
  await page.goto(process.env.BOXTEAM_BROWSER_BASE_URL, { waitUntil: 'domcontentloaded' });
  await expect(page.locator('.native-session-navigation')).toBeVisible({ timeout: 30000 });
  const command = (id, ...args) => page.evaluate(({ id, args }) => globalThis.workbench.commands.executeCommand(id, ...args), { id, args });
  await command('workbench.action.closeAllEditors');
  const api = async (path, init = {}, status = 200) => {
    const received = await page.evaluate(async ({ path, init }) => {
      const token = (await (await fetch('/api/gateway/auth/local-credential')).json()).data.token;
      const requestId = crypto.randomUUID();
      const started = performance.now();
      const response = await fetch(path, { ...init, headers: { 'Content-Type': 'application/json',
        'X-Local-Token': token, 'X-Request-ID': requestId, ...init.headers } });
      const text = await response.text();
      return { status: response.status, body: JSON.parse(text), requestId,
        header: response.headers.get('X-Request-ID'), elapsedMs: performance.now() - started, bytes: new TextEncoder().encode(text).length };
    }, { path, init });
    result.receipts.push({ path, ...received });
    assert.equal(received.status, status, JSON.stringify(received));
    assert.equal(received.header, received.requestId);
    assert.equal(received.body.request_id, received.requestId);
    return received.body.data;
  };
  const health = await api('/api/gateway/health'), workspaces = await api('/api/gateway/workspaces');
  const owner = workspaces.active_workspace;
  assert.equal(owner.gateway_id, health.gateway_id);
  const headers = { 'X-BoxTeam-Target-Gateway-Id': owner.gateway_id, 'X-BoxTeam-Workspace-Id': owner.workspace_id };
  assert.equal((await api('/api/v1/workspace', { headers })).workspace_id, owner.workspace_id);
  const session = await api('/api/v1/sessions', { method: 'POST', headers, body: JSON.stringify({ title: '压缩与派生真实入口验收' }) });
  await api(`/api/v1/sessions/${session.session_id}`, { method: 'PATCH', headers,
    body: JSON.stringify({ model: { provider_id: 'primary', model_id: 'default' } }) });
  result.owner = { ...owner, session_id: session.session_id, thread_id: session.thread_id };
  const base = `/api/v1/sessions/${session.session_id}`, threadBase = `${base}/threads/${session.thread_id}`;
  const surface = (target = session) => page.locator(`section.boxteam-thread[data-session-id="${target.session_id}"][data-thread-id="${target.thread_id}"]`);
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(page.locator(`button[data-session-id="${session.session_id}"]`)).toBeVisible({ timeout: 30000 });
  await page.locator(`button[data-session-id="${session.session_id}"]`).click();
  await expect(surface()).toBeVisible({ timeout: 30000 });
  const send = async (text, target = session) => {
    const targetPath = `/api/v1/sessions/${target.session_id}/threads/${target.thread_id}/messages`;
    const composer = surface(target).locator('[data-testid="composer-input"]');
    await expect(composer).toBeEnabled({ timeout: 30000 });
    await composer.fill(text);
    const [response] = await Promise.all([
      page.waitForResponse(response => response.request().method() === 'POST' && new URL(response.url()).pathname === targetPath),
      surface(target).locator('[data-testid="composer-send"]').click(),
    ]);
    const envelope = await response.json();
    assert.equal(response.status(), 200, JSON.stringify(envelope));
    assert.equal(response.headers()['x-request-id'], envelope.request_id);
    assert.equal(response.request().headers()['x-boxteam-workspace-id'], owner.workspace_id);
    assert.equal(response.request().headers()['x-boxteam-target-gateway-id'], owner.gateway_id);
    return envelope.data;
  };
  const done = async (accepted, text, target = session) => {
    await expect.poll(async () => {
      const job = await api(`/api/v1/jobs/${accepted.job_id}`, { headers });
      assert(!['failed', 'cancelled'].includes(job.status), JSON.stringify(job));
      return job.status;
    }, { timeout: 30000 }).toBe('completed');
    await expect(surface(target).getByText(text, { exact: true })).toBeVisible({ timeout: 30000 });
  };
  const history = () => api(`${threadBase}/history`, { method: 'POST', headers, body: JSON.stringify({ direction: 'tail' }) });
  const information = async (target = session) => {
    const response = await api('/api/v1/context/read', { method: 'POST', headers, body: JSON.stringify({
      resource_identity: target.resource_identity, uri: target.uri, scope: target.scope, view: 'information',
    }) });
    return response.items.find(item => item.kind === 'information').data.compaction;
  };
  const planned = await planChatCalls(session.session_id, session.thread_id, [
    { label: 'compaction-history', text: '历史约束已记录。' },
    { label: 'compaction-current', text: '当前对话继续执行。' },
    { label: 'compaction-summary', text: '历史约束摘要：保留记录的关键规则。' },
    { label: 'compaction-concurrent', text: '后台准备期间的对话未被阻塞。' },
    { label: 'compaction-applied', text: '已使用完整历史摘要继续。' },
  ], 'browser-chat-tool-loop-stub', process.env.BOXTEAM_BROWSER_MODEL_ENDPOINT);
  await testControl({ action: 'arm', operation_id: planned[2], phase: 'model.response_ready' }); gates.add(planned[2]);
  result.phase = 'history-from-real-composer';
  await done(await send('历史事实与约束：' + 'HISTORICAL_RULE_'.repeat(4000)), '历史约束已记录。');
  const initialForkSource = await api(`${base}/fork-source-point`, { headers });
  // 用原预算估算器决定第二轮输入长度，模型容量从后端实际配置取得。
  const capture = JSON.parse(await readFile(join(process.env.BOXTEAM_BROWSER_MODEL_ARTIFACTS, `${planned[0]}.json`), 'utf8'));
  const config = JSON.parse(await readFile(process.env.BOXTEAM_BROWSER_CONFIG_PATH, 'utf8'));
  const capacity = config.profiles['provider.primary'].settings.model_options.api_mode.model_info.max_input_tokens;
  const projected = { ...capture.request, messages: [...capture.request.messages,
    { role: 'assistant', content: [{ type: 'text', text: '历史约束已记录。' }] }, { role: 'user', content: '现在处理新的独立目标。' }] };
  const budgetInput = JSON.parse(execFileSync('uv', ['run', 'python', '-c',
    `import json, sys
from app.agents.providers.request_budget import estimate_wire_input

data = json.load(sys.stdin)
wire = data["wire"]
base = wire["messages"][-1]["content"]
lower, upper = 0, data["capacity"] * 4
while lower < upper:
    middle = (lower + upper) // 2
    wire["messages"][-1]["content"] = base + "P" * middle
    if estimate_wire_input(wire).known_tokens < data["capacity"] * .82:
        lower = middle + 1
    else:
        upper = middle
wire["messages"][-1]["content"] = base + "P" * lower
print(json.dumps({"text": wire["messages"][-1]["content"], "tokens": estimate_wire_input(wire).known_tokens}))`],
  { input: JSON.stringify({ wire: projected, capacity }), encoding: 'utf8' }).trim());
  result.compaction = { capacity, tokens: budgetInput.tokens, calls: planned };
  result.phase = 'background-does-not-block-main';
  await done(await send(budgetInput.text), '当前对话继续执行。');
  await testControl({ action: 'acknowledged', operation_id: planned[2], phase: 'model.response_ready' });
  await done(await send('摘要生成时继续一个很小的任务。'), '后台准备期间的对话未被阻塞。');
  const preparing = await information();
  assert.equal(preparing.candidate?.status, 'preparing', JSON.stringify(preparing));
  assert.equal(preparing.candidate.task_status, 'running');
  await page.screenshot({ path: join(artifacts, '01-background-conversation.png') });
  await testControl({ action: 'release', operation_id: planned[2], phase: 'model.response_ready' }); gates.delete(planned[2]);
  await expect.poll(async () => {
    const status = await information();
    assert(!['failed', 'cancelled', 'reconcile_required'].includes(status.candidate?.task_status), JSON.stringify(status));
    return status.candidate?.status;
  }, { timeout: 30000 }).toBe('ready');
  const ready = await information();
  assert.equal(ready.thread_id, session.thread_id);
  const applied = await send('最终输入保留：' + 'N'.repeat(Math.ceil(capacity * 0.16 * 4)));
  await done(applied, '已使用完整历史摘要继续。');
  const state = await information();
  assert.equal(state.candidate.status, 'applied', JSON.stringify(state));
  result.compaction.ready = ready; result.compaction.applied = state; result.compaction.summaryCalls = 1;
  const compactionCaptures = (await readdir(process.env.BOXTEAM_BROWSER_MODEL_ARTIFACTS)).filter(name => name.endsWith('.json'));
  assert.deepEqual(compactionCaptures.sort(), planned.map(id => `${id}.json`).sort());
  await page.screenshot({ path: join(artifacts, '02-summary-applied.png') });

  result.phase = 'derived-http-control-and-owner';
  result.derived = { completed: [] };
  const mainCalls = await planChatCalls(session.session_id, session.thread_id, [
    { label: 'derived-main', text: 'DERIVED_PRIVATE_RESULT_MAIN' },
    { label: 'derived-cancelled', text: 'DERIVED_PRIVATE_RESULT_LATE' },
  ], 'browser-chat-tool-loop-stub', process.env.BOXTEAM_BROWSER_MODEL_ENDPOINT);
  const submission = { session_id: session.session_id, thread_id: session.thread_id,
    idempotency_key: 'gateway-derived-main', instruction: '仅分析冻结历史，返回独立结果。', timeout_seconds: 90,
    max_model_calls: 1, allowed_tool_ids: [] };
  const receipts = await Promise.all([0, 1].map(() => api('/api/v1/derived-executions', { method: 'POST', headers, body: JSON.stringify(submission) })));
  assert.equal(receipts[0].task_id, receipts[1].task_id);
  const query = (task, thread = session.thread_id, suffix = '') => `/api/v1/derived-executions/${task}?session_id=${session.session_id}&thread_id=${thread}${suffix}`;
  let completed;
  await expect.poll(async () => {
    completed = await api(query(receipts[0].task_id, session.thread_id, '&wait_seconds=1'), { headers });
    assert(!['failed', 'cancelled', 'reconcile_required'].includes(completed.status), JSON.stringify(completed));
    return completed.status;
  }, { timeout: 30000 }).toBe('completed');
  assert(completed.result.output_refs.length > 0 && completed.result.output_ref_count > 0);
  assert(completed.result.output_refs.every(ref => ref.session_id === session.session_id && ref.thread_id === session.thread_id));
  assert(!JSON.stringify(completed).includes('DERIVED_PRIVATE_RESULT'));
  const inspectOutput = async (receipt, text) => {
    const reference = { kind: 'execution_output', execution_id: receipt.result.execution_id, ...receipt.result.output_refs[0] };
    const path = `${base}/threads/${receipt.thread_id}/items/inspect`;
    const content = await api(path, { method: 'POST', headers, body: JSON.stringify({ operation: 'content', content_ref: reference }) });
    assert.deepEqual(content.content_ref, reference);
    assert.equal(content.location.content_hash, reference.content_hash);
    assert.equal(content.location.model_call_id, receipt.result.dispatch_ref.model_call_id);
    assert.equal(content.location.execution_id, receipt.result.execution_id);
    assert.equal(content.contents.map(value => value.content ?? '').join(''), text);
    const metadata = await api(path, { method: 'POST', headers, body: JSON.stringify({ operation: 'content_metadata', content_ref: reference }) });
    assert(metadata.contents.every(value => value.content == null));
    await api(path, { method: 'POST', headers, body: JSON.stringify({ operation: 'content', item_id: reference.item_id }) }, 404);
    await api(path, { method: 'POST', headers, body: JSON.stringify({ operation: 'content', content_ref: { kind: 'item', item_id: reference.item_id } }) }, 404);
    await api(path, { method: 'POST', headers, body: JSON.stringify({ operation: 'content', content_ref: { ...reference, content_hash: 'sha256:wrong' } }) }, 404);
    await api(path, { method: 'POST', headers, body: JSON.stringify({ operation: 'content', content_ref: {
      ...reference, item_id: receipt.source_last_ref.item_id, content_hash: receipt.source_last_ref.content_hash,
    } }) }, 404);
    await api(path, { method: 'POST', headers: { ...headers, 'X-BoxTeam-Workspace-Id': crypto.randomUUID() },
      body: JSON.stringify({ operation: 'content', content_ref: reference }) }, 404);
    return content;
  };
  result.derived.mainContent = await inspectOutput(completed, 'DERIVED_PRIVATE_RESULT_MAIN');
  result.derived.completed.push(completed);
  assert(!JSON.stringify(await history()).includes('DERIVED_PRIVATE_RESULT'));
  const search = await api(`${threadBase}/history/search`, { method: 'POST', headers,
    body: JSON.stringify({ query: 'DERIVED_PRIVATE_RESULT' }) });
  assert.deepEqual(search.items, []);
  const source = await api(`${base}/fork-source-point`, { headers });
  const fork = await api(`${base}/fork-context`, { method: 'POST', headers, body: JSON.stringify({
    idempotency_key: 'derived-scope-child', source_uri: source.source_uri, source_view_id: source.source_view_id,
    source_view_revision: source.source_view_revision, anchor_item_id: source.anchor_item_id,
    target_container_uri: source.child_container_uri, mode: 'view_prefix_fork', creation_kind: 'new_child_thread',
  }) });
  result.derived.childThreadId = fork.thread_id;
  assert(!JSON.stringify(await api(`${base}/threads/${fork.thread_id}/history`, { method: 'POST', headers,
    body: JSON.stringify({ direction: 'tail' }) })).includes('DERIVED_PRIVATE_RESULT'));
  await api(query(completed.task_id, fork.thread_id), { headers }, 409);
  await api(query(completed.task_id), { headers: { ...headers, 'X-BoxTeam-Workspace-Id': crypto.randomUUID() } }, 404);
  await planChatCalls(session.session_id, fork.thread_id, [{ label: 'derived-child', text: 'DERIVED_PRIVATE_RESULT_CHILD' }],
    'browser-chat-tool-loop-stub', process.env.BOXTEAM_BROWSER_MODEL_ENDPOINT);
  const child = await api('/api/v1/derived-executions', { method: 'POST', headers, body: JSON.stringify({
    ...submission, thread_id: fork.thread_id, idempotency_key: 'gateway-derived-child',
  }) });
  let childDone;
  await expect.poll(async () => {
    childDone = await api(query(child.task_id, fork.thread_id, '&wait_seconds=1'), { headers });
    assert(!['failed', 'cancelled', 'reconcile_required'].includes(childDone.status), JSON.stringify(childDone));
    return childDone.status;
  }, { timeout: 30000 }).toBe('completed');
  assert.equal(childDone.thread_id, fork.thread_id);
  assert(childDone.result.output_refs.every(ref => ref.session_id === session.session_id && ref.thread_id === fork.thread_id));
  result.derived.child = childDone;
  result.derived.childContent = await inspectOutput(childDone, 'DERIVED_PRIVATE_RESULT_CHILD');
  const mainReference = { kind: 'execution_output', execution_id: completed.result.execution_id, ...completed.result.output_refs[0] };
  await api(`${base}/threads/${fork.thread_id}/items/inspect`, { method: 'POST', headers,
    body: JSON.stringify({ operation: 'content', content_ref: mainReference }) }, 400);
  await api(`${base}/threads/${fork.thread_id}/items/inspect`, { method: 'POST', headers,
    body: JSON.stringify({ operation: 'content', content_ref: { ...mainReference, thread_id: fork.thread_id } }) }, 404);
  await api(query(child.task_id), { headers }, 409);
  await testControl({ action: 'arm', operation_id: mainCalls[1], phase: 'model.response_ready' }); gates.add(mainCalls[1]);
  const pending = await api('/api/v1/derived-executions', { method: 'POST', headers, body: JSON.stringify({
    ...submission, idempotency_key: 'gateway-derived-cancel',
  }) });
  await testControl({ action: 'acknowledged', operation_id: mainCalls[1], phase: 'model.response_ready' });
  const running = await api(query(pending.task_id, session.thread_id, '&wait_seconds=0.01'), { headers });
  assert.equal(running.status, 'running');
  const cancelled = await api(`/api/v1/derived-executions/${pending.task_id}/cancel`, { method: 'POST', headers,
    body: JSON.stringify({ session_id: session.session_id, thread_id: session.thread_id }) });
  assert.equal(cancelled.status, 'cancelled'); result.derived.cancelled = cancelled;
  await testControl({ action: 'release', operation_id: mainCalls[1], phase: 'model.response_ready' }); gates.delete(mainCalls[1]);
  assert.equal((await api(query(pending.task_id), { headers })).status, 'cancelled');
  assert(!JSON.stringify(await history()).includes('DERIVED_PRIVATE_RESULT'));
  await page.screenshot({ path: join(artifacts, '03-derived-isolated.png') });

  result.phase = 'capacity-failure-visible-in-real-turn';
  const beforeCapacityFailure = (await readdir(process.env.BOXTEAM_BROWSER_MODEL_ARTIFACTS)).filter(name => name.endsWith('.json')).sort();
  const failed = await send('必须完整保留的超窗输入：' + 'L'.repeat(Math.ceil(capacity * 4.1)));
  let failedJob;
  await expect.poll(async () => {
    failedJob = await api(`/api/v1/jobs/${failed.job_id}`, { headers });
    return failedJob.status;
  }, { timeout: 30000 }).toBe('failed');
  assert.match(failedJob.error_message, /context-capacity-exceeded/);
  await expect(surface().locator(`[data-turn-id="${failed.turn_id}"]`).locator('.chat-inline-error[role="alert"]')
    .filter({ hasText: 'context-capacity-exceeded' })).toBeVisible({ timeout: 30000 });
  assert.deepEqual((await readdir(process.env.BOXTEAM_BROWSER_MODEL_ARTIFACTS)).filter(name => name.endsWith('.json')).sort(), beforeCapacityFailure);
  result.compaction.capacityFailure = failedJob;
  await page.screenshot({ path: join(artifacts, '04-capacity-error-visible.png') });

  result.phase = 'ordinary-fork-delete-and-legal-cleanup';
  const ordinary = (await api(`${base}/fork-context`, { method: 'POST', headers, body: JSON.stringify({
    idempotency_key: 'derived-scope-ordinary', source_uri: initialForkSource.source_uri, source_view_id: initialForkSource.source_view_id,
    source_view_revision: initialForkSource.source_view_revision, anchor_item_id: initialForkSource.anchor_item_id,
    target_container_uri: initialForkSource.main_container_uri, mode: 'view_prefix_fork', creation_kind: 'new_session_main',
  }) })).session;
  assert.notEqual(ordinary.session_id, session.session_id);
  const foregroundCalls = await planChatCalls(ordinary.session_id, ordinary.thread_id, [
    { label: 'foreground-after-summary', text: '等待摘要完成后继续主执行。' },
    { label: 'foreground-summary', text: '前台完整历史摘要。' },
  ], 'browser-chat-tool-loop-stub', process.env.BOXTEAM_BROWSER_MODEL_ENDPOINT);
  // 主执行先预留 model_call_id，压缩内部调用后分配；按真实身份阻塞摘要。
  await testControl({ action: 'arm', operation_id: foregroundCalls[1], phase: 'model.response_ready' }); gates.add(foregroundCalls[1]);
  await expect(page.locator(`button[data-session-id="${ordinary.session_id}"]`)).toBeVisible({ timeout: 30000 });
  await page.locator(`button[data-session-id="${ordinary.session_id}"]`).click();
  await expect(surface(ordinary)).toBeVisible({ timeout: 30000 });
  const blocked = await send('新的强制保留输入：' + 'F'.repeat(Math.ceil(capacity * 0.22 * 4)), ordinary);
  await testControl({ action: 'acknowledged', operation_id: foregroundCalls[1], phase: 'model.response_ready' });
  assert.equal((await api(`/api/v1/jobs/${blocked.job_id}`, { headers })).status, 'running');
  assert(!(await readdir(process.env.BOXTEAM_BROWSER_MODEL_ARTIFACTS)).includes(`${foregroundCalls[0]}.json`));
  await testControl({ action: 'release', operation_id: foregroundCalls[1], phase: 'model.response_ready' }); gates.delete(foregroundCalls[1]);
  await done(blocked, '等待摘要完成后继续主执行。', ordinary);
  result.compaction.foregroundWithoutCandidate = { owner: ordinary, blocked, calls: foregroundCalls };
  const [deleteCall] = await planChatCalls(ordinary.session_id, ordinary.thread_id,
    [{ label: 'derived-delete', text: 'DERIVED_PRIVATE_RESULT_AFTER_DELETE' }],
    'browser-chat-tool-loop-stub', process.env.BOXTEAM_BROWSER_MODEL_ENDPOINT);
  await testControl({ action: 'arm', operation_id: deleteCall, phase: 'model.response_ready' }); gates.add(deleteCall);
  const deletingTask = await api('/api/v1/derived-executions', { method: 'POST', headers, body: JSON.stringify({
    ...submission, session_id: ordinary.session_id, thread_id: ordinary.thread_id, idempotency_key: 'gateway-derived-delete',
  }) });
  await testControl({ action: 'acknowledged', operation_id: deleteCall, phase: 'model.response_ready' });
  const catalog = await api('/api/v1/session-catalog/snapshot', { headers });
  const operation = `op_${crypto.randomUUID().replaceAll('-', '')}`;
  const acceptedDelete = await api('/api/v1/session-catalog/operations:enqueue', { method: 'POST', headers,
    body: JSON.stringify({ intents: [{ client_operation_id: operation, client_sequence: 1,
      kind: 'delete_session', target_node_id: ordinary.session_id, base_catalog_revision: catalog.catalog_revision }] }) }, 202);
  assert.equal(acceptedDelete.receipts[0].operation_id, operation);
  let deleted;
  await expect.poll(async () => {
    const status = await api(`/api/v1/session-catalog/operations?operation_id=${operation}`, { headers });
    deleted = status.items[0];
    assert(!['rejected', 'cancelled', 'dependency_failed'].includes(deleted.state), JSON.stringify(deleted));
    return deleted.settlement;
  }, { timeout: 30000 }).toBe('settled');
  assert.equal(deleted.state, 'committed');
  await testControl({ action: 'release', operation_id: deleteCall, phase: 'model.response_ready' }); gates.delete(deleteCall);
  await api(`/api/v1/derived-executions/${deletingTask.task_id}?session_id=${ordinary.session_id}&thread_id=${ordinary.thread_id}`, { headers }, 404);
  result.derived.deleted = { ...ordinary, receipt: deleted, task: deletingTask };

  result.phase = 'manual-summary-with-automatic-disabled';
  const sourcePath = `/api/v1/workspaces/${owner.workspace_id}/config/sources/workspace_root_mutable_override`;
  const document = await api(sourcePath, { headers });
  const manualConfig = JSON.parse(document.jsonc);
  manualConfig.settings.workspace.ui.auto_summarize = false;
  const changed = await api(sourcePath, { method: 'PUT', headers, body: JSON.stringify({
    gateway_id: owner.gateway_id, workspace_id: owner.workspace_id, source_key: 'workspace_root_mutable_override',
    jsonc: JSON.stringify(manualConfig), base_file_digest: document.file_digest,
    base_layer_revision: document.layer_revision, base_layer_digest: document.layer_digest,
    expected_active_revision: document.active_revision, expected_active_digest: document.active_digest,
    idempotency_key: crypto.randomUUID(),
  }) });
  assert.equal(changed.edit.state, 'active', JSON.stringify(changed));
  const manual = await api('/api/v1/sessions', { method: 'POST', headers, body: JSON.stringify({ title: '关闭自动压缩后的手动请求' }) });
  await api(`/api/v1/sessions/${manual.session_id}`, { method: 'PATCH', headers,
    body: JSON.stringify({ model: { provider_id: 'primary', model_id: 'default' } }) });
  const manualCalls = await planChatCalls(manual.session_id, manual.thread_id, [
    { label: 'manual-history', text: '手动压缩的旧事实已记录。' },
    { label: 'manual-current', text: '保持当前独立目标。' },
    { label: 'manual-summary', text: '手动摘要保留最初事实。' },
    { label: 'manual-applied', text: '下一正常请求已采用手动摘要。' },
  ], 'browser-chat-tool-loop-stub', process.env.BOXTEAM_BROWSER_MODEL_ENDPOINT);
  await expect(page.locator(`button[data-session-id="${manual.session_id}"]`)).toBeVisible({ timeout: 30000 });
  await page.locator(`button[data-session-id="${manual.session_id}"]`).click();
  await expect(surface(manual)).toBeVisible({ timeout: 30000 });
  await done(await send('最初事实：' + 'MANUAL_OLD_FACT_'.repeat(256), manual), '手动压缩的旧事实已记录。', manual);
  await done(await send('现在保持一个很小的当前目标。', manual), '保持当前独立目标。', manual);
  const manualCapture = JSON.parse(await readFile(join(process.env.BOXTEAM_BROWSER_MODEL_ARTIFACTS, `${manualCalls[1]}.json`), 'utf8'));
  const manualTokens = Number(execFileSync('uv', ['run', 'python', '-c',
    'import json,sys; from app.agents.providers.request_budget import estimate_wire_input; print(estimate_wire_input(json.load(sys.stdin)).known_tokens)'],
  { input: JSON.stringify(manualCapture.request), encoding: 'utf8' }).trim());
  assert(manualTokens < capacity * .8, `${manualTokens}/${capacity}`);
  const beforeManual = await information(manual);
  assert.equal(beforeManual.automatic, false);
  assert.equal(beforeManual.candidate, null);
  await testControl({ action: 'arm', operation_id: manualCalls[2], phase: 'model.response_ready' }); gates.add(manualCalls[2]);
  const requestedManual = await api(`/api/v1/sessions/${manual.session_id}/compact?thread_id=${manual.thread_id}`, { method: 'POST', headers });
  assert.equal(requestedManual.status, 'scheduled');
  assert.equal(requestedManual.thread_id, manual.thread_id);
  await testControl({ action: 'acknowledged', operation_id: manualCalls[2], phase: 'model.response_ready' });
  assert(!(await readdir(process.env.BOXTEAM_BROWSER_MODEL_ARTIFACTS)).includes(`${manualCalls[3]}.json`));
  await testControl({ action: 'release', operation_id: manualCalls[2], phase: 'model.response_ready' }); gates.delete(manualCalls[2]);
  await expect.poll(async () => {
    const status = await information(manual);
    assert(!['failed', 'cancelled', 'reconcile_required'].includes(status.candidate?.task_status), JSON.stringify(status));
    return status.candidate?.status;
  }, { timeout: 30000 }).toBe('ready');
  const manualReady = await information(manual);
  assert.equal(manualReady.candidate.task_id, requestedManual.task_id);
  await done(await send('手动摘要准备好后的下一正常输入。', manual), '下一正常请求已采用手动摘要。', manual);
  const manualApplied = await information(manual);
  assert.equal(manualApplied.automatic, false);
  assert.equal(manualApplied.candidate.status, 'applied', JSON.stringify(manualApplied));
  assert.equal(manualApplied.candidate.task_id, requestedManual.task_id);
  const manualRequest = JSON.parse(await readFile(join(process.env.BOXTEAM_BROWSER_MODEL_ARTIFACTS, `${manualCalls[3]}.json`), 'utf8')).request;
  assert(JSON.stringify(manualRequest).includes('手动摘要保留最初事实。'));
  assert(!JSON.stringify(manualRequest).includes('MANUAL_OLD_FACT_'));
  result.compaction.manual = { owner: manual, tokens: manualTokens, requested: requestedManual,
    ready: manualReady, applied: manualApplied, calls: manualCalls };
  await page.screenshot({ path: join(artifacts, '05-manual-summary-applied.png') });
  result.phase = 'complete';
} catch (error) {
  result.error = String(error?.stack ?? error);
  if (page) await page.screenshot({ path: join(artifacts, 'failure.png') });
  throw error;
} finally {
  for (const id of gates) await testControl({ action: 'release', operation_id: id, phase: 'model.response_ready' });
  if (context) await context.tracing.stop({ path: join(artifacts, 'browser-trace.zip') });
  await writeFile(process.env.BOXTEAM_BROWSER_RESULT_PATH, JSON.stringify(result, null, 2) + '\n');
  if (context) await context.close();
  if (browser) await browser.close();
}
