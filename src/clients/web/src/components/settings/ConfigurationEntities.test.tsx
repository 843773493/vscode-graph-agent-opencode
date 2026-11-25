import React from 'react';
import { expect, mock, test } from 'bun:test';
import { renderToStaticMarkup } from 'react-dom/server';
import { act, create } from 'react-test-renderer';
import { readFileSync } from 'node:fs';
import { parse } from 'jsonc-parser';
import { t } from '../../../../shared/core/i18n/index.js';
import { ConfigSourceDocumentDTO, ConfigSourcePreflightDTO } from '../../types/protocol_generated/boxteam/workspace/v2/public.js';
import { ConfigurationEntities, localProfilePath } from './ConfigurationEntities';
import { ConfigurationProxyFields } from './ConfigurationProxyFields.js';
import { installGatewayFetch, restoreGatewayTestEnvironment, TEST_GATEWAY_WORKSPACE_REQUEST_TARGET } from '../../tests/fixtures/session/gatewayEnvironment.js';

function sourceDocument(): ConfigSourceDocumentDTO {
  const document = ConfigSourceDocumentDTO.fromPartial({ gateway_id: 'gateway-a', workspace_id: 'workspace-a', source_key: 'workspace', layer: 'workspace', writable: true,
    jsonc: '{}', file_presence: 'present', json_schema: parse(readFileSync('configs/workspace_schema.jsonc', 'utf8')),
    effective: { agents: { coder: { name: '当前运行显示名' } } }, source_effective: { agents: { coder: { name: '保存前的本层覆盖' } } },
    layers: [
      { source_key: 'user', layer: 'user', precedence: 1, writable: true, payload: {
        agents: { coder: { name: '下层显示名', instructions: { system_prompt: '下层提示词' },
          model: { fallbacks: [{ provider_id: 'team', model_id: 'backup-a' }, { provider_id: 'team', model_id: 'backup-b' }] } } },
        llm: { providers: { team: { config_ref: '#/profiles/provider', models: {
          main: { model: 'upstream-main', config_ref: '#/profiles/main' }, alternate: { model: 'upstream-alternate', config_ref: '#/profiles/alternate' } } },
          other: { config_ref: '#/profiles/other', models: {} } } },
      } },
      { source_key: 'workspace', layer: 'workspace', precedence: 2, writable: true, payload: { agents: { coder: { name: '保存前的本层覆盖' } } } },
      { source_key: 'runtime', layer: 'runtime_override', precedence: 3, writable: false, payload: { agents: { coder: { name: '更高层显示名' } } } },
    ] });
  document.source_inherited = document.layers[0].payload;
  document.source_inherited_origins = [
    { path: '/agents/coder', source_key: 'user', shadowed_sources: [] },
    { path: '/llm/providers/team', source_key: 'user', shadowed_sources: [] },
    { path: '/llm/providers/other', source_key: 'user', shadowed_sources: [] },
  ];
  for (const layer of document.layers) layer.payload = { config_version: 4, settings: { workspace: layer.payload } };
  return document;
}
const draft = (workspace: Record<string, unknown>, profiles: Record<string, unknown> = {}) => ({ config_version: 4, settings: { workspace }, profiles });
const profile = (options: Record<string, unknown>) => ({ extends: 'context:workspace', settings: { model_options: options } });
const providerDraft = (options: Record<string, unknown> = {}, modelOptions: Record<string, unknown> = {}) => draft({ llm: { providers: { team: {
  config_ref: '#/profiles/provider', models: { main: { model: 'my-model', config_ref: '#/profiles/main' } },
} } } }, { provider: profile(options), main: profile(modelOptions) });
const callbacks = { onSelect() {}, onEdit() {}, onSetDefaultAgent() {}, onOpenSource() {}, isNodeReady: () => true, async onPrepareOptions() {} };
const flush = () => new Promise<void>(resolve => setTimeout(resolve, 0));

test('来源、节点准备及实际目录请求的失败均显示安全短摘要，不改原原因', async () => {
  const reason = `配置读取失败 Authorization: Bearer model-form-secret\n${'完整诊断'.repeat(100)}`;
  const original = new Error(reason);
  const document = sourceDocument();
  let view: ReturnType<typeof create> | undefined;
  const check = () => {
    const alerts = view!.root.findAllByProps({ role: 'alert' });
    expect(alerts.length).toBeGreaterThan(0);
    for (const alert of alerts) {
      const visible = alert.children.join('');
      expect(visible).toContain('配置读取失败');
      expect(Array.from(visible).length).toBeLessThanOrEqual(160);
      expect(visible).not.toContain('model-form-secret');
      expect(visible).not.toContain('\n');
    }
  };
  try {
    await act(async () => {
      view = create(<ConfigurationEntities kind="providers" selectedId="team" selectedModelId="main"
        draft={providerDraft()} document={document} {...callbacks} error={reason} />);
      await flush();
    });
    check();
    await act(async () => {
      view!.update(<ConfigurationEntities kind="providers" selectedId="team" selectedModelId="main"
        draft={providerDraft()} document={document} {...callbacks} isNodeReady={() => false}
        onPrepareOptions={async () => { throw original; }} />);
      await flush();
    });
    check();
    installGatewayFetch(({ path }) => path.startsWith('/api/v1/config/llm/')
      ? Response.json({ detail: { message: reason } }, { status: 503, headers: { 'X-Request-ID': 'req_catalog_failure' } }) : undefined);
    await act(async () => {
      view!.update(<ConfigurationEntities kind="providers" selectedId="team" selectedModelId="main"
        draft={providerDraft({ custom_llm_provider: 'openai' })} document={document} {...callbacks}
        catalogTarget={TEST_GATEWAY_WORKSPACE_REQUEST_TARGET} catalogSignal={new AbortController().signal} />);
      await flush(); await flush();
    });
    check();
    expect(original.message).toBe(reason);
  } finally {
    act(() => view?.unmount());
    restoreGatewayTestEnvironment();
  }
});

test('节点归属未准备时禁用参数但允许修正引用，同文本版本的来源更新仍重新准备', async () => {
  const document = sourceDocument(), prepare = mock(async () => {});
  const props = { ...callbacks, isNodeReady: () => false, onPrepareOptions: prepare };
  let view: ReturnType<typeof create>;
  await act(async () => {
    view = create(<ConfigurationEntities kind="providers" selectedId="team" selectedModelId="main" draftVersion={7}
      draft={providerDraft({ endpoint: 'https://example.com/v1' })} document={document} {...props} onEditOptions={() => {}} />);
    await flush();
  });
  expect(prepare.mock.calls).toHaveLength(1);
  expect(view!.root.findAllByProps({ 'data-search-key': 'model_options.endpoint' })[0].findByType('input').props.disabled).toBe(true);
  expect(view!.root.findAllByProps({ 'data-search-key': 'config_ref' })[0].findByType('input').props.disabled).toBe(false);
  await act(async () => {
    view!.update(<ConfigurationEntities kind="providers" selectedId="team" selectedModelId="main" draftVersion={7}
      draft={providerDraft({ endpoint: 'https://example.com/v1' })} document={{ ...document }} {...props} onEditOptions={() => {}} />);
    await flush();
  });
  expect(prepare.mock.calls).toHaveLength(2);
  view!.unmount();
});

function render(workspace: Record<string, unknown>, document = sourceDocument()) {
  return renderToStaticMarkup(<ConfigurationEntities kind="agents" selectedId="coder" draft={draft(workspace)} document={document} {...callbacks} />);
}

test('移除本层 Agent 覆盖立即显示下层正文，不能继续展示旧覆盖或更高层值', () => {
  const html = render({ agents: {} });
  expect(html).toContain('value="下层显示名"'); expect(html).toContain('下层提示词');
  expect(html).toContain('value="backup-a"'); expect(html).toContain('value="backup-b"');
  expect(html).toContain('继承自 user'); expect(html).not.toContain('保存前的本层覆盖'); expect(html).not.toContain('更高层显示名');
});

test('继承实体跳转使用后端父链归属，不按来源列表顺序猜测', () => {
  const document = sourceDocument();
  document.layers = [];
  document.source_inherited_origins = document.source_inherited_origins.map(origin =>
    origin.path === '/agents/coder' ? { ...origin, source_key: 'inline' } : origin);
  const open = mock();
  const view = create(<ConfigurationEntities kind="agents" selectedId="coder" draft={draft({ agents: {} })}
    document={document} {...callbacks} onOpenSource={open} />);
  view.root.findAllByType('button').find(button => button.children.includes(t('configuration.entities.editOwningSource')))!.props.onClick();
  expect(open.mock.calls).toEqual([['inline']]);
  view.unmount();
});

test('局部 Agent 覆盖保留下层提示，fallback 保存完整模型引用', () => {
  const html = render({ agents: { coder: { name: '尚未保存的显示名' } } });
  expect(html).toContain('value="尚未保存的显示名"'); expect(html).toContain('placeholder="下层提示词"');
  expect(html).toContain('value="team"'); expect(html).toContain('value="backup-a"'); expect(html).not.toContain('[&quot;backup-a&quot;');
});

test('显式打开已移除的 Agent 不得误展示第一条定义', () => {
  const document = sourceDocument(); document.layers = []; document.source_inherited = {};
  const html = render({ agents: { another: { name: '另一份定义' } } }, document);
  expect(html).toContain(t('configuration.entities.definitionChanged', 'coder'));
  expect(html).not.toContain('data-definition-id="another"');
});

test('节点本地端点没有预检继承值时不提供虚假重置，身份与选项分开编辑', () => {
  const onEdit = mock(), onEditOptions = mock();
  const view = create(<ConfigurationEntities kind="providers" selectedId="team" selectedModelId="main"
    draft={providerDraft({ endpoint: 'https://example.com/v1' })} document={sourceDocument()} {...callbacks} onEdit={onEdit} onEditOptions={onEditOptions} />);
  const endpoint = view.root.findAllByProps({ 'data-search-key': 'model_options.endpoint' })[0];
  expect(endpoint.findByType('input').props.value).toBe('https://example.com/v1');
  expect(endpoint.findAllByType('button')).toHaveLength(0);
  endpoint.findByType('input').props.onChange({ target: { value: 'https://changed.example/v1' } });
  expect(onEditOptions.mock.calls).toEqual([[{ provider_id: 'team' }, ['model_options', 'endpoint'], 'https://changed.example/v1']]);
  const reference = view.root.findAllByProps({ 'data-search-key': 'config_ref' })[0];
  reference.findByType('input').props.onChange({ target: { value: '#/profiles/other' } });
  expect(onEdit.mock.calls).toEqual([['team', ['config_ref'], '#/profiles/other']]);
  expect(view.root.findByProps({ className: 'configuration-model-overrides' }).props.open).not.toBe(true);
  view.unmount();
});

test('无父模型根不提供代理继承，子模型仍可恢复自己的覆盖', () => {
  const base = providerDraft();
  const source = { ...base, profiles: { ...base.profiles, main: { settings: { network: { proxy: { mode: 'direct' } } } } } };
  const view = create(<ConfigurationEntities kind="providers" selectedId="team" selectedModelId="main"
    draft={source} document={sourceDocument()} {...callbacks} onEditOptions={() => {}} />);
  const proxies = view.root.findAllByType(ConfigurationProxyFields);
  expect(proxies[0].props.allowInherit).toBe(true);
  expect(proxies[1].props.allowInherit).toBe(false);
  expect(proxies[1].findAllByType('option').map(option => option.props.value)).not.toContain('');
  view.unmount();
});

test('稳定键列表保留继承模型，局部覆盖不会整体替换目录', () => {
  const html = renderToStaticMarkup(<ConfigurationEntities kind="providers" selectedId="team" selectedModelId="alternate"
    draft={draft({ llm: { providers: { team: { models: { main: { name: '本层名称' } } } } } })} document={sourceDocument()} {...callbacks} />);
  expect(html).toContain('data-model-id="alternate"'); expect(html).toContain('placeholder="upstream-alternate"');
  expect(html).toContain('<strong>本层名称</strong>'); expect(html).toContain('<strong>other</strong>');
});

test('OAuth节点不误报缺少API密钥，显式预检后重置仅移除本地端点', async () => {
  const onEditOptions = mock(), onSetSecret = mock();
  const receipt = ConfigSourcePreflightDTO.fromPartial({ provider_options: { provider_id: 'team', inherited_settings: { model_options: { endpoint: 'https://inherited.example/v1' } } } });
  const view = create(<ConfigurationEntities kind="providers" selectedId="team" draftVersion={1}
    draft={providerDraft({ endpoint: 'https://chatgpt.com/backend-api/codex', auth: { type: 'oauth', method: 'chatgpt' } })}
    document={sourceDocument()} {...callbacks} onEditOptions={onEditOptions} onSetSecret={onSetSecret} onInspectModel={async () => receipt} />);
  const credential = view.root.findAllByProps({ className: 'configuration-credential' })[0];
  expect(credential.findByType('h4').children).toEqual([t('configuration.entities.authentication')]);
  expect(credential.findByType('p').children).toEqual([t('configuration.entities.oauth')]); expect(credential.findAllByType('button')).toHaveLength(0);
  await act(async () => { view.root.findAllByProps({ className: 'configuration-model-inspection' })[0].findAllByType('button')[0].props.onClick(); await flush(); });
  const endpoint = view.root.findAllByProps({ 'data-search-key': 'model_options.endpoint' })[0];
  endpoint.findByType('button').props.onClick();
  expect(onEditOptions.mock.calls).toEqual([[{ provider_id: 'team' }, ['model_options', 'endpoint'], undefined]]);
  view.unmount();
});

test('配置和模型菜单传递精确身份，不在正文重复放删除按钮', () => {
  const onShowActions = mock();
  const view = create(<ConfigurationEntities kind="providers" selectedId="team" selectedModelId="main"
    draft={providerDraft()} document={sourceDocument()} {...callbacks} onShowActions={onShowActions} />);
  const anchor = {} as HTMLElement;
  view.root.findByProps({ 'aria-label': t('configuration.entities.definitionActions', 'team') }).props.onClick({ currentTarget: anchor });
  view.root.findByProps({ 'aria-label': t('configuration.entities.modelActions', 'my-model') }).props.onClick({ currentTarget: anchor });
  expect(onShowActions.mock.calls).toEqual([['team', undefined, anchor], ['team', 'main', anchor]]);
  expect(view.root.findAllByType('button').some(button => button.children.includes(t('configuration.entities.copyModel')))).toBe(false);
  view.unmount();
});

test('编辑继承模型写入精确身份，凭据操作沿相同目标', () => {
  const onEdit = mock(), onSetSecret = mock();
  const view = create(<ConfigurationEntities kind="providers" selectedId="team" selectedModelId="alternate"
    draft={draft({})} document={sourceDocument()} {...callbacks} onEdit={onEdit} onSetSecret={onSetSecret} />);
  const input = view.root.findAllByType('input').find(input => input.props.value === 'upstream-alternate')!;
  expect(input.props.disabled).toBe(false); input.props.onChange({ target: { value: 'changed-upstream' } });
  expect(onEdit.mock.calls).toEqual([['team', ['models', 'alternate', 'model'], 'changed-upstream']]);
  view.root.findByProps({ 'data-model-id': 'alternate' }).findAllByType('button').find(button => button.children.includes(t('configuration.entities.setSecret')))!.props.onClick();
  expect(onSetSecret.mock.calls).toEqual([['team', 'alternate']]); view.unmount();
});

test('协议切换替换本地reasoning整块，恢复继承保留streaming覆盖', () => {
  const onEditOptions = mock();
  const view = create(<ConfigurationEntities kind="providers" selectedId="team" draft={providerDraft({ api_mode: {
    protocol: 'responses', streaming: false, supports_reasoning: { reasoning_items: { summary: true } },
  } })} document={sourceDocument()} {...callbacks} onEditOptions={onEditOptions} />);
  const protocol = view.root.findAllByType('select').find(select => select.props.value === 'responses')!;
  protocol.props.onChange({ target: { value: 'anthropic_messages' } }); protocol.props.onChange({ target: { value: '' } });
  expect(onEditOptions.mock.calls).toEqual([
    [{ provider_id: 'team' }, ['model_options', 'api_mode'], { protocol: 'anthropic_messages', streaming: false, supports_reasoning: {} }],
    [{ provider_id: 'team' }, ['model_options', 'api_mode'], { streaming: false }],
  ]); view.unmount();
});

test('服务清除密钥保留无认证配置，模型重置仅移除自身字段且只读禁用', () => {
  const onEditOptions = mock();
  const render = (writable: boolean) => <ConfigurationEntities kind="providers" selectedId="team" selectedModelId="main"
    draft={providerDraft({ api_key: 'secret:sec_' + '1'.repeat(32) }, { api_key: '' })} document={{ ...sourceDocument(), writable }} {...callbacks} onEditOptions={onEditOptions} />;
  const view = create(render(true)); const credentials = view.root.findAllByProps({ className: 'configuration-credential' });
  expect(credentials.map(section => section.props['data-credential-scope'])).toEqual(['provider', 'model']);
  expect(credentials[1].findByType('p').children).toEqual([t('configuration.entities.noAuthentication')]);
  for (const section of credentials) section.findByType('button').props.onClick();
  expect(onEditOptions.mock.calls).toEqual([
    [{ provider_id: 'team' }, ['model_options', 'api_key'], ''],
    [{ provider_id: 'team', model_id: 'main' }, ['model_options', 'api_key'], undefined],
  ]);
  view.update(render(false)); expect(view.root.findAllByProps({ className: 'configuration-credential' }).every(section => section.findByType('button').props.disabled)).toBe(true); view.unmount();
});

test('继承的同名fragment不能劫持本地profile，凭据仅使用后端预检结果', async () => {
  const inspect = mock(async () => ConfigSourcePreflightDTO.fromPartial({ provider_options: { provider_id: 'team',
    options: { api_key: '${INHERITED_KEY}' } } }));
  const view = create(<ConfigurationEntities kind="providers" selectedId="team" draftVersion={1}
    draft={draft({}, { provider: profile({ api_key: 'wrong-local-value' }) })} document={sourceDocument()} {...callbacks} onInspectModel={inspect} />);
  expect(inspect).not.toHaveBeenCalled(); expect(JSON.stringify(view.toJSON())).not.toContain('wrong-local-value');
  await act(async () => { view.root.findAllByProps({ className: 'configuration-model-inspection' })[0].findAllByType('button')[0].props.onClick(); await flush(); });
  const credential = view.root.findAllByProps({ className: 'configuration-credential' })[0];
  expect(credential.findByType('p').children).toEqual([t('configuration.credentials.environment', 'INHERITED_KEY')]);
  expect(credential.findAllByType('button')).toHaveLength(0); view.unmount();
  expect(localProfilePath('#/profiles/a~1b')).toEqual(['profiles', 'a/b']);
  expect(localProfilePath('user:workspace#/profiles/provider')).toBeUndefined();
});

test('fallback编辑保留完整身份及顺序', () => {
  const onEdit = mock();
  const view = create(<ConfigurationEntities kind="agents" selectedId="coder" document={sourceDocument()}
    draft={draft({ agents: { coder: { model: { fallbacks: [{ provider_id: 'team', model_id: 'a' }, { provider_id: 'team', model_id: 'b' }] } } } })} {...callbacks} onEdit={onEdit} />);
  view.root.findAllByType('input').find(input => input.props.value === 'b')!.props.onChange({ target: { value: 'c' } });
  expect(onEdit.mock.calls).toEqual([['coder', ['model', 'fallbacks'], [{ provider_id: 'team', model_id: 'a' }, { provider_id: 'team', model_id: 'c' }]]]); view.unmount();
});

test('能力容量不变成输出限制，参数null/0/删除与reasoning整块保持区别', () => {
  const onEditOptions = mock();
  const view = create(<ConfigurationEntities kind="providers" selectedId="team" selectedModelId="main" draft={providerDraft({}, { request_options: { parameters: { temperature: null } } })}
    document={sourceDocument()} {...callbacks} onEditOptions={onEditOptions} />);
  const provider = view.root.findAllByProps({ className: 'configuration-model-options' })[0];
  provider.findAllByType('label').find(label => label.children.includes(t('configuration.capability.supports_function_calling')))!.findByType('select').props.onChange({ target: { value: 'false' } });
  const capacity = provider.findByProps({ 'data-search-key': 'api_mode.model_info.max_input_tokens' }).findByType('input');
  expect(capacity.props.inputMode).toBe('numeric');
  const input = { value: '128000', selectionStart: 6, selectionEnd: 6, setCustomValidity: mock(), reportValidity: mock(), setSelectionRange: mock() };
  capacity.props.onChange({ currentTarget: input }); expect(input.value).toBe('128,000'); expect(input.setSelectionRange).toHaveBeenLastCalledWith(7, 7);
  const model = view.root.findByProps({ 'data-model-id': 'main' }), parameter = model.findByProps({ 'data-parameter': 'temperature' });
  expect(parameter.findAllByType('small').flatMap(node => node.children)).toContain(t('configuration.options.omitted'));
  parameter.findAllByType('button').find(button => button.children.includes(t('configuration.options.clear')))!.props.onClick();
  parameter.findAllByType('button').find(button => button.children.includes(t('configuration.options.omit')))!.props.onClick();
  const value = { value: '0', setCustomValidity: mock(), reportValidity: mock() };
  parameter.findByType('form').props.onSubmit({ preventDefault() {}, currentTarget: { elements: { namedItem: () => value } } });
  model.findByProps({ className: 'configuration-model-options' }).findAllByType('button').find(button => button.children.includes(t('configuration.options.emptyReasoning')))!.props.onClick();
  expect(onEditOptions.mock.calls).toEqual([
    [{ provider_id: 'team' }, ['model_options', 'api_mode', 'model_info', 'supports_function_calling'], false],
    [{ provider_id: 'team' }, ['model_options', 'api_mode', 'model_info', 'max_input_tokens'], 128000],
    [{ provider_id: 'team', model_id: 'main' }, ['model_options', 'request_options', 'parameters', 'temperature'], undefined],
    [{ provider_id: 'team', model_id: 'main' }, ['model_options', 'request_options', 'parameters', 'temperature'], null],
    [{ provider_id: 'team', model_id: 'main' }, ['model_options', 'request_options', 'parameters', 'temperature'], 0],
    [{ provider_id: 'team', model_id: 'main' }, ['model_options', 'api_mode', 'supports_reasoning'], {}],
  ]); expect(value.reportValidity).not.toHaveBeenCalled(); view.unmount();
});

test('总上下文窗口独立显示和编辑，非法整数不写入且清空恢复继承', () => {
  const onEditOptions = mock();
  const view = create(<ConfigurationEntities kind="providers" selectedId="team" selectedModelId="main"
    draft={providerDraft({ api_mode: { model_info: { max_input_tokens: 128000, context_window: 200000 } } },
      { api_mode: { model_info: { context_window: 160000 } } })}
    document={sourceDocument()} {...callbacks} onEditOptions={onEditOptions} />);
  const provider = view.root.findAllByProps({ className: 'configuration-model-options' })[0];
  expect(provider.findByProps({ 'data-search-key': 'api_mode.model_info.max_input_tokens' }).findByType('input').props.value).toBe('128,000');
  const field = provider.findByProps({ 'data-search-key': 'api_mode.model_info.context_window' });
  expect(field.children).toContain(t('configuration.capability.context_window'));
  expect(field.findByType('input').props.value).toBe('200,000');
  const model = view.root.findByProps({ 'data-model-id': 'main' });
  const capacity = model.findByProps({ 'data-search-key': 'api_mode.model_info.context_window' }).findByType('input');
  expect(capacity.props.value).toBe('160,000');
  const input = { value: '256000', selectionStart: 6, selectionEnd: 6, setCustomValidity: mock(), reportValidity: mock(), setSelectionRange: mock() };
  capacity.props.onChange({ currentTarget: input });
  expect(input.value).toBe('256,000');
  for (const invalid of ['0', '-1', '1.5', '9007199254740992']) {
    input.value = invalid; capacity.props.onChange({ currentTarget: input });
  }
  expect(input.reportValidity).toHaveBeenCalledTimes(4);
  input.value = ''; capacity.props.onChange({ currentTarget: input });
  expect(onEditOptions.mock.calls).toEqual([
    [{ provider_id: 'team', model_id: 'main' }, ['model_options', 'api_mode', 'model_info', 'context_window'], 256000],
    [{ provider_id: 'team', model_id: 'main' }, ['model_options', 'api_mode', 'model_info', 'context_window'], undefined],
  ]);
  view.unmount();
});
