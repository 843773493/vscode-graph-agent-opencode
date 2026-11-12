import { buildSourcePlugin, pinnedUpstreamCommit, sourceTransform, transformedSourcePaths } from './sourceTransform.mjs';
import { actionTitleKeys, parseUniqueJson, writeActionTitleProjection, writeBootstrapMessageProjection } from './actionTitles.mjs';
import { buildExtensions } from './extensions.mjs';
import { build } from 'esbuild';
import { createHash } from 'node:crypto';
import { cp, mkdir, readFile, readdir, rm, writeFile } from 'node:fs/promises';
import { resolve, join, relative, dirname, basename, extname, sep } from 'node:path';
import { execFileSync } from 'node:child_process';
import { parseArgs } from 'node:util';

const root = resolve(import.meta.dirname, '..');
await writeActionTitleProjection(root, actionTitleKeys);
await writeBootstrapMessageProjection(root);
const upstream = join(root, 'upstream');
const { values } = parseArgs({ options: { entry: { type: 'string' }, outdir: { type: 'string' }, 'assets-only': { type: 'boolean', default: false } } });
const dist = resolve(values.outdir ?? join(root, 'dist'));
const entry = resolve(values.entry ?? join(root, 'src/bootstrap.ts'));
if (dist === root || basename(dist) !== 'dist') throw new Error('构建输出必须是独立的 dist 目录');
const commit = execFileSync('git', ['rev-parse', 'HEAD'], { cwd: upstream, encoding: 'utf8' }).trim();
const pinned = 'fc8954c90fe6dea1f4c411dd73fdfe5f1913b5ca';
if (commit !== pinned) throw new Error(`上游版本不匹配：${commit}，要求 ${pinned}`);
const product = JSON.parse(await readFile(join(upstream, 'product.json'), 'utf8'));
const upstreamPackage = JSON.parse(await readFile(join(upstream, 'package.json'), 'utf8'));
const englishPath = join(root, '../shared/core/i18n/messages/en.json');
const chinesePath = join(root, '../shared/core/i18n/messages/zh-cn.json');
const englishCatalog = await readFile(englishPath);
const chineseCatalog = await readFile(chinesePath);
const englishMessages = parseUniqueJson(englishPath, englishCatalog.toString('utf8'));
const chineseMessages = parseUniqueJson(chinesePath, chineseCatalog.toString('utf8'));
const productCatalogSha256 = createHash('sha256').update(englishCatalog).update('\0').update(chineseCatalog).digest('hex');
const englishKeys = Object.keys(englishMessages).sort();
if (JSON.stringify(englishKeys) !== JSON.stringify(Object.keys(chineseMessages).sort())) throw new Error('产品中英文 catalog 的 key 不一致');
const placeholders = message => [...message.matchAll(/\{(\d+)\}/g)].map(match => match[1]).sort().join(',');
for (const key of englishKeys) {
  if (placeholders(englishMessages[key]) !== placeholders(chineseMessages[key])) throw new Error(`产品翻译占位符不匹配：${key}`);
}
const webSources = [];
async function collectWebSources(folder) {
  for (const item of await readdir(folder, { withFileTypes: true })) {
    const path = join(folder, item.name);
    if (item.isDirectory()) await collectWebSources(path);
    else if (/\.tsx?$/.test(item.name)) webSources.push(path);
  }
}
await collectWebSources(join(root, '../web/src'));
await collectWebSources(join(root, 'src'));
for (const path of webSources) {
  const source = await readFile(path, 'utf8');
  for (const match of source.matchAll(/\bt\(\s*(['"])([^'"]+)\1/g)) {
    if (!(match[2] in englishMessages)) throw new Error(`缺少产品翻译 key ${match[2]}：${path}`);
  }
}
await rm(dist, { recursive: true, force: true });
await mkdir(dist, { recursive: true });
await mkdir(join(dist, 'out'), { recursive: true });
const nlsCollector = new Map();
const immutableAssets = new Set();
const localizationDirectory = join(root, 'resources/localization/extensions');
const translationPath = join(root, 'resources/localization/zh-cn-upstream-main.json');
const translationSource = await readFile(translationPath);
const projectTranslationPath = join(root, 'resources/localization/zh-cn-project.json');
const projectTranslationSource = await readFile(projectTranslationPath);
const projectTranslations = parseUniqueJson(projectTranslationPath, projectTranslationSource.toString('utf8')).contents;
const availableLocalizationAssets = {};
const availableExtensionLocalizationHashes = {};
const localizationSources = new Map();
const localizationFiles = await readdir(localizationDirectory);
for (const name of localizationFiles) {
  if (!name.endsWith('.i18n.json')) continue;
  const id = name.slice(0, -'.i18n.json'.length);
  const source = await readFile(join(localizationDirectory, name));
  parseUniqueJson(join(localizationDirectory, name), source.toString('utf8'));
  const asset = `localization/extensions/${id}-${createHash('sha256').update(source).digest('hex').slice(0, 16)}.i18n.json`;
  availableLocalizationAssets[id] = asset;
  availableExtensionLocalizationHashes[id] = createHash('sha256').update(source).digest('hex');
  localizationSources.set(id, source);
}
const nlsBuildHash = createHash('sha256').update(commit).update(englishCatalog).update(chineseCatalog).update(translationSource).update(projectTranslationSource);
for (const [id, source] of [...localizationSources].sort(([left], [right]) => left.localeCompare(right))) nlsBuildHash.update(id).update(source);
async function hashScripts(directory = join(root, 'scripts')) {
  for (const entry of (await readdir(directory, { withFileTypes: true })).sort((left, right) => left.name.localeCompare(right.name))) {
    const path = join(directory, entry.name);
    if (entry.isDirectory()) await hashScripts(path);
    else if (entry.isFile() && entry.name.endsWith('.mjs') && !entry.name.includes('.test.')) {
      nlsBuildHash.update(relative(root, path)).update(await readFile(path));
    }
  }
}
await hashScripts();
for (const path of [...transformedSourcePaths].sort()) nlsBuildHash.update(path).update(await readFile(join(upstream, path)));
const nlsBuildId = nlsBuildHash.digest('hex');
const nlsManifestPath = `/out/nls-assets-${nlsBuildId}.json`;
const discoverSource = sourceTransform(root, { ...product, version: upstreamPackage.version, commit, quality: 'stable' }, [], nlsCollector, availableLocalizationAssets);
const extensions = await buildExtensions(root, dist, discoverSource);
const extensionIds = extensions.map(extension => `vscode.${extension.packageJSON.name}`);
const localizationAssets = {};
const extensionLocalizationHashes = {};
for (const id of extensionIds) {
  const asset = availableLocalizationAssets[id];
  const source = localizationSources.get(id);
  if (!asset || !source) throw new Error(`缺少内置扩展本地化资源：${id}`);
  localizationAssets[id] = asset;
  extensionLocalizationHashes[id] = availableExtensionLocalizationHashes[id];
  const target = join(dist, 'out', asset);
  await mkdir(dirname(target), { recursive: true });
  await writeFile(target, source);
  immutableAssets.add(`out/${asset}`);
}
await writeFile(join(dist, 'localization-assets.json'), JSON.stringify(localizationAssets));
const transform = sourceTransform(root, { ...product, version: upstreamPackage.version, commit, quality: 'stable' }, extensions, nlsCollector, localizationAssets);
const inject = buildSourcePlugin(root, transform);
if (values['assets-only']) {
  // Vite 按需转换主程序；先沿同一转换器提取注入文案，才能在启动前供应完整资源。
  for (const path of transformedSourcePaths.filter(path => path.startsWith('src/'))) {
    const source = join(upstream, path);
    await transform(source, await readFile(source, 'utf8'));
  }
}
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
  bundle: true, format: 'esm', platform: 'browser', target: 'es2024', minify: true,
  nodePaths: [join(root, 'node_modules')],
  tsconfig: join(root, 'tsconfig.jsonc'),
  // 宿主和复用组件必须共享同一 React 实例。
  alias: { react: join(root, 'node_modules/react'), 'react-dom': join(root, 'node_modules/react-dom') },
  loader: { '.ttf': 'file', '.svg': 'file', '.png': 'file', '.sh': 'file', '.wasm': 'file' },
  assetNames: 'media/[name]-[hash]', plugins: [inject], logLevel: 'info',
  define: { __BOXTEAM_NLS_MANIFEST__: JSON.stringify(nlsManifestPath) },
};
async function recordAssets() {
  // esbuild 生成的内容地址资源仅落在这两个目录；NLS manifest 等固定名字资源不缓存为 immutable。
  for (const folder of ['chunks', 'media']) {
    async function collect(path) {
      for (const item of await readdir(path, { withFileTypes: true })) {
        const target = join(path, item.name);
        if (item.isDirectory()) await collect(target);
        else immutableAssets.add(relative(dist, target).split(sep).join('/'));
      }
    }
    const path = join(dist, 'out', folder);
    if (await readdir(path).then(() => true, error => error.code === 'ENOENT' ? false : Promise.reject(error))) await collect(path);
  }
}
// Vite 直接转换业务入口；开发资源准备不再额外编译无人使用的发布版主包。
if (!values['assets-only']) {
  await build({ ...options, entryPoints: { main: entry }, splitting: true, chunkNames: 'chunks/[name]-[hash]',
    outdir: join(dist, 'out') });
  await recordAssets();
}
for (const entry of workers) {
  await build({ ...options, entryPoints: [join(upstream, 'src', `${entry}.ts`)], outfile: join(dist, 'out', `${entry}.js`) });
  await recordAssets();
}
const translationFile = parseUniqueJson(translationPath, translationSource.toString('utf8'));
const extensionTranslations = {};
for (const id of extensionIds) {
  const path = join(localizationDirectory, `${id}.i18n.json`);
  const resource = parseUniqueJson(path, localizationSources.get(id).toString('utf8'));
  extensionTranslations[id] = resource.contents?.bundle ?? {};
}
const missing = [];
const localizedWorkbenchMessages = { ...translationFile.contents };
for (const [moduleId, translations] of Object.entries(projectTranslations)) {
  for (const [key, translated] of Object.entries(translations)) {
    const identity = `${moduleId}#${key}`;
    const original = nlsCollector.get(identity);
    if (original === undefined) {
      if (!values['assets-only']) throw new Error(`项目补译没有有效 NLS 调用：${identity}`);
    } else if (typeof translated !== 'string' || placeholders(translated) !== placeholders(original)) {
      throw new Error(`项目补译占位符不匹配：${identity}`);
    }
  }
  localizedWorkbenchMessages[moduleId] = { ...localizedWorkbenchMessages[moduleId], ...translations };
}
for (const [identity, original] of nlsCollector) {
  const separator = identity.indexOf('#');
  const key = identity.slice(separator + 1);
  if (!key.startsWith('workbench.') || !Object.hasOwn(englishMessages, key)) continue;
  if (englishMessages[key] !== original) throw new Error(`Workbench NLS original 与产品 catalog 不匹配：${identity}`);
  const translated = chineseMessages[key];
  const moduleId = identity.slice(0, separator);
  localizedWorkbenchMessages[moduleId] = { ...localizedWorkbenchMessages[moduleId], [key]: translated };
}
for (const [identity, original] of nlsCollector) {
  const separator = identity.indexOf('#');
  const moduleId = identity.slice(0, separator);
  const extension = /^extensions\/([^/]+)/.exec(moduleId)?.[1];
  const translated = extension
    ? extensionTranslations[`vscode.${extension}`]?.[original]
    : localizedWorkbenchMessages[moduleId]?.[identity.slice(separator + 1)];
  if (typeof translated !== 'string') missing.push({ identity, reason: 'missing-translation' });
  else if (placeholders(translated) !== placeholders(original)) missing.push({ identity, reason: 'placeholder-mismatch' });
}
await writeFile(join(dist, 'out/nls.zh-cn.missing.json'), JSON.stringify(missing, null, 2));
const languageResources = {
  en: Buffer.from(JSON.stringify({ buildId: nlsBuildId, contents: {}, extensions: {} })),
  'zh-cn': Buffer.from(JSON.stringify({ buildId: nlsBuildId, contents: localizedWorkbenchMessages, extensions: extensionTranslations })),
};
const languageAssets = {};
for (const [locale, contents] of Object.entries(languageResources)) {
  const filename = `nls-${locale}-${createHash('sha256').update(contents).digest('hex').slice(0, 16)}.json`;
  await writeFile(join(dist, 'out', filename), contents);
  languageAssets[locale] = `/out/${filename}`;
  immutableAssets.add(`out/${filename}`);
}
const nlsManifest = { buildId: nlsBuildId, assets: languageAssets };
await writeFile(join(dist, 'out', nlsManifestPath.slice('/out/'.length)), JSON.stringify(nlsManifest));
immutableAssets.add(nlsManifestPath.slice(1));
console.log(`[nls] ${nlsCollector.size} source messages, ${missing.length} upstream translation gaps; see out/nls.zh-cn.missing.json`);
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
await cp(join(root, 'resources/localization/LICENSE.txt'), join(dist, 'LOCALIZATION-LICENSE.txt'));
await cp(join(root, 'resources/localization/README.md'), join(dist, 'LOCALIZATION-SOURCE.md'));
const upstreamLocalization = await readFile(join(root, 'resources/localization/zh-cn-upstream-main.json'));
await writeFile(join(dist, 'version.json'), JSON.stringify({
  commit,
  version: upstreamPackage.version,
  extensions: extensions.length,
  localization: {
    source: 'microsoft/vscode-loc',
    sourceCommit: '62d5443b567022539ba6e8565b0f43f5f6b3682b',
    mainSha256: createHash('sha256').update(upstreamLocalization).digest('hex'),
    productCatalogSha256,
    nlsManifest: nlsManifestPath,
    extensions: extensionLocalizationHashes,
    nlsAssets: languageAssets,
  },
}));
await writeFile(join(dist, 'immutable-assets.json'), JSON.stringify([...immutableAssets].sort()));
console.log(`${values['assets-only'] ? 'Workbench 开发资源' : '完整 Workbench'}构建完成：${commit}，${extensions.length} 个内置 Web 扩展`);
