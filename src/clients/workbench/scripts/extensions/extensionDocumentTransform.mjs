import { join } from 'node:path';

export const extensionDocumentSourcePaths = ['src/vs/workbench/api/browser/mainThreadDocuments.ts'];

export function transformExtensionDocuments(root, path, source, replaceOnce) {
  if (path !== join(root, 'upstream', extensionDocumentSourcePaths[0])) return source;
  // 文件事件只属于仍有同步 tracker 的模型；窗口模型退出追踪后不再转发迟到事件。
  return replaceOnce(source, 'return !!model && shouldSynchronizeModel(model);', 'return !!model && this._modelTrackers.has(resource) && shouldSynchronizeModel(model);');
}
