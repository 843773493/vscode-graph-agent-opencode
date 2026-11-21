import { expect, test, jest } from 'bun:test';
import React from 'react';
import { act, create, type ReactTestRenderer } from 'react-test-renderer';
import NodeDebugConsoleView, { DebugOutputContext } from './NodeDebugConsoleView';
import type { NodeDebugState } from '../../types/backend';

test('程序输出保留原控制序列交给宿主，更新和卸载时释放旧链接', () => {
  const dispose = jest.fn();
  const render = jest.fn(() => dispose);
  const host = {};
  const props = { status: 'paused' as const, actionBusy: false, extensionWindow: false,
    expression: '', setExpression: () => {}, onEvaluate: () => {} };
  const view = (output: string[]) => <DebugOutputContext.Provider value={render}>
    <NodeDebugConsoleView {...props} state={{ session_id: 'session-test', thread_id: 'thread-test', status: 'paused',
      configurations: [], args: [], call_stack: [], breakpoints: [], source_changed_paths: [],
      output, actions: [], evaluations: [] } satisfies NodeDebugState} />
  </DebugOutputContext.Provider>;
  let root!: ReactTestRenderer;
  act(() => { root = create(view(['\x1b[32m中文', 'https://example.com\x1b[0m']), { createNodeMock: () => host }); });
  expect(render).toHaveBeenLastCalledWith(host, '\x1b[32m中文\nhttps://example.com\x1b[0m');
  expect(dispose).not.toHaveBeenCalled();
  act(() => root.update(view(['下一条输出'])));
  expect(dispose).toHaveBeenCalledTimes(1);
  expect(render).toHaveBeenLastCalledWith(host, '下一条输出');
  act(() => root.unmount());
  expect(dispose).toHaveBeenCalledTimes(2);
});
