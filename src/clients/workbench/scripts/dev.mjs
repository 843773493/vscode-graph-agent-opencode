import { createServer } from 'vite';
import react from '@vitejs/plugin-react-swc';
import compression from 'compression';
import { readFile } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import { resolve, join } from 'node:path';
import { parseArgs } from 'node:util';
import { spawn } from 'node:child_process';
import { buildSourcePlugin, sourceTransform } from './sourceTransform.mjs';
import { createWorkbenchServer } from './server.mjs';
import { workbenchDependencies } from './devDependencies.mjs';

const root = resolve(import.meta.dirname, '..');
const { values } = parseArgs({ options: {
  port: { type: 'string', default: process.env.BOXTEAM_DEV_FRONTEND_PORT ?? '8411' },
  host: { type: 'string', default: process.env.BOXTEAM_DEV_LISTEN_HOST ?? '127.0.0.1' },
  gateway: { type: 'string', default: `http://127.0.0.1:${process.env.BOXTEAM_GATEWAY_PORT ?? '8014'}` },
  outdir: { type: 'string', default: join(root, 'dist') },
  cache: { type: 'string' },
} });
const assetsRoot = resolve(values.outdir);

// 扩展、Worker 和 FileAccess 资源仍来自唯一的发布构建；业务源码直接交给 Vite。
await new Promise((fulfill, reject) => {
  const build = spawn('bun', ['scripts/build.mjs', '--assets-only', '--outdir', assetsRoot], { cwd: root, stdio: 'inherit' });
  build.on('error', reject);
  build.on('exit', code => code === 0 ? fulfill() : reject(new Error(`Workbench 开发资源构建失败 (${code})`)));
});
const loadJSON = async path => JSON.parse(await readFile(path, 'utf8'));
const version = await loadJSON(join(assetsRoot, 'version.json'));
const product = await loadJSON(join(root, 'upstream/product.json'));
const extensions = await loadJSON(join(assetsRoot, 'builtin-extensions.json'));
const transformSource = sourceTransform(root, { ...product, ...version, quality: 'stable' }, extensions);
const dependencies = await workbenchDependencies(root);
const assets = createWorkbenchServer({ root: assetsRoot });
const esbuildOptions = { tsconfigRaw: { compilerOptions: { experimentalDecorators: true, useDefineForClassFields: false } } };
const { promise: ready, resolve: markReady } = Promise.withResolvers();

const server = await createServer({
  configFile: false,
  root,
  publicDir: false,
  cacheDir: values.cache,
  plugins: [
    {
      name: 'boxteam-workbench-source',
      enforce: 'pre',
      configureServer(vite) {
        // 压缩开发模块与资源，API/SSE 仍按原始流直接代理。
        vite.middlewares.use(compression({ filter: (request, response) =>
          !request.url.startsWith('/api/') && compression.filter(request, response) }));
        vite.middlewares.use(async (request, response, next) => {
          const url = new URL(request.url, 'http://localhost');
          if (request.method === 'GET' && url.pathname === '/health') {
            await ready;
            response.setHeader('Content-Type', 'application/json; charset=utf-8');
            response.end(JSON.stringify({ status: 'ok', service: 'frontend' }));
            return;
          }
          if (url.pathname === '/mcp-oauth-callback.html'
            || url.pathname.startsWith('/out/') || url.pathname.startsWith('/extensions/')
            || (url.pathname.startsWith('/node_modules/') && !url.search
              && existsSync(join(assetsRoot, url.pathname)))) {
            assets.emit('request', request, response);
          } else next();
        });
        vite.httpServer.on('close', () => assets.close());
      },
      transformIndexHtml: {
        order: 'pre',
        handler(html) {
          return html.replace('<link rel="stylesheet" href="/out/main.css">', '')
            .replace('src="/out/main.js"', 'src="/src/main.ts"');
        },
      },
      async resolveId(id, importer) {
        if (id.endsWith('.css?text')) {
          return this.resolve(id.replace(/\?text$/, '?inline'), importer, { skipSelf: true });
        }
        if (importer?.includes('/upstream/') && id.includes('node_modules/')) {
          return this.resolve(join(root, 'node_modules', id.split('node_modules/').at(-1)), importer, { skipSelf: true });
        }
      },
      transform(code, id) {
        if (id.startsWith(join(root, 'src'))) return dependencies.transform(code, id);
        if (id.endsWith('.ts')) return transformSource(id, code);
      },
    },
    react({
      tsDecorators: true,
      // 原版 Workbench 的基类会调用子类初始化，必须沿用上游的字段赋值语义。
      useAtYourOwnRisk_mutateSwcOptions(options) { options.jsc.transform.useDefineForClassFields = false; },
    }),
  ],
  resolve: {
    dedupe: ['react', 'react-dom'],
    // CSS 的 url() 使用 Vite 资源解析器，字体路径必须在这里参与解析。
    alias: [dependencies.alias, { find: './codicon.ttf', replacement: join(root, 'node_modules/@vscode/codicons/dist/codicon.ttf') }],
  },
  esbuild: esbuildOptions,
  optimizeDeps: {
    entries: ['index.html'],
    include: ['react', 'react-dom/client', 'react/jsx-dev-runtime', ...dependencies.include],
    // 固定上游仍保留 source map，但不把整套源码重复内嵌进每次开发响应。
    esbuildOptions: { ...esbuildOptions, sourcesContent: false,
      plugins: [dependencies.plugin, buildSourcePlugin(root, transformSource)] },
  },
  server: {
    host: values.host, port: Number(values.port), strictPort: true,
    fs: { allow: [resolve(root, '..')] },
    proxy: { '/api': { target: values.gateway, changeOrigin: true, xfwd: true, ws: true, timeout: 0 } },
  },
});
await server.listen();
// 原版工作台的静态依赖图很大，先完成 Vite 转换，再向统一开发栈报告可用。
const startedAt = performance.now();
await Promise.all(['/src/main.ts', '/src/agent.contribution.ts', '/src/react/mcpAuthorization.tsx']
  .map(entry => server.environments.client.transformRequest(entry)));
await server.environments.client.waitForRequestsIdle();
const optimizer = server.environments.client.depsOptimizer;
await optimizer.scanProcessing;
await Promise.all(Object.values(optimizer.metadata.discovered).map(dependency => dependency.processing));
markReady();
console.log(`Workbench Vite 入口预热完成：${Math.round(performance.now() - startedAt)}ms`);
server.printUrls();
