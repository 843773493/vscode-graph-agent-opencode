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
async function respond(value: unknown) {
  if (!process.stdout.write(JSON.stringify(value) + '\n')) await once(process.stdout, 'drain');
}
try {
  await respond({ ready: true });
  for await (const line of input) {
    const request: unknown = JSON.parse(line);
    if (typeof request !== 'object' || request === null || !('method' in request)) throw new Error('扩展管理控制请求无效');
    try {
      let result: unknown;
      if (request.method === 'inventory') result = await management.inventory();
      else if (request.method === 'environment') result = await management.environment();
      else if (request.method === 'install' && 'vsix_path' in request && typeof request.vsix_path === 'string' && isAbsolute(request.vsix_path)) {
        result = await management.install(request.vsix_path);
      } else if (request.method === 'configuration-view' && 'id' in request && typeof request.id === 'string'
        && 'folders' in request && Array.isArray(request.folders) && request.folders.every((folder: unknown): folder is NativeViewFolder =>
          typeof folder === 'object' && folder !== null && 'native_uri' in folder && typeof folder.native_uri === 'string'
          && 'name' in folder && typeof folder.name === 'string')) {
        result = await management.configurationFiles.prepareView(request.id, request.folders);
      } else if ('native_uri' in request && typeof request.native_uri === 'string') {
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
      console.error(error);
      const code = error instanceof FileOperationError && error.fileOperationResult === FileOperationResult.FILE_NOT_FOUND
        ? FileSystemProviderErrorCode.FileNotFound : toFileSystemProviderErrorCode(error instanceof Error ? error : null);
      await respond({ error: error instanceof Error ? error.message : String(error), code });
    }
  }
} finally {
  input.close();
  management.dispose();
}
// 原扫描服务带维护计时器；已收完输入、结清操作并释放服务后，刷新控制输出再退出，
// 不让这些后台计时器把父进程的正常关闭拖成强杀。
await new Promise<void>(resolve => process.stdout.end(resolve));
process.exit(0);
