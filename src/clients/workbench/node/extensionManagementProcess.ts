import { createInterface } from 'node:readline';
import { isAbsolute, join } from 'node:path';
import { once } from 'node:events';
import { createExtensionManagement } from './extensionManagement.js';
import { FileOperationError, FileOperationResult, FileSystemProviderErrorCode, toFileSystemProviderErrorCode } from '../upstream/src/vs/platform/files/common/files.js';
import type { NativeViewFolder } from './extensionConfiguration.js';

const directory = process.argv[2];
if (!directory || !isAbsolute(directory)) throw new Error('扩展管理必须使用目标 owner 指定的绝对目录');

// 私有父子控制流，不承载扩展 RPC，不接收客户端地址或任意脚本。
const management = await createExtensionManagement(directory, join(import.meta.dirname, 'builtin-extensions'));
const input = createInterface({ input: process.stdin, crlfDelay: Infinity });
// 先挂接行迭代器再公布 ready，避免父进程紧随握手发来的首条请求被 readline 丢失。
const requests = input[Symbol.asyncIterator]();
async function respond(value: unknown) {
  if (!process.stdout.write(JSON.stringify(value) + '\n')) await once(process.stdout, 'drain');
}
function isFolders(value: unknown): value is NativeViewFolder[] {
  return Array.isArray(value) && value.every((folder: unknown): folder is NativeViewFolder =>
    typeof folder === 'object' && folder !== null && 'native_uri' in folder && typeof folder.native_uri === 'string'
    && 'name' in folder && typeof folder.name === 'string');
}
function errorMessage(error: unknown): string {
  return error instanceof AggregateError ? [error.message, ...error.errors.map(errorMessage)].join('\n')
    : error instanceof Error ? error.message : String(error);
}
async function respondError(error: unknown): Promise<void> {
  console.error(error);
  const code = error instanceof FileOperationError && error.fileOperationResult === FileOperationResult.FILE_NOT_FOUND
    ? FileSystemProviderErrorCode.FileNotFound : toFileSystemProviderErrorCode(error instanceof Error ? error : null);
  await respond({ error: errorMessage(error), code });
}
let ready = false;
try {
  await management.ready;
  ready = true;
  await respond({ ready: true });
  for await (const line of requests) {
    const request: unknown = JSON.parse(line);
    if (typeof request !== 'object' || request === null || !('method' in request)) throw new Error('扩展管理控制请求无效');
    try {
      let result: unknown;
      if (request.method === 'inventory') result = await management.inventory();
      else if (request.method === 'environment') result = await management.environment();
      else if (request.method === 'install' && 'vsix_path' in request && typeof request.vsix_path === 'string' && isAbsolute(request.vsix_path)) {
        result = await management.install(request.vsix_path);
      } else if (request.method === 'configuration-view' && 'id' in request && typeof request.id === 'string'
        && 'folders' in request && isFolders(request.folders)) {
        result = await management.configurationFiles.prepareView(request.id, request.folders);
      } else if ('native_uri' in request && typeof request.native_uri === 'string') {
        if (!('view' in request)) throw new Error('配置请求缺少原会话的视图登记');
        if (request.view !== null) {
          const view = request.view;
          if (typeof view !== 'object' || !('id' in view) || typeof view.id !== 'string'
            || !('native_uri' in view) || typeof view.native_uri !== 'string' || !('folders' in view) || !isFolders(view.folders)) {
            throw new Error('配置请求的原会话视图登记无效');
          }
          management.configurationFiles.registerView({ id: view.id, native_uri: view.native_uri, folders: view.folders });
        }
        if (request.method === 'configuration-stat') result = await management.configurationFiles.stat(request.native_uri);
        else if (request.method === 'configuration-read') result = await management.configurationFiles.read(request.native_uri);
        else if (request.method === 'configuration-write' && 'content_base64' in request && typeof request.content_base64 === 'string'
          && 'options' in request && typeof request.options === 'object' && request.options !== null
          && 'create' in request.options && typeof request.options.create === 'boolean'
          && 'overwrite' in request.options && typeof request.options.overwrite === 'boolean'
          && 'unlock' in request.options && typeof request.options.unlock === 'boolean'
          && 'atomic' in request.options && typeof request.options.atomic === 'boolean'
          && 'append' in request.options && typeof request.options.append === 'boolean') {
          if (request.options.atomic) throw new Error('配置 Provider 尚未提供原子写入能力');
          result = await management.configurationFiles.write(request.native_uri, request.content_base64, {
            create: request.options.create, overwrite: request.options.overwrite, unlock: request.options.unlock,
            atomic: false, append: request.options.append,
          });
        } else throw new Error('不支持的原生配置操作');
      } else throw new Error('不支持的扩展管理操作或目标原生路径');
      await respond({ result });
    } catch (error) {
      // 业务失败保留原错误；未知结果不会在进程重启后重放。
      await respondError(error);
    }
    // 每个进程只处理本次独占安装域内的一条业务请求，不复用过时的 watcher/profile 内存。
    break;
  }
} catch (error) {
  if (ready) throw error;
  // 初始化明确失败也保留原原因；排空并正常退出后，后续显式请求可重新读取原 profile。
  await respondError(error);
} finally {
  input.close();
  process.stdin.destroy();
  await management.dispose();
}
await respond({ drained: true });
// 回执刷新后由事件循环自然退出；父进程还必须核实原句柄的退出码与原进程组。
await new Promise<void>(resolve => process.stdout.end(resolve));
