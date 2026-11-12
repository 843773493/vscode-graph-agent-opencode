import { analyzeLocalizeCalls, parseLocalizeKeyOrValue, TextModel } from '../upstream/build/lib/nls-analysis.ts';
import { relative, sep } from 'node:path';

export function transformNlsCalls(upstream, path, source, collector) {
  const sourceRoot = `${upstream}${sep}src${sep}`;
  const base = path.startsWith(sourceRoot) ? sourceRoot : `${upstream}${sep}`;
  const moduleId = relative(base, path).split(sep).join('/').replace(/\.tsx?$/, '');
  // 只跳过没有 NLS 模块的文件；实际 import 绑定、别名与调用均交给上游分析器。
  if (!/['"][^'"\r\n]*\/nls(?:\.js)?['"]/.test(source)) return source;
  const functions = ['localize', 'localize2'].filter(name => source.includes(name));
  if (!functions.length) return source;
  const model = new TextModel(source);
  const calls = functions.flatMap(name => analyzeLocalizeCalls(source, name))
    .sort((left, right) => left.keySpan.start.line - right.keySpan.start.line
    || left.keySpan.start.character - right.keySpan.start.character);
  const patches = [];
  for (const call of calls) {
    const parsedKey = parseLocalizeKeyOrValue(call.key);
    const key = typeof parsedKey === 'string' ? parsedKey : parsedKey.key;
    const parsedMessage = parseLocalizeKeyOrValue(call.value);
    if (typeof parsedMessage !== 'string') throw new Error(`NLS message must be a string: ${moduleId}`);
    const identity = `${moduleId}#${key}`;
    const existing = collector.get(identity);
    if (existing !== undefined && existing !== parsedMessage) throw new Error(`NLS key collision: ${identity}`);
    collector.set(identity, parsedMessage);
    patches.push({ span: call.keySpan, identity });
  }
  for (const patch of patches.reverse()) model.apply(patch.span, JSON.stringify(patch.identity));
  return model.toString();
}
