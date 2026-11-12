import { createServer } from 'vite';
import { fileURLToPath } from 'node:url';

// 由 Vite 加载配置及其转换脚本依赖，修改后沿原机制重载配置。
const server = await createServer({ configFile: fileURLToPath(new URL('./devConfig.mjs', import.meta.url)) });
await server.listen();
server.printUrls();
