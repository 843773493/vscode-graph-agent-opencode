import react from '@vitejs/plugin-react-swc';
import { buildWorkbenchDependencies } from './devDependencies.mjs';
import compression from 'compression';
import { mkdir, readFile, readdir, realpath, rename, rm } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import { resolve, join, basename } from 'node:path';
import { parseArgs } from 'node:util';
import { spawn } from 'node:child_process';
import { sourceTransform } from './sourceTransform.mjs';
import { createWorkbenchServer } from './server.mjs';

export default async function () {
  const root = resolve(import.meta.dirname, '..');
  const codiconsAssets = await realpath(join(root, 'node_modules/@vscode/codicons/dist'));
  const { values } = parseArgs({ options: {
    port: { type: 'string', default: process.env.BOXTEAM_DEV_FRONTEND_PORT ?? '8411' },
    host: { type: 'string', default: process.env.BOXTEAM_DEV_LISTEN_HOST ?? '127.0.0.1' },
    gateway: { type: 'string', default: `http://127.0.0.1:${process.env.BOXTEAM_GATEWAY_PORT ?? '8014'}` },
    outdir: { type: 'string', default: join(root, 'dist') },
    cache: { type: 'string' },
  } });
  const assetsRoot = resolve(values.outdir);
  if (assetsRoot === root || basename(assetsRoot) !== 'dist') throw new Error('开发资源输出必须是独立的 dist 目录');
  const assetsStage = join(assetsRoot, '..', '.dev-next', 'dist');
  const assetsPrevious = join(assetsRoot, '..', '.dev-previous', 'dist');
  await rm(join(assetsRoot, '..', '.dev-next'), { recursive: true, force: true });
  await mkdir(join(assetsStage, '..'), { recursive: true });

  // 新代完整就绪后再替换发布静态树，配置重启期间旧页面仍能读取旧 NLS manifest。
  await new Promise((fulfill, reject) => {
    const build = spawn('bun', ['scripts/build.mjs', '--assets-only', '--outdir', assetsStage], { cwd: root, stdio: 'inherit' });
    build.on('error', reject);
    build.on('exit', code => code === 0 ? fulfill() : reject(new Error(`Workbench 开发资源构建失败 (${code})`)));
  });
  const loadJSON = async path => JSON.parse(await readFile(path, 'utf8'));
  const version = await loadJSON(join(assetsStage, 'version.json'));
  const product = await loadJSON(join(root, 'upstream/product.json'));
  const extensions = await loadJSON(join(assetsStage, 'builtin-extensions.json'));
  const localizationAssets = await loadJSON(join(assetsStage, 'localization-assets.json'));
  const transformSource = sourceTransform(root, { ...product, ...version, quality: 'stable' }, extensions, new Map(), localizationAssets);
  const dependencies = await buildWorkbenchDependencies(root, transformSource, join(assetsStage, 'out'));
  const webPackages = ['react-markdown', 'react-virtuoso', 'rehype-sanitize', 'remark-gfm'];
  let assets;
  const { promise: ready, resolve: markReady, reject: markFailed } = Promise.withResolvers();
  const startedAt = performance.now();

  return {
    root,
    publicDir: false,
    cacheDir: values.cache,
    plugins: [
      {
        name: 'boxteam-workbench-source',
        enforce: 'pre',
        async configResolved(config) {
          // 翻译输入和转换配置一样沿 Vite 原重启链生效，不能只更新 JSON 模块留下旧 NLS。
          const localizationRoot = join(root, 'resources/localization');
          config.configFileDependencies.push(...[
            join(root, '../shared/core/i18n/messages/en.json'),
            join(root, '../shared/core/i18n/messages/zh-cn.json'),
            join(localizationRoot, 'zh-cn-upstream-main.json'),
            join(localizationRoot, 'zh-cn-project.json'),
            ...(await readdir(join(localizationRoot, 'extensions'))).filter(name => name.endsWith('.i18n.json'))
              .map(name => join(localizationRoot, 'extensions', name)),
          ].map(file => file.replaceAll('\\', '/')));
        },
        configureServer(vite) {
          // 页面源码在完整静态代发布并预热前不能开始导入旧配置指向的新资源。
          vite.middlewares.use((request, response, next) => {
            const pathname = new URL(request.url, 'http://localhost').pathname;
            if (pathname === '/health' || pathname.startsWith('/api/')) { next(); return; }
            void ready.then(() => next(), next);
          });
          vite.httpServer.once('listening', () => {
            const warmup = async () => {
              await rm(join(assetsPrevious, '..'), { recursive: true, force: true });
              if (existsSync(assetsRoot)) await rename(assetsRoot, assetsPrevious);
              try {
                await rename(assetsStage, assetsRoot);
              } catch (error) {
                if (existsSync(assetsPrevious)) await rename(assetsPrevious, assetsRoot);
                throw error;
              }
              await rm(join(assetsPrevious, '..'), { recursive: true, force: true });
              assets = createWorkbenchServer({ root: assetsRoot });
              const client = vite.environments.client;
              await Promise.all(['/src/bootstrap.ts', '/src/agent.contribution.ts'].map(id => client.transformRequest(id)));
              await client.waitForRequestsIdle();
              markReady();
              console.log('Workbench 开发入口预热完成：' + Math.round(performance.now() - startedAt) + 'ms');
            };
            void warmup().catch(markFailed);
          });
          ready.catch(error => {
            vite.config.logger.error(error.stack ?? String(error));
            void vite.close();
            process.exitCode = 1;
          });
          // 压缩开发模块与资源，API/SSE 仍按原始流直接代理。
          vite.middlewares.use(compression({ filter: (request, response) =>
            !request.url.startsWith('/api/') && compression.filter(request, response) }));
          vite.middlewares.use(async (request, response, next) => {
            const url = new URL(request.url, 'http://localhost');
            if (request.method === 'GET' && url.pathname === '/health') {
              try { await ready; } catch (error) { next(error); return; }
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
          vite.httpServer.on('close', () => assets?.close());
        },
        transformIndexHtml: {
          order: 'pre',
          handler(html) {
            return html.replace('<link rel="stylesheet" href="/out/main.css">', '')
              .replace('src="/out/main.js"', 'src="/src/bootstrap.ts"');
          },
        },
        async resolveId(id, importer) {
          if (id.startsWith('/out/dev-upstream/')) return join(assetsRoot, id.slice(1));
          if (id.endsWith('.css?text')) {
            return this.resolve(id.replace(/\?text$/, '?inline'), importer, { skipSelf: true });
          }
        },
        transform(code, id) {
          const sourcePath = id.split('?', 1)[0];
          if (sourcePath.startsWith(join(root, 'src'))) return dependencies.transform(code, sourcePath);
        },
      },
      react({
        tsDecorators: true,
        devTarget: 'es2024',
        // 原版 Workbench 的基类会调用子类初始化，必须沿用上游的字段赋值语义。
        useAtYourOwnRisk_mutateSwcOptions(options) { options.jsc.transform.useDefineForClassFields = false; },
      }),
    ],
    resolve: {
      dedupe: ['react', 'react-dom'],
      // CSS 的 url() 使用 Vite 资源解析器，字体路径必须在这里参与解析。
      alias: [
        ...webPackages.map(name => ({ find: name, replacement: join(root, '../web/node_modules', name) })),
        { find: './codicon.ttf', replacement: join(root, 'node_modules/@vscode/codicons/dist/codicon.ttf') },
      ],
    },
    define: { __BOXTEAM_NLS_MANIFEST__: JSON.stringify(version.localization.nlsManifest) },
    // 上游模块在同次 esbuild split build 中共享身份，React 继续由 Vite 预构建。
    optimizeDeps: {
      include: [
        'react', 'react/jsx-runtime', 'react/jsx-dev-runtime', 'react-dom', 'react-dom/client',
        ...webPackages,
      ],
      noDiscovery: true,
    },
    server: {
      host: values.host, port: Number(values.port), strictPort: true,
      fs: { allow: [resolve(root, '..'), assetsRoot, codiconsAssets] },
      proxy: { '/api': { target: values.gateway, changeOrigin: true, xfwd: true, ws: true, timeout: 0 } },
    },
  };
}
