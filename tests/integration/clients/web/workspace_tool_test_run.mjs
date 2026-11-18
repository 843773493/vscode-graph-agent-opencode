import assert from 'node:assert/strict';
import { writeFile } from 'node:fs/promises';
import path from 'node:path';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';

const fixture = JSON.parse(process.env.BOXTEAM_TOOL_TEST_FIXTURE);
const modelRef = { provider_id: fixture.providerId, model_id: fixture.modelId };
const result = { phase: 'initialization', pageErrors: [], starts: [] };
let browser;
try {
  browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH, headless: true });
  const page = await browser.newPage({ viewport: { width: 1360, height: 900 } });
  page.setDefaultTimeout(15000);
  page.on('pageerror', error => result.pageErrors.push(error.message));
  page.on('request', request => {
    if (request.method() === 'POST' && /\/api\/v1\/tools\/[^/]+\/tests$/.test(new URL(request.url()).pathname)) {
      const headers = request.headers();
      result.starts.push({ body: request.postDataJSON(), gatewayId: headers['x-boxteam-target-gateway-id'],
        workspaceId: headers['x-boxteam-workspace-id'] });
    }
  });
  const command = (id, ...args) => page.evaluate(({ id, args }) => globalThis.workbench.commands.executeCommand(id, ...args), { id, args });
  const api = target => page.evaluate(async ({ target, fixture }) => {
    const credential = (await (await fetch('/api/gateway/auth/local-credential')).json()).data;
    const response = await fetch(target, { headers: { 'X-Local-Token': credential.token,
      'X-BoxTeam-Target-Gateway-Id': fixture.gatewayId, 'X-BoxTeam-Workspace-Id': fixture.workspaceId } });
    const envelope = await response.json();
    if (!response.ok || envelope.code !== 0 || !envelope.request_id
      || envelope.request_id !== response.headers.get('X-Request-ID')) throw new Error(`${target}: ${response.status}`);
    return envelope.data;
  }, { target, fixture });
  const ready = async () => {
    await expect(page.locator('#workbench .monaco-workbench')).toBeVisible({ timeout: 30000 });
    await page.waitForFunction(() => !!globalThis.workbench?.commands?.executeCommand);
    for (const url of ['/api/gateway/health', '/api/gateway/workspaces', '/api/v1/workspace']) await api(url);
    await command('boxteam.tools.focus');
  };
  await page.goto(fixture.baseUrl, { waitUntil: 'domcontentloaded' });
  await ready();
  const panel = page.locator('[data-panel-kind="tools"]');
  const row = panel.locator('.composer-tool-item').filter({ has: page.locator('strong', { hasText: /^edit_file$/ }) });
  await expect(row).toBeVisible();
  const agentId = await panel.locator('.native-workspace-tools').getAttribute('data-agent-id');
  assert(agentId);
  result.phase = 'explicit-model-and-repetitions';
  await row.getByRole('button', { name: '测试', exact: true }).click();
  const dialog = panel.getByRole('dialog');
  await expect(dialog).toContainText('可能产生真实费用');
  const confirm = dialog.getByRole('button', { name: '开始测试', exact: true });
  await expect(confirm).toBeDisabled();
  await expect(dialog.getByText(`${fixture.providerId} / catalog-only · tool-test-replay-model`, { exact: true })).toHaveCount(0);
  await dialog.getByText(`${fixture.providerId} / ${fixture.modelId} · tool-test-replay-model`, { exact: true }).locator('input').check();
  await dialog.getByLabel('我了解这次测试可能产生模型费用', { exact: true }).check();
  await expect(confirm).toBeDisabled();
  await dialog.getByLabel('运行次数', { exact: true }).fill('2');
  assert.deepEqual(result.starts, []);
  const started = page.waitForResponse(response => response.request().method() === 'POST'
    && new URL(response.url()).pathname === '/api/v1/tools/edit_file/tests');
  await confirm.click();
  const response = await started;
  assert.equal(response.status(), 200, await response.text());
  const receipt = await response.json();
  assert.equal(receipt.request_id, response.headers()['x-request-id']);
  assert(['queued', 'running'].includes(receipt.data.status));
  const runId = receipt.data.run_id;
  assert(runId);
  assert.deepEqual(result.starts, [{ gatewayId: fixture.gatewayId, workspaceId: fixture.workspaceId,
    body: { agent_id: agentId, model_refs: [modelRef], repetitions: 2 } }]);
  result.phase = 'reload-original-run';
  await page.reload({ waitUntil: 'domcontentloaded' });
  await ready();
  await expect(row.locator('.tool-test-metric.completed')).toHaveText('成功率 10% · 2/20 通过', { timeout: 45000 });
  const run = await api(`/api/v1/tools/tests/${encodeURIComponent(runId)}`);
  assert.equal(run.run_id, runId);
  assert.equal(run.tool_name, 'edit_file');
  assert.equal(run.status, 'completed');
  assert.equal(run.repetitions, 2);
  assert.equal(run.progress, 100);
  assert.equal(run.attempts.length, 20);
  assert.equal(new Set(run.attempts.map(item => item.attempt_id)).size, 20);
  assert.deepEqual(run.models.map(item => [item.model_ref, item.status, item.completed, item.total, item.passed, item.failed]),
    [[modelRef, 'completed', 20, 20, 2, 18]]);
  for (const attempt of run.attempts) {
    assert.deepEqual(attempt.model_ref, modelRef);
    assert.equal(attempt.model_calls, 1);
    assert.equal(attempt.transient_retries, 0);
    assert.equal(attempt.tool_called, true);
    assert.equal(attempt.passed, attempt.case_id === 'edit_file_exact_multiline');
  }
  const runs = await api('/api/v1/tools/tests?tool_name=edit_file');
  assert.deepEqual(runs.items.map(item => item.run_id), [runId]);
  assert.equal(result.starts.length, 1, '重开观察只能恢复原 run，不能重新提交');
  assert.deepEqual(result.pageErrors, []);
  result.run = run;
  result.phase = 'completed';
  await page.screenshot({ path: path.join(fixture.artifacts, 'tool-test-completed.png') });
} catch (error) {
  result.error = error.stack ?? String(error);
  throw error;
} finally {
  try { await writeFile(path.join(fixture.artifacts, 'result.json'), JSON.stringify(result, null, 2) + '\n'); }
  finally { await browser?.close(); }
}
