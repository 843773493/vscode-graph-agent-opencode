import { expect, test } from 'bun:test';
import { readFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import ts from 'typescript';
import { pinnedUpstreamCommit, sourceTransform } from './sourceTransform.mjs';
import { testingExtensionSourcePaths } from './testingExtensionTransform.mjs';

const root = resolve(process.cwd(), 'src/clients/workbench');
const transform = sourceTransform(root, { commit: pinnedUpstreamCommit }, []);

async function converted(relativePath: string): Promise<ts.SourceFile> {
  const path = join(root, 'upstream', relativePath);
  const source = await transform(path, await readFile(path, 'utf8'));
  const parsed = ts.createSourceFile(path, source, ts.ScriptTarget.Latest, true);
  expect(parsed.parseDiagnostics).toEqual([]);
  return parsed;
}

test('测试视图不再提供 Gallery 扩展搜索入口', async () => {
  expect(testingExtensionSourcePaths).toEqual([
    'src/vs/workbench/contrib/testing/common/constants.ts',
    'src/vs/workbench/contrib/testing/browser/testExplorerActions.ts',
    'src/vs/workbench/contrib/testing/browser/testing.contribution.ts',
  ]);

  const [constants, actions, contribution] = await Promise.all(testingExtensionSourcePaths.map(converted));
  const enumDeclaration = constants.statements.find(statement => ts.isEnumDeclaration(statement) && statement.name.text === 'TestCommandId');
  expect(enumDeclaration && ts.isEnumDeclaration(enumDeclaration)
    ? enumDeclaration.members.map(member => member.name.getText(constants)) : []).not.toContain('SearchForTestExtension');
  expect(actions.statements.some(statement => ts.isClassDeclaration(statement) && statement.name?.text === 'SearchForTestExtension')).toBe(false);
  expect(actions.statements.some(statement => ts.isImportDeclaration(statement)
    && statement.moduleSpecifier.getText(actions) === "'../../extensions/common/extensions.js'")).toBe(false);
  expect(contribution.getText()).not.toContain('Install Additional Test Extensions...');
  expect(contribution.getText()).toContain('No tests have been found in this workspace yet.');
});
