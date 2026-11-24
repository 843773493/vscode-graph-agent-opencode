import { build } from 'esbuild';
import { join } from 'node:path';

/** 发布与 Vite 共用同一内联入口，不依赖可能下载失败的主程序。 */
export async function injectStartupFailure(root, html) {
  const marker = '<!-- boxteam-startup-failure -->';
  if (html.split(marker).length !== 2) throw new Error('启动故障呈现入口与 index.html 不匹配');
  const result = await build({
    stdin: { contents: "import { installStartupFailureListeners } from './src/startupFailure.ts'; installStartupFailureListeners();",
      resolveDir: root, sourcefile: 'startup-failure-prelude.ts', loader: 'ts' },
    bundle: true, format: 'iife', platform: 'browser', target: 'es2024', minify: true, write: false,
    tsconfig: join(root, 'tsconfig.jsonc'), nodePaths: [join(root, 'node_modules')],
  });
  return html.replace(marker, () => `<script>${result.outputFiles[0].text.replace(/<\/script/gi, '<\\/script')}</script>`);
}
