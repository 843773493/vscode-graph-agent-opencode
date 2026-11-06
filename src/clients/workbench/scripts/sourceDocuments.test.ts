import { describe, expect, test, spyOn, mock } from 'bun:test';
import { URI } from '../upstream/src/vs/base/common/uri.js';
import type { ITextModel } from '../upstream/src/vs/editor/common/model.js';
import type { ILanguageService, ILanguageSelection } from '../upstream/src/vs/editor/common/languages/language.js';
import type { IModelService } from '../upstream/src/vs/editor/common/services/model.js';
import type { ITextModelService, ITextModelContentProvider } from '../upstream/src/vs/editor/common/services/resolverService.js';
import { gateway, type WorkspaceTarget } from '../src/agentApi.js';
import { SourceDocuments, sourceDocumentUri, sourceDocumentSource } from '../src/sourceDocuments.js';
import { ResourceOpener } from '../src/resourceOpener.js';
import { workspaceContext } from '../src/workspaceContext.js';
import { gatewayWorkspaceKey } from '../../web/src/api/workspaceIdentity.js';
import type { IOpenerService } from '../upstream/src/vs/platform/opener/common/opener.js';
import type { IEditorService } from '../upstream/src/vs/workbench/services/editor/common/editorService.js';
import type { IEditorGroupsService } from '../upstream/src/vs/workbench/services/editor/common/editorGroupsService.js';
import type { IConfigurationService } from '../upstream/src/vs/platform/configuration/common/configuration.js';

const owner = { gatewayId: 'gateway-source', workspaceId: 'workspace-source' };
const readyTarget: WorkspaceTarget = { gateway_id: owner.gatewayId, workspace_id: owner.workspaceId,
  name: '来源', backend_url: 'http://source.invalid', connection_kind: 'local', status: 'ready',
  active: false, managed: true, removable: true, system_default: false, services: {},
  checked_at: '2026-11-05T00:00:00Z', affected_workspace_ids: [], remote_gateways: [] };
const uri = `boxteam://${owner.gatewayId}/workspace/${owner.workspaceId}/resources/skills/skill-a/中文%20%25.md`;
const resource = sourceDocumentUri(owner, uri);
const result = { node: { uri, resource_shape: 'file' }, content: '# 原来源', content_encoding: 'utf-8', media_type: 'text', truncated: false };
const sessionSource = `boxteam://${owner.gatewayId}/workspace/${owner.workspaceId}/resources/session/ses_00000000400070008000000000000000`;
const sources = [{ name: 'Skill', uri, kind: 'skills' },
  { name: '工具结果', uri: `${sessionSource}/tool-results/tool_${'a'.repeat(24)}.txt`, kind: 'session' },
  { name: 'Session 文本附件', uri: `${sessionSource}/attachments/att_${'b'.repeat(32)}`, kind: 'session' }];

function setup(resource = sourceDocumentUri(owner, uri)) {
  let provider: ITextModelContentProvider | undefined;
  const model = { uri: resource } as ITextModel;
  const created: { content: string; uri: URI }[] = [];
  const documents = new SourceDocuments({ registerTextModelContentProvider(scheme: string, content: ITextModelContentProvider) {
    expect(scheme).toBe('boxteam-source');
    provider = content;
    return { dispose() { provider = undefined; } };
  } } as unknown as ITextModelService, {
    getModel() { return null; },
    createModel(content: string, _language: ILanguageSelection, uri: URI) { created.push({ content, uri }); return model; },
  } as unknown as IModelService, {
    createByFilepathOrFirstLine() { return {}; },
  } as unknown as ILanguageService);
  return { documents, model, created, read: () => provider!.provideTextContent(resource) };
}

describe('只读来源文档的原内容 Provider', () => {
  test('不透明引用往返保留 Unicode，归属与规范 URI 独立于临时 route', () => {
    expect(sourceDocumentSource(URI.parse(resource.toString()))).toEqual({ ...owner, uri });
    expect(resource.path.endsWith('/中文 %.md')).toBe(true);
    expect(sourceDocumentUri({ ...owner, gatewayId: 'other-gateway' }, uri).toString()).not.toBe(resource.toString());
    expect(() => sourceDocumentSource(resource.with({ query: 'route=old' }))).toThrow('完整归属');
  });

  for (const source of sources) {
    test(`${source.name} 只向重新解析的 ready owner 发一次原导航读取，完整正文进入同一个模型`, async () => {
      const resource = sourceDocumentUri(owner, source.uri), fixture = setup(resource);
      const resolve = spyOn(gateway, 'resolveWorkspaceTarget').mockResolvedValue(readyTarget);
      const request = spyOn(gateway, 'request').mockResolvedValue({ ...result, node: { ...result.node, uri: source.uri } });
      try {
        expect(await fixture.read()).toBe(fixture.model);
        const expectedSource = { ...owner, uri: source.uri };
        expect(resolve.mock.calls[0][0]).toEqual(expectedSource);
        expect(request.mock.calls).toHaveLength(1);
        expect(request.mock.calls[0][0]).toBe('/api/v1/resources/navigation');
        expect(request.mock.calls[0][2]).toBe(gatewayWorkspaceKey(owner));
        expect(JSON.parse(String(request.mock.calls[0][1]?.body))).toMatchObject({ uri: source.uri, operation: 'read', read_format: 'auto' });
        expect(fixture.created).toEqual([{ content: '# 原来源', uri: resource }]);
      } finally { fixture.documents.dispose(); resolve.mockRestore(); request.mockRestore(); }
    });
  }

  test('截断／非文本／错误归属不伪装为完整来源模型', async () => {
    const fixture = setup();
    const resolve = spyOn(gateway, 'resolveWorkspaceTarget').mockResolvedValue(readyTarget);
    const request = spyOn(gateway, 'request');
    try {
      for (const [response, error] of [
        [{ ...result, node: undefined }, '未对应请求'],
        [{ ...result, truncated: true, truncation_reason: 'file_byte_limit' }, '未完整读取'],
        [{ ...result, content_encoding: 'base64', media_type: 'image' }, '不是文本'],
        [{ ...result, node: { uri: uri + '/other', resource_shape: 'file' } }, '未对应请求'],
      ] as const) {
        request.mockResolvedValueOnce(response);
        await expect(fixture.read()).rejects.toThrow(error);
      }
      expect(fixture.created).toEqual([]);
    } finally { fixture.documents.dispose(); resolve.mockRestore(); request.mockRestore(); }
  });

  test('停止的工作区不读取、不自动启动、不产生模型', async () => {
    const fixture = setup();
    const resolve = spyOn(gateway, 'resolveWorkspaceTarget').mockResolvedValue({ ...readyTarget, status: 'offline', runtime_action: 'start_managed_backend' });
    const request = spyOn(gateway, 'request');
    try {
      await expect(fixture.read()).rejects.toThrow('请明确启动或连接');
      expect(request).not.toHaveBeenCalled();
      expect(fixture.created).toEqual([]);
    } finally { fixture.documents.dispose(); resolve.mockRestore(); request.mockRestore(); }
  });
});

describe('来源打开沿原模型引用核验', () => {
  for (const source of sources) for (const scenario of ['success', 'read-failure', 'open-failure'] as const) {
    test(`原 Opener ${source.name} ${scenario} 沿来源能力核验，失败不改投或建组，完成后释放预读引用`, async () => {
      const snapshot = spyOn(workspaceContext, 'getSnapshot').mockReturnValue({ ...workspaceContext.getSnapshot(), gatewayId: owner.gatewayId });
      const request = spyOn(gateway, 'request').mockResolvedValue({ uri: source.uri, gateway_id: owner.gatewayId,
        scope: 'workspace', scope_id: owner.workspaceId, kind: source.kind });
      const resolve = spyOn(gateway, 'resolveWorkspaceTarget');
      const dispose = mock(() => {}), addGroup = mock(() => ({ id: 8 }));
      const openEditor = mock(async () => { if (scenario === 'open-failure') throw new Error('打开失败'); });
      const createModelReference = mock(async (target: URI) => {
        expect(sourceDocumentSource(target)).toEqual({ ...owner, uri: source.uri });
        if (scenario === 'read-failure') throw new Error('读取失败');
        return { object: {}, dispose };
      });
      const getGroup = mock(() => ({ id: 7 }));
      const opener = new ResourceOpener({ registerOpener() { return { dispose() {} }; } } as unknown as IOpenerService,
        { openEditor } as unknown as IEditorService,
        { activeGroup: { id: 9 }, getGroup, findGroup: () => undefined, addGroup } as unknown as IEditorGroupsService,
        { getValue: () => 'right' } as unknown as IConfigurationService,
        { createModelReference } as unknown as ITextModelService);
      const options = { boxteamTargetGroup: 7, openToSide: true, editorOptions: { pinned: true, preserveFocus: true,
        selection: { startLineNumber: 4, startColumn: 2 } } };
      try {
        if (scenario === 'success') expect(await opener.open(source.uri, options)).toBe(true);
        else await expect(opener.open(source.uri, options)).rejects.toThrow(scenario === 'read-failure' ? '读取失败' : '打开失败');
        expect(createModelReference).toHaveBeenCalledTimes(1);
        expect(resolve).not.toHaveBeenCalled();
        expect(request).toHaveBeenCalledTimes(1);
        expect(request.mock.calls[0][0]).toBe('/api/gateway/resource-addresses/parse');
        expect(JSON.parse(String(request.mock.calls[0][1]?.body)).uri).toBe(source.uri);
        expect(addGroup).toHaveBeenCalledTimes(scenario === 'read-failure' ? 0 : 1);
        expect(openEditor).toHaveBeenCalledTimes(scenario === 'read-failure' ? 0 : 1);
        expect(dispose).toHaveBeenCalledTimes(scenario === 'read-failure' ? 0 : 1);
        if (scenario !== 'read-failure') {
          expect(getGroup).toHaveBeenCalledWith(7);
          expect(openEditor).toHaveBeenCalledWith({ resource: sourceDocumentUri(owner, source.uri),
            description: `${owner.workspaceId} · ${source.kind}`, options: options.editorOptions }, 8);
        }
      } finally { opener.dispose(); snapshot.mockRestore(); request.mockRestore(); resolve.mockRestore(); }
    });
  }
});
