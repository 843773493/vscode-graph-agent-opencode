import React from 'react';
import { afterEach, expect, mock, test } from 'bun:test';
import { act, create, type ReactTestRenderer } from 'react-test-renderer';
import { t } from '../../../../shared/core/i18n/index.js';
import { ConfigModelInspectionDTO, ConfigSourcePreflightDTO, type ModelReferenceDTO } from '../../types/protocol_generated/boxteam/workspace/v2/public.js';
import { ModelOptionsInspection } from './ModelOptionsInspection.js';

let view: ReactTestRenderer | undefined;
afterEach(() => { act(() => view?.unmount()); view = undefined; });
const modelRef = { provider_id: 'team', model_id: 'main' };
const result = ConfigModelInspectionDTO.fromPartial({ model_ref: modelRef, model: 'upstream-main',
  options: { temperature: 0 }, request_url: 'https://models.test/v1/responses',
  origins: [{ path: '/temperature', source_key: 'workspace', definition: 'overrides',
    definition_path: '/llm/providers/team/models/main/overrides/temperature' }] });
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
  expect(sections[0].findByType('pre').props.children).toBe(JSON.stringify(result.options, null, 2));
  expect(sections[1].findByType('pre').props.children).toBe(JSON.stringify(receipt.active_model_options!.options, null, 2));
  expect(JSON.stringify(sections[1].findAllByType('p').map(paragraph => paragraph.props.children))).toContain(t('configuration.inspect.activeRevision', 7));
  act(() => view!.update(render(2)));
  expect(inspect).toHaveBeenCalledTimes(1);
  expect(JSON.stringify(view!.toJSON())).toContain(t('configuration.inspect.stale'));
  act(() => view!.root.findAllByType('button')[1].props.onClick());
  expect(open.mock.calls).toEqual([['workspace']]);
});

test('编辑、换模型及卸载取消预检，不采用未遵守取消的迟到回执', async () => {
  const calls: { signal: AbortSignal; resolve: (value: ConfigSourcePreflightDTO) => void }[] = [];
  const inspect = mock((_reference: ModelReferenceDTO, signal: AbortSignal) => new Promise<ConfigSourcePreflightDTO>(resolve => calls.push({ signal, resolve })));
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
