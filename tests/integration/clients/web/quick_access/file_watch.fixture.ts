import { installBackendFiles } from '../../../../../src/clients/workbench/src/workspaceFiles.js';
import type { IFileService, IFileSystemProvider } from '../../../../../src/clients/workbench/upstream/src/vs/platform/files/common/files.js';
export { FileChangeType, FileSystemProviderErrorCode, toFileSystemProviderErrorCode } from '../../../../../src/clients/workbench/upstream/src/vs/platform/files/common/files.js';
export { gateway } from '../../../../../src/clients/workbench/src/agentApi.js';
export { workspaceFileUri } from '../../../../../src/clients/workbench/src/workspaceFiles.js';
export { installSessionApiFetchMock, unwrapSessionApiFetch } from '../../../../../src/clients/web/src/api/session/sessionApiFetchMock.js';

/** 通过真实注册入口取得 Provider；FileService 注册及出站 HTTP 是显式装具边界。 */
export function fileProvider(): IFileSystemProvider {
  let registered: IFileSystemProvider | undefined;
  installBackendFiles({ registerProvider(_scheme: string, provider: IFileSystemProvider) { registered = provider; } } as unknown as IFileService);
  if (!registered) throw new Error('原文件 Provider 未注册');
  return registered;
}
