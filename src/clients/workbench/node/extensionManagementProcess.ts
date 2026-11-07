import { once } from 'node:events';
import { rename, rm, writeFile } from 'node:fs/promises';
import { createServer, type IncomingMessage, type ServerResponse } from 'node:http';
import { isAbsolute, join } from 'node:path';
import { closeHttpServer } from '../../../shared/serverHttpShutdown.mjs';
import { createJsonRequestReader } from '../../../shared/serverJsonRequest.mjs';
import { createRuntimeLogger, runtimeLoggingState } from '../../../shared/serverRuntimeLogging.mjs';
import { handleRuntimeLoggingRequest, verifyRuntimeLoggingToken } from '../../../shared/serverRuntimeLoggingControl.mjs';
import { createExtensionManagement } from './extensionManagement.js';
import { withExtensionDownloadTransport } from './extensionDownload.js';
import { acquireProcessLock } from '../upstream/src/vs/platform/agentHost/node/copilot/localModelBridge/shim/processLock.js';
import { ExtensionManagementError } from '../upstream/src/vs/platform/extensionManagement/common/extensionManagement.js';
import { FileOperationError, FileOperationResult, FileSystemProviderErrorCode, toFileSystemProviderErrorCode } from '../upstream/src/vs/platform/files/common/files.js';
import product from '../upstream/src/vs/platform/product/common/product.js';

const [directory, backendId] = process.argv.slice(2);
if (!directory || !isAbsolute(directory) || !backendId) throw new Error('扩展管理必须使用目标 owner 指定的绝对目录和安装域');
// 服务自己持有原版 OS 独占锁；Gateway 换代只交接连接，不抢锁或另启代理进程。
const lock = await acquireProcessLock(directory, 'extension-management');
if (typeof lock === 'string') throw new Error(`扩展管理安装域锁不可用: ${lock}`);
const serviceFile = join(directory, 'service.json');
const logger = createRuntimeLogger('extension-management', 'extensions.management');
const readJson = createJsonRequestReader(logger);
let published = false;
let temporary: string | undefined;
try {
  const identity = {
    backend_id: backendId, process_id: process.pid,
    runtime_instance_id: runtimeLoggingState('extension-management').runtime_instance_id,
    commit: product.commit, vscode_version: product.version, node_version: process.versions.node,
  };
  // 缺少原凭据立即失败，不能发布一个不可认证或无认证的服务。
  verifyRuntimeLoggingToken(process.env.BOXTEAM_RUNTIME_CONTROL_TOKEN, process.env.BOXTEAM_RUNTIME_CONTROL_TOKEN);
  const management = await createExtensionManagement(directory, join(import.meta.dirname, 'builtin-extensions'));
  let queue = Promise.resolve();
  let stopping = false;
  let disposed = false;
  const stopped = Promise.withResolvers<void>();
  function serialize(operation: () => Promise<void>): Promise<void> {
    const execution = queue.then(operation);
    // 只结清队列尾；原错误仍由该 HTTP 请求报告，不能毒化后续合法请求。
    queue = execution.catch(() => {});
    return execution;
  }
  const server = createServer((request, response) => {
    void handle(request, response).catch(error => {
      logger.error(error);
      if (response.headersSent) response.destroy(error instanceof Error ? error : undefined);
      else respond(response, failure(error), error instanceof SyntaxError ? 400
        : typeof error?.statusCode === 'number' ? error.statusCode : 500);
    });
  });
  let baseUrl: string;
  async function handle(request: IncomingMessage, response: ServerResponse): Promise<void> {
    if (request.socket.remoteAddress !== '127.0.0.1' || request.headers.origin !== undefined
      || Object.keys(request.headers).some(header => header.startsWith('sec-fetch-'))) {
      respond(response, { error: '扩展管理只接受本机管理通道' }, 403);
      return;
    }
    verifyRuntimeLoggingToken(request.headers['x-local-token'], process.env.BOXTEAM_RUNTIME_CONTROL_TOKEN);
    const pathname = request.url;
    if (await handleRuntimeLoggingRequest(request, response, { pathname, service: 'extension-management', readJson })) return;
    if (pathname === '/health' && request.method === 'GET') {
      respond(response, { ...identity, base_url: baseUrl, status: stopping ? 'stopping' : 'ok' });
      return;
    }
    if (request.method !== 'POST' || !['/management', '/publication', '/shutdown'].includes(pathname ?? '')) {
      respond(response, { error: '不支持的扩展管理入口' }, 404);
      return;
    }
    if (stopping) {
      respond(response, { error: '扩展管理服务正在排空' }, 503);
      return;
    }
    if (pathname === '/shutdown') {
      stopping = true;
      try {
        await queue;
        await management.dispose();
        disposed = true;
        respond(response, { drained: true });
        stopped.resolve();
      } catch (error) {
        stopped.reject(error);
        throw error;
      }
      return;
    }
    const input: unknown = await readJson(request);
    // 读取请求体时可能已有完整停机请求进入，不能在已排空队列之后追加新工作。
    if (stopping) {
      respond(response, { error: '扩展管理服务正在排空' }, 503);
      return;
    }
    await serialize(async () => {
      try {
        if (typeof input !== 'object' || input === null || !('method' in input) || typeof input.method !== 'string') {
          throw new TypeError('扩展管理控制请求无效');
        }
        let result: unknown;
        if (pathname === '/publication' && input.method === 'extension-publication'
          && 'profile_id' in input && typeof input.profile_id === 'string'
          && 'extension_id' in input && typeof input.extension_id === 'string'
          && 'extension_key' in input && typeof input.extension_key === 'string'
          && 'current_profile_member' in input && typeof input.current_profile_member === 'boolean') {
          result = await management.publication(input.profile_id, input.extension_id, input.extension_key, input.current_profile_member);
          if (response.destroyed) return;
          // 同一跨代队列保护描述核验和 Python 实际 I/O；关闭本次流才释放准入。
          const released = once(response, 'close');
          response.writeHead(200, { 'content-type': 'application/x-ndjson' });
          response.write(JSON.stringify({ result }) + '\n');
          await released;
          return;
        }
        if (pathname !== '/management') throw new TypeError('不支持的扩展管理操作');
        if (input.method === 'inventory') result = await management.inventory();
        else if (input.method === 'environment') result = await management.environment();
        else if (input.method === 'install' && 'vsix_path' in input && typeof input.vsix_path === 'string' && isAbsolute(input.vsix_path)) {
          const requestTransport = 'request_transport' in input ? input.request_transport : undefined;
          const vsixPath = input.vsix_path;
          result = await withExtensionDownloadTransport(requestTransport, backendId, () => management.install(vsixPath));
        } else if (input.method === 'uninstall' && 'profile_id' in input && typeof input.profile_id === 'string' && input.profile_id
          && 'extension_id' in input && typeof input.extension_id === 'string' && input.extension_id
          && 'version' in input && typeof input.version === 'string' && input.version) {
          result = await management.uninstall(input.profile_id, input.extension_id, input.version);
        } else throw new TypeError('不支持的扩展管理操作或目标原生路径');
        respond(response, { result });
      } catch (error) {
        logger.error(error);
        if (response.headersSent) throw error;
        // 业务失败保留原 code；取消及传输中断均不重放已接受的安装。
        respond(response, failure(error));
      }
    });
  }
  try {
    await management.ready;
    const listening = once(server, 'listening');
    server.listen(0, '127.0.0.1');
    await listening;
    const address = server.address();
    if (address === null || typeof address === 'string') throw new Error('扩展管理没有有效本机 HTTP 地址');
    baseUrl = `http://127.0.0.1:${address.port}`;
    temporary = join(directory, `service-${identity.runtime_instance_id}.tmp`);
    await writeFile(temporary, JSON.stringify({ ...identity, base_url: baseUrl }) + '\n', { mode: 0o600 });
    await rename(temporary, serviceFile);
    published = true;
    logger.info('扩展管理服务就绪: backend_id=%s', backendId);
    await stopped.promise;
  } finally {
    stopping = true;
    try {
      if (!disposed) { await queue; await management.dispose(); }
    } finally {
      if (server.listening) await closeHttpServer(server);
    }
  }
} finally {
  try { if (published) await rm(serviceFile); }
  finally {
    try { if (temporary) await rm(temporary, { force: true }); }
    finally { lock.dispose(); }
  }
}

function errorMessage(error: unknown): string {
  return error instanceof AggregateError ? [error.message, ...error.errors.map(errorMessage)].join('\n')
    : error instanceof Error ? error.message : String(error);
}
function failure(error: unknown) {
  const code = error instanceof ExtensionManagementError ? error.code
    : error instanceof FileOperationError && error.fileOperationResult === FileOperationResult.FILE_NOT_FOUND
    ? FileSystemProviderErrorCode.FileNotFound
    : error instanceof FileOperationError && error.fileOperationResult === FileOperationResult.FILE_TOO_LARGE
    ? FileSystemProviderErrorCode.FileTooLarge : toFileSystemProviderErrorCode(error instanceof Error ? error : null);
  return { error: errorMessage(error), code };
}
function respond(response: ServerResponse, value: unknown, status = 200): void {
  response.writeHead(status, { 'content-type': 'application/json; charset=utf-8' });
  response.end(JSON.stringify(value));
}
