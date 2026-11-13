import React from 'react';
import { expect, test } from 'bun:test';
import { renderToStaticMarkup } from 'react-dom/server';
import { t } from '../../../../shared/core/i18n/index.js';
import type { ConfigSourceDocumentDTO } from '../../types/protocol_generated/boxteam/workspace/v2/public.js';
import { ConfigurationEntities } from './ConfigurationEntities';

function sourceDocument(): ConfigSourceDocumentDTO {
  return { gateway_id: 'gateway-a', workspace_id: 'workspace-a', source_key: 'workspace', layer: 'workspace', writable: true,
    jsonc: '{}', file_presence: 'present', file_digest: undefined, layer_revision: undefined, layer_digest: undefined,
    source_generation: undefined, active_revision: undefined, active_digest: undefined, json_schema: {}, status: undefined,
    effective: { agents: { coder: { name: '当前运行显示名' } } },
    source_effective: { agents: { coder: { name: '保存前的本层覆盖' } } }, origins: [],
    layers: [
      { source_key: 'user', layer: 'user', precedence: 1, writable: true, payload: { agents: { coder: {
        name: '下层显示名', instructions: { system_prompt: '下层提示词' }, model: { fallback_providers: ['backup-a', 'backup-b'] },
      } } } },
      { source_key: 'workspace', layer: 'workspace', precedence: 2, writable: true, payload: { agents: { coder: { name: '保存前的本层覆盖' } } } },
      { source_key: 'runtime', layer: 'runtime_override', precedence: 3, writable: false, payload: { agents: { coder: { name: '更高层显示名' } } } },
    ] };
}

function render(draft: Record<string, unknown>, document = sourceDocument()) {
  return renderToStaticMarkup(<ConfigurationEntities kind="agents" selectedId="coder" draft={draft} document={document}
    onSelect={() => {}} onEdit={() => {}} onCreate={() => {}} onRemove={() => {}} onRestoreProviders={() => {}}
    onSetDefaultAgent={() => {}} onOpenSource={() => {}} onSave={() => {}} />);
}

test('移除未保存的本层 Agent 覆盖立即显示下层正文，不能继续展示旧覆盖或更高层值', () => {
  const html = render({ agents: {} });
  expect(html).toContain('value="下层显示名"');
  expect(html).toContain('下层提示词');
  expect(html).toContain('backup-a\nbackup-b');
  expect(html).toContain('继承自 user');
  expect(html).not.toContain('保存前的本层覆盖');
  expect(html).not.toContain('更高层显示名');
});

test('局部 Agent 覆盖保留缺省字段的下层提示，fallback 按每行一个 ID 展示', () => {
  const html = render({ agents: { coder: { name: '尚未保存的显示名' } } });
  expect(html).toContain('value="尚未保存的显示名"');
  expect(html).toContain('placeholder="下层提示词"');
  expect(html).toContain('placeholder="backup-a\nbackup-b"');
  expect(html).not.toContain('[&quot;backup-a&quot;');
});

test('显式打开已移除的 Agent 不得误展示第一条定义', () => {
  const document = sourceDocument();
  document.layers = [];
  const html = render({ agents: { another: { name: '另一份定义' } } }, document);
  expect(html).toContain(t('configuration.entities.definitionChanged', 'coder'));
  expect(html).not.toContain('data-definition-id="another"');
  expect(html).not.toContain('value="另一份定义"');
});
