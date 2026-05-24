import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

suite('Graph Agent extension metadata tests', () => {
  test('package.json 里声明了核心贡献点', async () => {
    const packageJson = JSON.parse(await readFile(new URL('../../package.json', import.meta.url), 'utf8'));

    const commandIds = (packageJson.contributes?.commands ?? []).map((command) => command.command);
    const viewIds = (packageJson.contributes?.views?.['vscode-graph-agent'] ?? []).map((view) => view.id);
    const chatParticipantIds = (packageJson.contributes?.chatParticipants ?? []).map((participant) => participant.id);

    assert.equal(packageJson.main, './src/extension.js');
    assert.ok(commandIds.includes('vscode-graph-agent.openSidebar'));
    assert.ok(commandIds.includes('graph-agent.showHistory'));
    assert.ok(commandIds.includes('graph-agent.showStatus'));
    assert.ok(viewIds.includes('vscode-graph-agent.sidebar'));
    assert.ok(chatParticipantIds.includes('vscode-graph-agent.workspace'));
  });
});