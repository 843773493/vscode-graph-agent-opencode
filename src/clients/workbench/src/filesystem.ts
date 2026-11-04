import { IndexedDB } from '../upstream/src/vs/base/browser/indexedDB.js';
import { IndexedDBFileSystemProvider } from '../upstream/src/vs/platform/files/browser/indexedDBFileSystemProvider.js';
import { IFileService } from '../upstream/src/vs/platform/files/common/files.js';
import { URI } from '../upstream/src/vs/base/common/uri.js';
import { installBackendFiles } from './workspaceFiles.js';

export const workspaceFolder = URI.parse('workbench:/Workspace');

// 必须先于上游工作区校验注册，不能等到 Workbench contribution 启动。
export async function installWorkspaceFileSystem(files: IFileService): Promise<void> {
  installBackendFiles(files);
  const database = await IndexedDB.create('boxteam-workbench-files', 1, ['files']);
  const provider = new IndexedDBFileSystemProvider('workbench', database, 'files', true);
  await provider.mkdir(workspaceFolder);
  files.registerProvider('workbench', provider);
}
