import { URI } from '../../../../../src/clients/workbench/upstream/src/vs/base/common/uri.js';
import { Event } from '../../../../../src/clients/workbench/upstream/src/vs/base/common/event.js';
import { extUri, type IExtUri } from '../../../../../src/clients/workbench/upstream/src/vs/base/common/resources.js';
import { Range } from '../../../../../src/clients/workbench/upstream/src/vs/editor/common/core/range.js';
import { TextModelSearch, SearchParams } from '../../../../../src/clients/workbench/upstream/src/vs/editor/common/model/textModelSearch.js';
import { IModelService } from '../../../../../src/clients/workbench/upstream/src/vs/editor/common/services/model.js';
import { IFileService } from '../../../../../src/clients/workbench/upstream/src/vs/platform/files/common/files.js';
import { getSingletonServiceDescriptors } from '../../../../../src/clients/workbench/upstream/src/vs/platform/instantiation/common/extensions.js';
import { InstantiationService } from '../../../../../src/clients/workbench/upstream/src/vs/platform/instantiation/common/instantiationService.js';
import { ServiceCollection } from '../../../../../src/clients/workbench/upstream/src/vs/platform/instantiation/common/serviceCollection.js';
import { NullLogService, ILogService } from '../../../../../src/clients/workbench/upstream/src/vs/platform/log/common/log.js';
import { ITelemetryService } from '../../../../../src/clients/workbench/upstream/src/vs/platform/telemetry/common/telemetry.js';
import { IUriIdentityService } from '../../../../../src/clients/workbench/upstream/src/vs/platform/uriIdentity/common/uriIdentity.js';
import { IWebWorkerService } from '../../../../../src/clients/workbench/upstream/src/vs/platform/webWorker/browser/webWorkerService.js';
import { IEditorService } from '../../../../../src/clients/workbench/upstream/src/vs/workbench/services/editor/common/editorService.js';
import { IExtensionService } from '../../../../../src/clients/workbench/upstream/src/vs/workbench/services/extensions/common/extensions.js';
import { ISearchService } from '../../../../../src/clients/workbench/upstream/src/vs/workbench/services/search/common/search.js';
import { WorkspaceSearchService } from '../../../../../src/clients/workbench/src/search/searchService.js';
export { CancellationTokenSource } from '../../../../../src/clients/workbench/upstream/src/vs/base/common/cancellation.js';
export { ResourceSet } from '../../../../../src/clients/workbench/upstream/src/vs/base/common/map.js';
export { WorkspaceSearchProvider, textMatch } from '../../../../../src/clients/workbench/src/search/fileProvider.js';
export { resourceInQuery } from '../../../../../src/clients/workbench/src/search/query.js';
export { ExtUri, extUri } from '../../../../../src/clients/workbench/upstream/src/vs/base/common/resources.js';
export { gateway } from '../../../../../src/clients/workbench/src/agentApi.js';
export { workspaceFileUri, workspaceFileSource } from '../../../../../src/clients/workbench/src/workspaceFiles.js';
export { URI, WorkspaceSearchService };

/** 只提供内存文本存取，匹配使用真实上游引擎；不加载浏览器编辑器 UI。 */
export function model(resource: URI, text: string) {
  const lines = text.split('\n');
  return {
    uri: resource, calls: 0,
    getLineContent(line: number) { return lines[line - 1]; },
    getLineCount() { return lines.length; },
    getLanguageId() { return 'plaintext'; },
    findMatches(pattern: string, _scope: false, regex: boolean, sensitive: boolean, separators: string | null, captures: boolean, limit: number) {
      this.calls++;
      return TextModelSearch.findMatches(this as never, new SearchParams(pattern, regex, sensitive, separators),
        new Range(1, 1, lines.length, lines.at(-1)!.length + 1), captures, limit);
    },
  };
}

export function fixture(models: ReturnType<typeof model>[], open = models.map(model => model.uri), identity: IExtUri = extUri) {
  // 按真实 web.main 的顺序消费全体描述符；同一 ServiceCollection 最终只能得到项目实现。
  const services = new ServiceCollection(...getSingletonServiceDescriptors());
  const descriptor = services.get(ISearchService);
  const editors = { editors: open.map(resource => ({ resource })) };
  services.set(IModelService, { getModels: () => models } as never);
  services.set(IEditorService, editors as never);
  services.set(IFileService, { exists: async () => true, hasProvider: () => true, getProvider: () => { throw new Error('业务查询进入了浏览器 handle provider'); } } as never);
  services.set(ILogService, new NullLogService());
  services.set(ITelemetryService, { publicLog2() {} } as never);
  services.set(IExtensionService, { activateByEvent: async () => {}, whenInstalledExtensionsRegistered: async () => {} } as never);
  services.set(IUriIdentityService, { extUri: identity, onDidChange: Event.None } as never);
  services.set(IWebWorkerService, { createWorkerClient() { throw new Error('业务查询启动了浏览器文件 worker'); } } as never);
  const instantiation = new InstantiationService(services, true);
  const search = instantiation.invokeFunction(accessor => accessor.get(ISearchService));
  return { search, editors, descriptor, instantiation };
}
export { installSessionApiFetchMock, unwrapSessionApiFetch } from '../../../../../src/clients/web/src/api/session/sessionApiFetchMock.js';
