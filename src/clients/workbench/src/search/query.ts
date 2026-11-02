import * as glob from '../../upstream/src/vs/base/common/glob.js';
import { isEqual, isEqualOrParent, relativePath } from '../../upstream/src/vs/base/common/resources.js';
import { URI } from '../../upstream/src/vs/base/common/uri.js';
import { EditorResourceAccessor, SideBySideEditor } from '../../upstream/src/vs/workbench/common/editor.js';
import type { IEditorService } from '../../upstream/src/vs/workbench/services/editor/common/editorService.js';
import type { ICommonQueryProps, IFolderQuery, ITextQuery } from '../../upstream/src/vs/workbench/services/search/common/search.js';
import type { ResourceNavigationSearchOptions } from '../../../web/src/types/protocol_generated/boxteam/workspace/v2/public.js';
import { workspaceFileSource } from '../workspaceFiles.js';

export const fileScheme = 'boxteam-file';

export function businessQuery(query: ICommonQueryProps<URI>): boolean {
  const resources = [...query.folderQueries.map(folder => folder.folder), ...(query.extraFileResources ?? [])];
  if (!resources.some(resource => resource.scheme === fileScheme)) return false;
  if (resources.some(resource => resource.scheme !== fileScheme)) throw new Error('业务搜索不能混入原生容器或其它资源根');
  if (!query.folderQueries.length) throw new Error('业务搜索必须提供明确目录根');
  return true;
}

export function patterns(expression?: glob.IExpression): string[] {
  return Object.entries(expression ?? {}).flatMap(([pattern, enabled]) => {
    if (typeof enabled !== 'boolean') throw new Error('业务文件搜索尚未支持 sibling 条件模式');
    return enabled ? [pattern] : [];
  });
}

export function fileOptions(query: ICommonQueryProps<URI>, folder: IFolderQuery): ResourceNavigationSearchOptions {
  // TODO：原文件 owner 补齐目标 ignore 文件及其余显式选项前，先拒绝，不能悄悄弱化查询。
  if (query.ignoreGlobCase || folder.ignoreGlobCase) throw new Error('业务文件搜索尚未支持忽略 glob 大小写');
  if (folder.fileEncoding && !/^utf-?8$/i.test(folder.fileEncoding)) throw new Error('业务文件搜索只支持 UTF-8');
  if (folder.ignoreSymlinks) throw new Error('业务文件搜索尚未支持忽略符号链接');
  if (folder.disregardGlobalIgnoreFiles === false || folder.disregardParentIgnoreFiles === false) {
    throw new Error('业务文件搜索尚未支持全局或父目录 ignore 文件');
  }
  const globalIncludes = patterns(query.includePattern), folderIncludes = patterns(folder.includePattern);
  if (globalIncludes.length && folderIncludes.length) throw new Error('业务文件搜索尚未支持两层 include 模式的交集');
  const excludes = (folder.excludePattern ?? []).flatMap(exclude => {
    if (exclude.folder && !isEqual(exclude.folder, folder.folder)) throw new Error('业务文件搜索尚未支持子目录限定的 exclude 模式');
    return patterns(exclude.pattern);
  });
  return { case_mode: 'sensitive', is_regex: false, whole_word: false, is_multiline: false, all_matches: false,
    include_globs: [...globalIncludes, ...folderIncludes], exclude_globs: [...patterns(query.excludePattern), ...excludes],
    ignore_mode: folder.disregardIgnoreFiles === undefined ? 'default' : folder.disregardIgnoreFiles ? 'none' : 'respect' };
}

export function validateQuery(query: ICommonQueryProps<URI>): void {
  if (!businessQuery(query)) throw new Error('业务文件 Provider 只接受明确的业务资源根');
  if (query.onlyFileScheme) throw new Error('业务搜索不能限定为原生 file scheme');
  if (query.maxResults !== undefined && (!Number.isSafeInteger(query.maxResults) || query.maxResults < 0)) {
    throw new Error('搜索结果上限必须是非负整数');
  }
  for (const folder of query.folderQueries) {
    workspaceFileSource(folder.folder);
    const options = fileOptions(query, folder);
    if (options.ignore_mode === 'respect') throw new Error('尚未支持读取目标目录的 ignore 文件，请关闭该选项后搜索');
  }
  for (const resource of query.extraFileResources ?? []) {
    workspaceFileSource(resource);
    if (!query.folderQueries.some(folder => isEqualOrParent(resource, folder.folder))) {
      throw new Error('额外搜索文件不属于本次明确目录根');
    }
  }
}

export function validateTextQuery(query: ITextQuery): void {
  validateQuery(query);
  if (query.surroundingContext) throw new Error('业务文件搜索尚未支持前后文行');
  if (!query.contentPattern.pattern) throw new Error('文件内容搜索不能为空');
  if (query.maxFileSize !== undefined && (!Number.isSafeInteger(query.maxFileSize) || query.maxFileSize < 1 || query.maxFileSize > 33554432)) {
    throw new Error('业务文件搜索的文件读取上限必须在 1～33554432 字节内');
  }
}

export function resourceInQuery(resource: URI, query: ICommonQueryProps<URI>): boolean {
  if (resource.scheme !== fileScheme) return false;
  return query.folderQueries.some(folder => {
    if (!isEqualOrParent(resource, folder.folder)) return false;
    const relative = relativePath(folder.folder, resource)!;
    const options = fileOptions(query, folder);
    if ('contentPattern' in query && options.ignore_mode === 'default' && relative.split('/').some(part => ['.git', 'node_modules', '.boxteam'].includes(part))) return false;
    // 与原文件 owner 的 glob 根语义一致，也排除已被目录模式剪枝的子项。
    const match = (pattern: string, path: string) => glob.match('**/' + pattern.replace(/^\/+/, ''), path);
    const ancestors = relative.split('/').map((_, index, parts) => parts.slice(0, index + 1).join('/'));
    return (!options.include_globs.length || options.include_globs.some(pattern => match(pattern, relative)))
      && !options.exclude_globs.some(pattern => ancestors.some(path => match(pattern, path) || match(pattern, path + '/')));
  });
}

export function openResources(editors: IEditorService, query: ICommonQueryProps<URI>): URI[] {
  const resources = new Map<string, URI>();
  for (const editor of editors.editors) {
    const resource = EditorResourceAccessor.getCanonicalUri(editor, { supportSideBySide: SideBySideEditor.PRIMARY });
    if (resource && resourceInQuery(resource, query)) resources.set(resource.toString(), resource);
  }
  return [...resources.values()];
}
