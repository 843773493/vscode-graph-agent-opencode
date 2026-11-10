import { expect, test } from 'bun:test';
import { readFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import ts from 'typescript';
import { pinnedUpstreamCommit, sourceTransform } from './sourceTransform.mjs';
import { chatExtensionCompletionSourcePaths } from './chatExtensionCompletionTransform.mjs';

const root = resolve(process.cwd(), 'src/clients/workbench');

test('聊天输入继续提供 Agent 补全并移除 Gallery 扩展安装建议', async () => {
  expect(chatExtensionCompletionSourcePaths).toEqual([
    'src/vs/workbench/contrib/chat/browser/widget/input/editor/chatInputCompletions.ts',
  ]);
  const path = join(root, 'upstream', chatExtensionCompletionSourcePaths[0]);
  const transform = sourceTransform(root, { commit: pinnedUpstreamCommit }, []);
  const parsed = ts.createSourceFile(path, await transform(path, await readFile(path, 'utf8')), ts.ScriptTarget.Latest, true);
  const converted = parsed.getText();
  expect(parsed.parseDiagnostics).toEqual([]);
  expect(converted).not.toContain("_debugDisplayName: 'installChatExtensions'");
  expect(converted).not.toContain('Install Chat Extensions...');
  expect(converted).not.toContain('@tag:chat-participant');
  expect(converted).toContain("_debugDisplayName: 'chatAgentAndSubcommand'");
  expect(converted).toContain("_debugDisplayName: 'mcpPromptSlashCommands'");
  expect(converted).toContain('registerWorkbenchContribution(SlashCommandCompletions');
  expect(converted).toContain('registerWorkbenchContribution(AgentCompletions');
});
