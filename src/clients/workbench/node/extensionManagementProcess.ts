import { createInterface } from 'node:readline';
import { isAbsolute, join } from 'node:path';
import { once } from 'node:events';
import { createExtensionManagement } from './extensionManagement.js';

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
      else if (request.method === 'environment') result = management.environment();
      else if (request.method === 'install' && 'vsix_path' in request && typeof request.vsix_path === 'string' && isAbsolute(request.vsix_path)) {
        result = await management.install(request.vsix_path);
      } else throw new Error('不支持的扩展管理操作或目标原生路径');
      await respond({ result });
    } catch (error) {
      // 业务失败保留原错误；未知结果不会在进程重启后重放。
      console.error(error);
      await respond({ error: error instanceof Error ? error.message : String(error) });
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
