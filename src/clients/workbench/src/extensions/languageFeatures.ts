import { isEqualOrParent, toSlashes } from '../../upstream/src/vs/base/common/extpath.js';
import { match, type IRelativePattern } from '../../upstream/src/vs/base/common/glob.js';
import { posix, win32 } from '../../upstream/src/vs/base/common/path.js';
import { originalFSPath } from '../../upstream/src/vs/base/common/resources.js';
import { equalsIgnoreCase } from '../../upstream/src/vs/base/common/strings.js';
import { URI, type UriComponents } from '../../upstream/src/vs/base/common/uri.js';
import { type LanguageFilter, type LanguageSelector, score } from '../../upstream/src/vs/editor/common/languageSelector.js';
import type { IExtensionHost } from '../../upstream/src/vs/workbench/services/extensions/common/extensions.js';
import { TargetUriTransformer, hostOwnsWorkspaceResource } from './targetUris.js';
import { targetOperatingSystem } from './targetContext.js';

/** 原注册表先按宿主候选评分；原 selector、glob 和独占排序仍由上游持有。 */
export function hostLanguageFeatureScore(host: IExtensionHost, selector: LanguageSelector): typeof score | undefined {
  if (!host.targetContext) return undefined;
  if (host.targetContext.kind === 'web') return (_selector, resource, language, synchronized, notebookResource, notebookType) => {
    if (!hostOwnsWorkspaceResource(host, resource) || notebookResource && !hostOwnsWorkspaceResource(host, notebookResource)) return 0;
    return score(selector, resource, language, synchronized, notebookResource, notebookType);
  };
  const transformer = host.uriTransformer;
  if (!(transformer instanceof TargetUriTransformer)) throw new Error('语言 Provider 缺少已绑定 URI transformer');
  const windows = targetOperatingSystem(host.targetContext) === 'windows';
  const normalize = (path: string) => windows ? toSlashes(win32.normalize(path)) : posix.normalize(path);
  const nativePath = (uri: URI) => normalize(windows ? originalFSPath(uri) : uri.path);
  const project = (filter: string | LanguageFilter): string | LanguageFilter => {
    if (typeof filter === 'string' || !filter.pattern) return filter;
    if (typeof filter.pattern === 'string') return { ...filter, pattern: windows ? toSlashes(filter.pattern) : filter.pattern };
    // 原 RelativePattern 同时携带 typed baseUri 和原生 base；只为本地评分解析位置。
    const pattern = filter.pattern as IRelativePattern & { baseUri?: UriComponents };
    const base = pattern.baseUri ? nativePath(transformer.transformOutgoingURI(URI.revive(pattern.baseUri))) : normalize(pattern.base);
    return { ...filter, pattern: { base, pattern: windows ? toSlashes(pattern.pattern) : pattern.pattern } };
  };
  const nativeSelector = typeof selector === 'string' ? selector : Array.isArray(selector)
    ? selector.map(project) : project(selector as LanguageFilter);
  const matchPattern = (pattern: string | IRelativePattern, uri: URI): boolean => {
    const path = nativePath(uri), options = { ignoreCase: windows };
    if (typeof pattern === 'string') return (windows ? equalsIgnoreCase(pattern, path) : pattern === path) || match(pattern, path, options);
    if (!isEqualOrParent(path, pattern.base, windows, true)) return false;
    const relative = path.slice(pattern.base.length).replace(/^\/+/, '');
    return match(pattern.pattern, relative, options);
  };
  return (_selector, resource, language, synchronized, notebookResource, notebookType) => {
    // TODO: 6.2 的 Notebook cell 模型同步与归属仍需原 Notebook owner 迁齐，不能仅凭关联文件放行。
    if (!transformer.ownsWorkspaceResource(resource) || notebookResource && !transformer.ownsWorkspaceResource(notebookResource)) return 0;
    return score(nativeSelector, transformer.transformOutgoingURI(resource), language, synchronized,
      notebookResource ? transformer.transformOutgoingURI(notebookResource) : undefined, notebookType, matchPattern);
  };
}
