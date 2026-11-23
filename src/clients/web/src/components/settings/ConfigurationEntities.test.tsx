import React from 'react';
import { expect, mock, test } from 'bun:test';
import { renderToStaticMarkup } from 'react-dom/server';
import { create } from 'react-test-renderer';
import { readFileSync } from 'node:fs';
import { parse } from 'jsonc-parser';
import { t } from '../../../../shared/core/i18n/index.js';
import type { ConfigSourceDocumentDTO } from '../../types/protocol_generated/boxteam/workspace/v2/public.js';
import { ConfigurationEntities } from './ConfigurationEntities';

function sourceDocument(): ConfigSourceDocumentDTO {
  return { gateway_id: 'gateway-a', workspace_id: 'workspace-a', source_key: 'workspace', layer: 'workspace', writable: true,
    jsonc: '{}', file_presence: 'present', file_digest: undefined, layer_revision: undefined, layer_digest: undefined,
    source_generation: undefined, active_revision: undefined, active_digest: undefined, json_schema: {}, status: undefined,
    effective: { agents: { coder: { name: '当前运行显示名' } } },
    source_effective: { agents: { coder: { name: '保存前的本层覆盖' } } }, origins: [], active_origins: [], source_baseline: {},
    layers: [
      { source_key: 'user', layer: 'user', precedence: 1, writable: true, payload: { agents: { coder: {
        name: '下层显示名', instructions: { system_prompt: '下层提示词' }, model: { fallbacks: [{ provider_id: 'team', model_id: 'backup-a' }, { provider_id: 'team', model_id: 'backup-b' }] },
      } } } },
      { source_key: 'workspace', layer: 'workspace', precedence: 2, writable: true, payload: { agents: { coder: { name: '保存前的本层覆盖' } } } },
      { source_key: 'runtime', layer: 'runtime_override', precedence: 3, writable: false, payload: { agents: { coder: { name: '更高层显示名' } } } },
    ] };
}

function render(draft: Record<string, unknown>, document = sourceDocument()) {
  return renderToStaticMarkup(<ConfigurationEntities kind="agents" selectedId="coder" draft={draft} document={document}
    onSelect={() => {}} onEdit={() => {}}
    onSetDefaultAgent={() => {}} onOpenSource={() => {}} />);
}

test('移除未保存的本层 Agent 覆盖立即显示下层正文，不能继续展示旧覆盖或更高层值', () => {
  const html = render({ agents: {} });
  expect(html).toContain('value="下层显示名"');
  expect(html).toContain('下层提示词');
  expect(html).toContain('value="backup-a"');
  expect(html).toContain('value="backup-b"');
  expect(html).toContain('继承自 user');
  expect(html).not.toContain('保存前的本层覆盖');
  expect(html).not.toContain('更高层显示名');
});

test('局部 Agent 覆盖保留下层提示，fallback 保存完整模型引用', () => {
  const html = render({ agents: { coder: { name: '尚未保存的显示名' } } });
  expect(html).toContain('value="尚未保存的显示名"');
  expect(html).toContain('placeholder="下层提示词"');
  expect(html).toContain('value="team"');
  expect(html).toContain('value="backup-a"');
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

function providerDocument(): ConfigSourceDocumentDTO {
  const document = sourceDocument();
  document.json_schema = parse(readFileSync('configs/workspace_schema.jsonc', 'utf8'));
  document.layers[0].payload = { llm: { providers: { team: {
    defaults: { endpoint: 'https://inherited.example/v1' },
    models: { main: { model: 'upstream-main' }, alternate: { model: 'upstream-alternate' } },
  }, other: { models: { main: { model: 'other-upstream' } } } } } };
  return document;
}

test('服务默认配置不提供虚假的重置，显示名称且把模型单独设置收起', () => {
  const onEdit = mock();
  const view = create(<ConfigurationEntities kind="providers" selectedId="fresh" selectedModelId="first"
    draft={{ llm: { providers: { fresh: { name: '我的模型服务', defaults: { endpoint: 'https://example.com/v1' },
      models: { first: { model: 'upstream-one' }, second: { model: 'upstream-two' } } } } } }}
    document={providerDocument()} onSelect={() => {}} onEdit={onEdit}
    onSetDefaultAgent={() => {}} onOpenSource={() => {}} />);
  const article = view.root.findByType('article');
  expect(article.findByType('h2').children).toEqual(['我的模型服务']);
  const endpoint = article.findAllByProps({ className: 'configuration-entity-field' }).find(field => field.findAllByType('label')
    .some(label => label.children.includes(t('configuration.entities.endpoint'))))!;
  expect(endpoint.findAllByType('button')).toHaveLength(0);
  expect(article.findByProps({ className: 'configuration-model-overrides' }).props.open).not.toBe(true);
  expect(view.root.findByProps({ 'aria-label': t('configuration.entities.models') }).findAllByType('strong').map(node => node.children[0]))
    .toEqual(['upstream-one', 'upstream-two']);
  const name = article.findAllByType('input').find(input => input.props.value === '我的模型服务')!;
  name.props.onChange({ target: { value: '' } });
  expect(onEdit.mock.calls).toEqual([['fresh', ['name'], undefined]]);
  view.unmount();
});

test('稳定键列表保留同 Provider 下的继承模型，局部覆盖不会整体替换目录', () => {
  const html = renderToStaticMarkup(<ConfigurationEntities kind="providers" selectedId="team" selectedModelId="alternate"
    draft={{ llm: { providers: { team: { models: { main: { name: '本层名称' } } } } } }} document={providerDocument()}
    onSelect={() => {}} onEdit={() => {}} onSetDefaultAgent={() => {}} onOpenSource={() => {}} />);
  expect(html).toContain('data-model-id="alternate"');
  expect(html).toContain('placeholder="upstream-alternate"');
  expect(html).toContain('继承值：upstream-alternate');
  expect(html).toContain('<strong>本层名称</strong>');
  expect(html).toContain('<strong>other</strong>');
  expect(html).not.toContain('confirmProviderOverride');
});

test('已有 OAuth 服务不误报缺少 API 密钥，地址重置仍只移除本层字段', () => {
  const onEdit = mock(), onSetSecret = mock();
  const view = create(<ConfigurationEntities kind="providers" selectedId="team"
    draft={{ llm: { providers: { team: { defaults: {
      endpoint: 'https://chatgpt.com/backend-api/codex', auth: { type: 'oauth', method: 'chatgpt' },
    } } } } }} document={providerDocument()} onSelect={() => {}} onEdit={onEdit}
    onSetDefaultAgent={() => {}} onOpenSource={() => {}} onSetSecret={onSetSecret} />);
  const credential = view.root.findAllByProps({ className: 'configuration-credential' })[0];
  expect(credential.findByType('h4').children).toEqual([t('configuration.entities.authentication')]);
  expect(credential.findByType('p').children).toEqual([t('configuration.entities.oauth')]);
  expect(credential.findAllByType('button')).toHaveLength(0);
  const endpoint = view.root.findAllByProps({ className: 'configuration-entity-field' })
    .find(field => field.props['data-search-key'] === 'defaults.endpoint')!;
  expect(endpoint.findByType('label').props.htmlFor).toBe(endpoint.findByType('input').props.id);
  endpoint.findByType('button').props.onClick();
  expect(onEdit.mock.calls).toEqual([['team', ['defaults', 'endpoint'], undefined]]);
  view.unmount();
});

test('配置和模型菜单传递各自精确身份，不在正文重复放删除按钮', () => {
  const onShowActions = mock();
  const view = create(<ConfigurationEntities kind="providers" selectedId="team" selectedModelId="main"
    draft={{ llm: { providers: { team: { models: { main: { model: 'my-model' } } } } } }}
    document={providerDocument()} onSelect={() => {}} onEdit={() => {}} onShowActions={onShowActions}
    onSetDefaultAgent={() => {}} onOpenSource={() => {}} />);
  const anchor = {} as HTMLElement;
  const provider = view.root.findByProps({ 'aria-label': t('configuration.entities.definitionActions', 'team') });
  const model = view.root.findByProps({ 'aria-label': t('configuration.entities.modelActions', 'my-model') });
  provider.props.onClick({ currentTarget: anchor });
  model.props.onClick({ currentTarget: anchor });
  expect(onShowActions.mock.calls).toEqual([['team', undefined, anchor], ['team', 'main', anchor]]);
  expect(view.root.findAllByType('button').some(button => button.children.includes(t('configuration.entities.copyModel')))).toBe(false);
  view.unmount();
});

test('编辑继承模型写入精确 Provider／Model 路径，凭据操作沿相同目标', () => {
  const onEdit = mock(), onSetSecret = mock();
  const view = create(<ConfigurationEntities kind="providers" selectedId="team" selectedModelId="alternate"
    draft={{}} document={providerDocument()} onSelect={() => {}} onEdit={onEdit}
    onSetDefaultAgent={() => {}} onOpenSource={() => {}} onSetSecret={onSetSecret} />);
  const article = view.root.findByProps({ 'data-definition-id': 'team' });
  const input = article.findAllByType('input').find(input => input.props.value === 'upstream-alternate')!;
  expect(input.props.disabled).toBe(false);
  input.props.onChange({ target: { value: 'changed-upstream' } });
  expect(onEdit.mock.calls).toEqual([['team', ['models', 'alternate', 'model'], 'changed-upstream']]);
  const model = article.findByProps({ 'data-model-id': 'alternate' });
  model.findAllByType('button').find(button => button.children.includes(t('configuration.entities.setSecret')))!.props.onClick();
  expect(onSetSecret.mock.calls).toEqual([['team', 'alternate']]);
  view.unmount();
});

test('协议切换替换本层 reasoning 整块，恢复继承保留独立 streaming 覆盖', () => {
  const onEdit = mock();
  const view = create(<ConfigurationEntities kind="providers" selectedId="team" draft={{ llm: { providers: { team: {
    defaults: { api_mode: { protocol: 'responses', streaming: false, supports_reasoning: { reasoning_items: { summary: true } } } },
  } } } }} document={providerDocument()} onSelect={() => {}} onEdit={onEdit}
    onSetDefaultAgent={() => {}} onOpenSource={() => {}} />);
  const protocol = view.root.findAllByType('select').find(select => select.props.value === 'responses')!;
  protocol.props.onChange({ target: { value: 'anthropic_messages' } });
  expect(onEdit.mock.calls[0]).toEqual(['team', ['defaults', 'api_mode'], {
    protocol: 'anthropic_messages', streaming: false, supports_reasoning: {},
  }]);
  protocol.props.onChange({ target: { value: '' } });
  expect(onEdit.mock.calls[1]).toEqual(['team', ['defaults', 'api_mode'], { streaming: false }]);
  view.unmount();
});

test('服务清除密钥保留有效的无认证配置，模型重置只移除本层精确字段', () => {
  const onEdit = mock();
  const document = providerDocument();
  const draft = { llm: { providers: { team: {
    defaults: { api_key: 'secret:sec_' + '1'.repeat(32) },
    models: { main: { overrides: { api_key: '' } } },
  } } } };
  const render = (writable: boolean) => <ConfigurationEntities kind="providers" selectedId="team" selectedModelId="main"
    draft={draft} document={{ ...document, writable }} onSelect={() => {}} onEdit={onEdit}
    onSetDefaultAgent={() => {}} onOpenSource={() => {}} />;
  const view = create(render(true));
  const credentials = view.root.findAllByProps({ className: 'configuration-credential' });
  expect(credentials.map(section => section.props['data-credential-scope'])).toEqual(['provider', 'model']);
  expect(credentials[1].findByType('p').children).toEqual([t('configuration.entities.noAuthentication')]);
  for (const section of credentials) section.findByType('button').props.onClick();
  expect(onEdit.mock.calls).toEqual([
    ['team', ['defaults', 'api_key'], ''],
    ['team', ['models', 'main', 'overrides', 'api_key'], undefined],
  ]);
  view.update(render(false));
  expect(view.root.findAllByProps({ className: 'configuration-credential' }).every(section => section.findByType('button').props.disabled)).toBe(true);
  view.unmount();
});

test('继承的凭据不提供删除下层值的动作', () => {
  const document = providerDocument();
  document.layers[0].payload!.llm = { providers: { team: {
    defaults: { api_key: '${INHERITED_KEY}' },
    models: { main: { model: 'main', overrides: { api_key: '' } } },
  } } };
  const view = create(<ConfigurationEntities kind="providers" selectedId="team" selectedModelId="main"
    draft={{}} document={document} onSelect={() => {}} onEdit={() => {}}
    onSetDefaultAgent={() => {}} onOpenSource={() => {}} />);
  const credentials = view.root.findAllByProps({ className: 'configuration-credential' });
  expect(credentials[0].findByType('p').children).toEqual([t('configuration.credentials.environment', 'INHERITED_KEY')]);
  expect(credentials[1].findByType('p').children).toEqual([t('configuration.entities.noAuthentication')]);
  expect(credentials.every(section => section.findAllByType('button').length === 0)).toBe(true);
  view.unmount();
});

test('fallback 编辑保留完整身份及顺序，不按 Provider 合并同服务模型', () => {
  const onEdit = mock();
  const view = create(<ConfigurationEntities kind="agents" selectedId="coder" document={sourceDocument()}
    draft={{ agents: { coder: { model: { fallbacks: [
      { provider_id: 'team', model_id: 'a' }, { provider_id: 'team', model_id: 'b' },
    ] } } } }} onSelect={() => {}} onEdit={onEdit}
    onSetDefaultAgent={() => {}} onOpenSource={() => {}} />);
  view.root.findAllByType('input').find(input => input.props.value === 'b')!.props.onChange({ target: { value: 'c' } });
  expect(onEdit.mock.calls).toEqual([['coder', ['model', 'fallbacks'], [
    { provider_id: 'team', model_id: 'a' }, { provider_id: 'team', model_id: 'c' },
  ]]]);
  view.unmount();
});

test('高级能力按目标schema提供类型，不把输入容量当请求输出限制或复制继承值', () => {
  const onEdit = mock();
  const view = create(<ConfigurationEntities kind="providers" selectedId="team" draft={{}}
    document={providerDocument()} onSelect={() => {}} onEdit={onEdit}
    onSetDefaultAgent={() => {}} onOpenSource={() => {}} />);
  const defaults = view.root.findAllByProps({ className: 'configuration-model-options' })[0];
  const label = defaults.findAllByType('label').find(label => label.children.includes(t('configuration.capability.supports_function_calling')))!;
  label.findByType('select').props.onChange({ target: { value: 'false' } });
  const capacity = defaults.findByProps({ className: 'configuration-capacity-input' }).findByType('input');
  expect(capacity.props.inputMode).toBe('numeric');
  const input = { value: '128000', selectionStart: 6, selectionEnd: 6, setCustomValidity: mock(), reportValidity: mock(), setSelectionRange: mock() };
  capacity.props.onChange({ currentTarget: input });
  expect(input.value).toBe('128,000');
  expect(input.setSelectionRange).toHaveBeenLastCalledWith(7, 7);
  expect(onEdit.mock.calls).toEqual([
    ['team', ['defaults', 'api_mode', 'model_info', 'supports_function_calling'], false],
    ['team', ['defaults', 'api_mode', 'model_info', 'max_input_tokens'], 128000],
  ]);
  expect(onEdit.mock.calls.some(call => JSON.stringify(call).includes('max_output_tokens'))).toBe(false);
  view.unmount();
});

test('参数区分null与恢复继承，JSON显式等值覆盖和reasoning整块仍写原路径', () => {
  const onEdit = mock();
  const view = create(<ConfigurationEntities kind="providers" selectedId="team" selectedModelId="main"
    draft={{ llm: { providers: { team: { models: { main: { overrides: { request_options: { parameters: { temperature: null } } } } } } } } }}
    document={providerDocument()} onSelect={() => {}} onEdit={onEdit}
    onSetDefaultAgent={() => {}} onOpenSource={() => {}} />);
  const model = view.root.findByProps({ 'data-model-id': 'main' });
  const parameter = model.findByProps({ 'data-parameter': 'temperature' });
  expect(parameter.findAllByType('small').flatMap(node => node.children)).toContain(t('configuration.options.omitted'));
  parameter.findAllByType('button').find(button => button.children.includes(t('configuration.options.clear')))!.props.onClick();
  parameter.findAllByType('button').find(button => button.children.includes(t('configuration.options.omit')))!.props.onClick();
  const value = { value: '0', setCustomValidity: mock(), reportValidity: mock() };
  parameter.findByType('form').props.onSubmit({ preventDefault() {}, currentTarget: { elements: { namedItem: () => value } } });
  const options = model.findByProps({ className: 'configuration-model-options' });
  options.findAllByType('button').find(button => button.children.includes(t('configuration.options.emptyReasoning')))!.props.onClick();
  expect(onEdit.mock.calls).toEqual([
    ['team', ['models', 'main', 'overrides', 'request_options', 'parameters', 'temperature'], undefined],
    ['team', ['models', 'main', 'overrides', 'request_options', 'parameters', 'temperature'], null],
    ['team', ['models', 'main', 'overrides', 'request_options', 'parameters', 'temperature'], 0],
    ['team', ['models', 'main', 'overrides', 'api_mode', 'supports_reasoning'], {}],
  ]);
  expect(value.reportValidity).not.toHaveBeenCalled();
  view.unmount();
});
