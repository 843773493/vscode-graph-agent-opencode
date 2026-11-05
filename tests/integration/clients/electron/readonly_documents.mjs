import { expect } from 'playwright/test';
import { selectThread } from '../web/persistent_tasks/workbench.mjs';

/** 同一便携产物的只读模型观察；历史由真实Agent工具执行生成。 */
export function readonlyDocumentProbe(reads, checks) {
  let phase = 'startup', target;
  const command = (page, id, ...args) => page.evaluate(({ id, args }) => globalThis.workbench.commands.executeCommand(id, ...args), { id, args });
  const groups = page => page.locator('.editor-group-container');
  const diff = (page, index) => groups(page).nth(index).locator('.monaco-diff-editor').filter({ visible: true });
  const source = page => groups(page).nth(0).locator('.editor-instance .monaco-editor').filter({ visible: true });
  const showDiff = async (page, index) => {
    await expect(diff(page, index).locator('.editor.modified .view-lines[role="presentation"]')).toContainText('NATIVE_HISTORY_LINE_01');
    await expect(diff(page, index).locator('.editor.original .view-lines[role="presentation"]')).toHaveText('');
  };
  const showSource = async page => {
    await expect(source(page)).toHaveCount(1);
    await expect(source(page).locator('.view-lines')).toContainText('NATIVE_READONLY_AGENT_SPEC');
  };
  return {
    observe(page) {
      page.on('request', request => {
        const path = new URL(request.url()).pathname;
        if (/\/changesets\/.+$/.test(path)) reads.push({ phase, kind: 'change', path, gatewayId: request.headers()['x-boxteam-target-gateway-id'], workspaceId: request.headers()['x-boxteam-workspace-id'] });
        if (path === '/api/v1/resources/navigation' && request.postDataJSON()?.operation === 'read') {
          reads.push({ phase, kind: 'source', uri: request.postDataJSON().uri, gatewayId: request.headers()['x-boxteam-target-gateway-id'], workspaceId: request.headers()['x-boxteam-workspace-id'] });
        }
      });
    },
    async prepare(page, { owner, gatewayId, turnId }) {
      target = { gatewayId, workspaceId: owner.workspace_id, sessionId: owner.session_id, threadId: owner.thread_id,
        changesetPath: `/api/v1/sessions/${owner.session_id}/changesets/${encodeURIComponent('turn:' + turnId)}` };
      const thread = await selectThread(page, target, 0);
      const toggle = thread.getByRole('button', { name: '切换会话资源', exact: true });
      if (await toggle.getAttribute('aria-expanded') !== 'true') await toggle.click();
      await thread.getByRole('tab', { name: '更改', exact: true }).click();
      const changeset = thread.locator(`.session-changeset-row[title="显示任务 ${turnId} 中产生的文件变更"]`);
      await changeset.click();
      await expect(changeset).toHaveAttribute('aria-pressed', 'true');
      await expect(thread.locator('.session-change-file-main')).toHaveCount(1);
      phase = 'open-diff';
      await thread.locator('.session-change-file-main').click();
      await showDiff(page, 0);
      expect(reads.filter(read => read.phase === phase && read.path === target.changesetPath)).toHaveLength(1);
      phase = 'split-diff';
      await command(page, 'workbench.action.splitEditorRight');
      await expect(groups(page)).toHaveCount(2);
      await showDiff(page, 0); await showDiff(page, 1);
      expect(reads.filter(read => read.phase === phase && read.kind === 'change')).toEqual([]);
      const input = diff(page, 1).locator('.editor.modified').getByRole('textbox');
      await input.focus(); await page.keyboard.insertText('NATIVE_UNEXPECTED_CHANGE');
      await showDiff(page, 1);
      await expect(diff(page, 1)).not.toContainText('NATIVE_UNEXPECTED_CHANGE');
      checks.readonlyDiffShared = true;
      phase = 'open-source';
      await command(page, 'workbench.action.focusFirstEditorGroup');
      await command(page, 'workbench.action.quickOpen', `boxteam://${gatewayId}/workspace/${owner.workspace_id}/resources/agent-spec/root/AGENTS.md`);
      await page.locator('.quick-input-widget').getByText('打开链接', { exact: true }).click();
      await showSource(page);
      await source(page).getByRole('textbox').focus(); await page.keyboard.insertText('NATIVE_UNEXPECTED_CHANGE');
      await expect(source(page)).not.toContainText('NATIVE_UNEXPECTED_CHANGE');
      expect(reads.filter(read => read.phase === phase && read.kind === 'source')).toHaveLength(1);
      await selectThread(page, target, 0);
      await showDiff(page, 1);
      phase = 'restart';
    },
    async restored(page) {
      await expect(groups(page)).toHaveCount(2);
      await showDiff(page, 1);
      expect(reads.filter(read => read.phase === 'restart' && read.path === target.changesetPath)).toHaveLength(1);
      const tab = groups(page).nth(0).locator('.tabs-container .tab[data-resource-name="AGENTS.md"]');
      await expect(tab).toHaveCount(1);
      await tab.click(); await showSource(page);
      expect(reads.filter(read => read.phase === 'restart' && read.kind === 'source')).toHaveLength(1);
      expect(reads.filter(read => ['open-diff', 'open-source', 'restart'].includes(read.phase)).every(read => read.gatewayId === target.gatewayId && read.workspaceId === target.workspaceId)).toBe(true);
      checks.readonlyDocumentsRestored = true;
      await selectThread(page, target, 0);
      phase = 'independent-profile';
    },
    async isolated(page) {
      await expect(page.locator('.monaco-diff-editor')).toHaveCount(0);
      await expect(page.locator('.tabs-container .tab[data-resource-name="AGENTS.md"]')).toHaveCount(0);
      expect(reads.filter(read => read.phase === 'independent-profile')).toEqual([]);
      checks.readonlyProfileIsolation = true;
    },
  };
}
