import React from 'react';
import { afterEach, beforeEach, expect, mock, test } from 'bun:test';
import { act, create, type ReactTestRenderer } from 'react-test-renderer';
import { setLocaleResources, t } from '../../../../shared/core/i18n/index.js';
import chinese from '../../../../shared/core/i18n/messages/zh-cn.json';
import { ConfigActiveOptionsInspectionDTO, ConfigModelInspectionDTO, ConfigSourcePreflightDTO } from '../../types/protocol_generated/boxteam/workspace/v2/public.js';
import type { ConfigurationSelector } from './ConfigurationEntities.js';
import { ModelOptionsInspection } from './ModelOptionsInspection.js';

let view: ReactTestRenderer | undefined;
beforeEach(() => setLocaleResources('zh-cn', chinese));
afterEach(() => { act(() => view?.unmount()); view = undefined; setLocaleResources('zh-cn', chinese); });
const modelRef = { provider_id: 'team', model_id: 'main' };
const result = ConfigModelInspectionDTO.fromPartial({ model_ref: modelRef, model: 'upstream-main',
  config_ref: '#/profiles/fast', node_id: 'fast', chain: [{ node_id: 'fast', source_key: 'workspace', definition_path: '/profiles/fast' }],
  local_settings: { model_options: { temperature: 0 } }, inherited_settings: {}, network: { proxy: { mode: 'direct' } },
  options: { temperature: 0 }, request_url: 'https://models.test/v1/responses',
  origins: [{ path: '/temperature', source_key: 'workspace', definition: 'node',
    definition_path: '/profiles/fast/settings/model_options/temperature' }] });
const receipt = ConfigSourcePreflightDTO.fromPartial({ document: { active_revision: 7 }, model_options: result,
  active_model_options: { ...result, options: { temperature: 1 } } });
const flush = () => new Promise<void>(resolve => setTimeout(resolve, 0));

test('只有显式预检才请求，编辑标记旧回执过期并保持原来源跳转', async () => {
  const inspect = mock(async () => receipt), open = mock();
  const render = (version: number) => <ModelOptionsInspection modelRef={modelRef} version={version} inspect={inspect} onOpenSource={open} />;
  act(() => { view = create(render(1)); });
  expect(inspect).not.toHaveBeenCalled();
  await act(async () => { view!.root.findAllByType('button')[0].props.onClick(); await flush(); });
  expect(inspect).toHaveBeenCalledTimes(1);
  expect(JSON.stringify(view!.toJSON())).toContain(result.request_url);
  expect(JSON.stringify(view!.toJSON())).toContain(t('configuration.inspect.current'));
  const sections = view!.root.findAll(node => node.type === 'section' && node.props['data-model-options']);
  expect(sections.map(section => section.props['data-model-options'])).toEqual(['draft', 'active']);
  expect(sections[0].findAllByType('pre').at(-1)!.props.children).toBe(JSON.stringify(result.options, null, 2));
  expect(sections[1].findAllByType('pre').at(-1)!.props.children).toBe(JSON.stringify(receipt.active_model_options!.options, null, 2));
  expect(sections[1].findAllByType('p').some(paragraph => paragraph.children.includes(t('configuration.inspect.activeRevision', 7)))).toBe(true);
  act(() => view!.update(render(2)));
  expect(inspect).toHaveBeenCalledTimes(1);
  expect(JSON.stringify(view!.toJSON())).toContain(t('configuration.inspect.stale'));
  act(() => view!.root.findAllByType('button').find(button => button.props.title === '/profiles/fast/settings/model_options/temperature')!.props.onClick());
  expect(open.mock.calls).toEqual([['workspace', '/profiles/fast/settings/model_options/temperature']]);
});

test('编辑、换模型及卸载取消预检，不采用未遵守取消的迟到回执', async () => {
  const calls: { signal: AbortSignal; resolve: (value: ConfigSourcePreflightDTO) => void }[] = [];
  const inspect = mock((_reference: ConfigurationSelector, signal: AbortSignal) => new Promise<ConfigSourcePreflightDTO>(resolve => calls.push({ signal, resolve })));
  const render = (version: number, model = modelRef) => <ModelOptionsInspection modelRef={model} version={version} inspect={inspect} onOpenSource={() => {}} />;
  act(() => { view = create(render(1)); });
  const run = () => act(() => view!.root.findAllByType('button')[0].props.onClick());
  run();
  act(() => view!.update(render(2)));
  expect(calls[0].signal.aborted).toBe(true);
  await act(async () => { calls[0].resolve(receipt); await flush(); });
  expect(JSON.stringify(view!.toJSON())).not.toContain(result.request_url);
  run();
  act(() => view!.update(render(2, { ...modelRef, model_id: 'other' })));
  expect(calls[1].signal.aborted).toBe(true);
  run();
  act(() => view!.unmount()); view = undefined;
  expect(calls[2].signal.aborted).toBe(true);
  expect(inspect).toHaveBeenCalledTimes(3);
});

test('Provider预检使用独立节点及发现地址，链、网络和节点值来自同一回执', async () => {
  const inspect = mock(async (_reference: ConfigurationSelector, _signal: AbortSignal) => ConfigSourcePreflightDTO.fromPartial({ provider_options: {
    provider_id: 'team', config_ref: '#/profiles/service', node_id: 'service', shared: true,
    catalog_url: 'https://provider.test/v1/models', options: { endpoint: 'https://provider.test/v1' },
    chain: [{ node_id: 'base', source_key: 'user', source_uri: 'boxteam://user/local/resources/config/workspace', definition_path: '' },
      { node_id: 'service', parent_id: 'base', source_key: 'workspace', definition_path: '/profiles/service' }],
    local_settings: { model_options: { endpoint: 'https://provider.test/v1' } },
    inherited_settings: { model_options: { api_key: '${PROVIDER_KEY}' } }, network: { proxy: { mode: 'direct' } },
  } })), open = mock();
  act(() => { view = create(<ModelOptionsInspection modelRef={{ provider_id: 'team' }} version={1} inspect={inspect} onOpenSource={open} />); });
  expect(inspect).not.toHaveBeenCalled();
  await act(async () => { view!.root.findAllByType('button')[0].props.onClick(); await flush(); });
  expect(inspect.mock.calls[0][0]).toEqual({ provider_id: 'team' });
  const output = JSON.stringify(view!.toJSON());
  expect(output).toContain('https://provider.test/v1/models'); expect(output).toContain('#/profiles/service');
  expect(output).toContain('PROVIDER_KEY'); expect(output).toContain('direct');
  act(() => view!.root.findAllByType('button').find(button => button.props.title === 'boxteam://user/local/resources/config/workspace#')!.props.onClick());
  expect(open.mock.calls).toEqual([['user', '']]);
});

test('已发布快照只展示active，不虚构草稿预检通过', async () => {
  const active = ConfigActiveOptionsInspectionDTO.fromPartial({ active_revision: 12, active_digest: 'published',
    provider_options: { provider_id: 'team' }, model_options: result });
  act(() => { view = create(<ModelOptionsInspection modelRef={modelRef} version={1}
    inspect={async () => active} onOpenSource={() => {}} />); });
  await act(async () => { view!.root.findAllByType('button')[0].props.onClick(); await flush(); });
  expect(view!.root.findAll(node => node.type === 'section' && node.props['data-model-options']).map(node => node.props['data-model-options'])).toEqual(['active']);
  const output = JSON.stringify(view!.toJSON());
  expect(output).toContain(t('configuration.inspect.published'));
  expect(output).not.toContain(t('configuration.inspect.current'));
  expect(output).toContain(t('configuration.inspect.activeRevision', 12));
});
