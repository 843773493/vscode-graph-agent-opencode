import { expect, test } from 'bun:test';
import ts from '../../src/clients/web/node_modules/typescript/lib/typescript.js';
import { readdir, readFile } from 'node:fs/promises';
import path from 'node:path';

async function sources(root) {
  const entries = await readdir(root, { withFileTypes: true });
  const files = await Promise.all(entries.map(entry => {
    const file = path.join(root, entry.name);
    return entry.isDirectory() ? sources(file) : [file];
  }));
  return files.flat().filter(file => /\.(js|mjs|ts|tsx)$/.test(file) && !/\.test\./.test(file)
    && !file.includes('/types/') && !file.includes('/tests/'));
}

test('常驻业务事件连接只能由统一实时入口创建，有限请求流与专用协议显式登记', async () => {
  const roots = ['src/clients/web/src', 'src/clients/workbench/src', 'src/workspace-services/terminal/client'];
  // DAP 与终端字节是专用协议；它们不能被误算成业务事件连接。
  const dedicatedSockets = new Set(['src/clients/workbench/src/debug/adapter.ts', 'src/workspace-services/terminal/client/terminalConnection.js']);
  const finiteStreams = new Set(['api/configSources.ts', 'api/stream/federatedSearch.ts', 'api/stream/realtime.ts']);
  const failures = [];
  for (const file of (await Promise.all(roots.map(sources))).flat()) {
    const projectRelative = path.relative(process.cwd(), file);
    const relative = path.relative(path.resolve(roots[0]), file);
    const tree = ts.createSourceFile(file, await readFile(file, 'utf8'), ts.ScriptTarget.Latest, true);
    function inspect(node) {
      if (ts.isNewExpression(node) && /(?:^|\.)(?:WebSocket|EventSource)$/.test(node.expression.getText(tree))
        && !dedicatedSockets.has(projectRelative)) failures.push(projectRelative + ': 独立事件连接');
      if (ts.isCallExpression(node) && node.expression.getText(tree) === 'consumeSseResponse'
        && !finiteStreams.has(relative) && projectRelative !== 'src/workspace-services/terminal/client/main.js') failures.push(projectRelative + ': 绕过统一订阅入口');
      ts.forEachChild(node, inspect);
    }
    inspect(tree);
  }
  expect(failures).toEqual([]);
});
