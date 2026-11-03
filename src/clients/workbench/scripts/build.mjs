import { buildSourcePlugin, sourceTransform } from './sourceTransform.mjs';
import { buildExtensions } from './extensions.mjs';
import { build } from 'esbuild';
import { cp, mkdir, readFile, readdir, rm, writeFile } from 'node:fs/promises';
import { resolve, join, relative, dirname, extname, basename, sep } from 'node:path';
import { execFileSync } from 'node:child_process';
import { parseArgs } from 'node:util';

const root = resolve(import.meta.dirname, '..');
const upstream = join(root, 'upstream');
const { values } = parseArgs({ options: { entry: { type: 'string' }, outdir: { type: 'string' }, 'assets-only': { type: 'boolean', default: false } } });
const dist = resolve(values.outdir ?? join(root, 'dist'));
const entry = resolve(values.entry ?? join(root, 'src/main.ts'));
if (dist === root || basename(dist) !== 'dist') throw new Error('构建输出必须是独立的 dist 目录');
const commit = execFileSync('git', ['rev-parse', 'HEAD'], { cwd: upstream, encoding: 'utf8' }).trim();
const pinned = 'fc8954c90fe6dea1f4c411dd73fdfe5f1913b5ca';
if (commit !== pinned) throw new Error(`上游版本不匹配：${commit}，要求 ${pinned}`);
const product = JSON.parse(await readFile(join(upstream, 'product.json'), 'utf8'));
const upstreamPackage = JSON.parse(await readFile(join(upstream, 'package.json'), 'utf8'));
await rm(dist, { recursive: true, force: true });
await mkdir(dist, { recursive: true });
const extensions = await buildExtensions(root, dist, sourceTransform(root, { ...product, version: upstreamPackage.version, commit, quality: 'stable' }, []));
const inject = buildSourcePlugin(root, sourceTransform(root, { ...product, version: upstreamPackage.version, commit, quality: 'stable' }, extensions));
await writeFile(join(dist, 'builtin-extensions.json'), JSON.stringify(extensions));
const workers = [
  'vs/editor/common/services/editorWebWorkerMain',
  'vs/workbench/api/worker/extensionHostWorkerMain',
  'vs/workbench/contrib/notebook/common/services/notebookWebWorkerMain',
  'vs/workbench/services/languageDetection/browser/languageDetectionWebWorkerMain',
  'vs/workbench/services/search/worker/localFileSearchMain',
  'vs/workbench/contrib/output/common/outputLinkComputerMain',
  'vs/workbench/contrib/mediaPreview/browser/imageEncoderWorkerMain',
  'vs/workbench/services/textMate/browser/backgroundTokenization/worker/textMateTokenizationWorker.workerMain',
  ...['linux', 'darwin', 'win'].map(os => `vs/workbench/services/keybinding/browser/keyboardLayouts/layout.contribution.${os}`),
];
const options = {
  bundle: true, format: 'esm', platform: 'browser', target: 'es2024', metafile: true,
  nodePaths: [join(root, 'node_modules')],
  tsconfig: join(root, 'tsconfig.jsonc'),
  // 宿主和复用组件必须共享同一 React 实例。
  alias: { react: join(root, 'node_modules/react'), 'react-dom': join(root, 'node_modules/react-dom') },
  loader: { '.ttf': 'file', '.svg': 'file', '.png': 'file', '.sh': 'file', '.wasm': 'file' },
  assetNames: 'media/[name]-[hash]', plugins: [inject], logLevel: 'info',
};
const immutableAssets = new Set();
function recordAssets(result) {
  // 只登记本次 esbuild 文件 loader 产出的内容地址，不能按请求扩展名猜缓存期限。
  for (const [path, output] of Object.entries(result.metafile.outputs)) {
    if (!output.entryPoint && !['.js', '.css', '.map'].includes(extname(path))) {
      immutableAssets.add(relative(dist, resolve(path)).split(sep).join('/'));
    }
  }
}
// Vite 直接转换业务入口；开发资源准备不再额外编译无人使用的发布版主包。
if (!values['assets-only']) {
  await build({ ...options, entryPoints: { main: entry }, outdir: join(dist, 'out') }).then(async result => {
    recordAssets(result);
    await writeFile(join(dist, 'build-meta.json'), JSON.stringify(result.metafile));
  });
}
for (const entry of workers) {
  recordAssets(await build({ ...options, entryPoints: [join(upstream, 'src', `${entry}.ts`)], outfile: join(dist, 'out', `${entry}.js`) }));
}
// 上游通过 FileAccess 按原路径加载的静态资源，保留相同目录结构。
async function copyResources(folder) {
  for (const item of await readdir(folder, { withFileTypes: true })) {
    const path = join(folder, item.name);
    if (item.isDirectory()) {
      if (!['test', 'node', 'electron-main', 'electron-browser', 'electron-utility'].includes(item.name)) await copyResources(path);
    } else if (!['.ts', '.css', '.map'].includes(extname(path))) {
      const target = join(dist, 'out', relative(join(upstream, 'src'), path));
      await mkdir(dirname(target), { recursive: true });
      await cp(path, target);
    }
  }
}
await copyResources(join(upstream, 'src/vs'));
// AMD 和 WASM 加载器使用独立 Web 依赖的原始发行文件。
for (const name of Object.keys(JSON.parse(await readFile(join(upstream, 'remote/web/package.json'), 'utf8')).dependencies)) {
  await cp(join(root, 'node_modules', name), join(dist, 'node_modules', name), { recursive: true, dereference: true });
}
await cp(join(root, 'index.html'), join(dist, 'index.html'));
// 私有回调与 Web 共用唯一源文件，不加载 Workbench 或第三方资源。
await cp(join(root, '../web/mcp-oauth-callback.html'), join(dist, 'mcp-oauth-callback.html'));
for (const name of ['LICENSE.txt', 'ThirdPartyNotices.txt']) await cp(join(upstream, name), join(dist, name));
await writeFile(join(dist, 'version.json'), JSON.stringify({ commit, version: upstreamPackage.version, extensions: extensions.length }));
await writeFile(join(dist, 'immutable-assets.json'), JSON.stringify([...immutableAssets].sort()));
console.log(`${values['assets-only'] ? 'Workbench 开发资源' : '完整 Workbench'}构建完成：${commit}，${extensions.length} 个内置 Web 扩展`);
