import { expect, test } from 'bun:test';
import { readFileSync } from 'node:fs';
import React, { useContext, useEffect, useRef, useState } from 'react';
import { act, create, type ReactTestRenderer } from 'react-test-renderer';
import ts from 'typescript';
import { t } from '../../../../shared/core/i18n/index.js';
import { failureSummary } from '../../../../shared/core/failurePresentation';
import { gatewayWorkspaceKey, nullableGatewayWorkspaceKey, workspaceOwnerFromKey } from '../../api/workspaceIdentity';
import { errorSummary } from '../../utils/errorMessage';
import { OverlayRootContext } from '../overlays/OverlayRootContext';

// 使用原组件和真实 React 生命周期；仅将 DOM portal 交给 renderer 的根。
function dialog(name: string): React.ComponentType<Record<string, unknown>> {
  const source = readFileSync(new URL(`./${name}.tsx`, import.meta.url), 'utf8')
    .replace(/^import .*;\n/gm, '').replace('export default function', 'function');
  const script = ts.transpileModule(source, { compilerOptions: {
    jsx: ts.JsxEmit.React, target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext,
  } }).outputText.replace(/^export \{\};?\n/gm, '');
  const environment = { React, useContext, useEffect, useRef, useState, t, failureSummary, errorSummary,
    gatewayWorkspaceKey, nullableGatewayWorkspaceKey, workspaceOwnerFromKey, OverlayRootContext,
    createPortal: (children: React.ReactNode) => children, document: { body: {} } };
  return new Function(...Object.keys(environment), `${script}; return ${name};`)(...Object.values(environment));
}

for (const name of ['WorkspaceRenameDialog', 'WorkspaceDetailsDialog']) {
  for (const result of ['success', 'failure']) test(`${name} 的 A→B→A 不接受旧 A 的${result}回执`, async () => {
    const Component = dialog(name), pending = Promise.withResolvers<string>();
    const workspaces = ['A', 'B'].map(id => ({ gateway_id: 'gateway-original', workspace_id: id, name: id,
      status: 'stopped', connection_kind: 'local', removable: false, affected_workspace_ids: [], remote_gateways: [] }));
    let closes = 0, calls = 0, renderer!: ReactTestRenderer;
    const operation = () => { calls++; return calls === 1 ? pending.promise : Promise.resolve('saved'); };
    const props = (index: number) => name === 'WorkspaceRenameDialog'
      ? { workspace: workspaces[index], onClose: () => { closes++; }, onSubmit: operation }
      : { target: { workspaceKey: gatewayWorkspaceKey(workspaces[index]), entryGatewayId: 'entry' }, workspaces,
        onClose: () => { closes++; }, onOpenLogs: operation };
    const submit = () => name === 'WorkspaceRenameDialog'
      ? renderer.root.findByType('form').props.onSubmit({ preventDefault() {} })
      : renderer.root.findAllByType('button').find(button => button.children.includes(t('gateway.workspaceDetails.logs')))!.props.onClick();
    await act(async () => { renderer = create(<Component {...props(0)} />); });
    try {
      await act(async () => { submit(); });
      await act(async () => { renderer.update(<Component {...props(1)} />); });
      await act(async () => { renderer.update(<Component {...props(0)} />); });
      await act(async () => {
        if (result === 'success') pending.resolve('old'); else pending.reject(new Error('旧 A 的完整失败原因'));
      });
      expect(closes).toBe(0);
      expect(renderer.root.findAllByProps({ role: 'alert' })).toHaveLength(0);
      await act(async () => { submit(); });
      expect(calls).toBe(2);
      expect(closes).toBe(1);
    } finally { act(() => renderer.unmount()); }
  });
}
