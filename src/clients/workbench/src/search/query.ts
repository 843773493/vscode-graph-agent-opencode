import * as glob from '../../upstream/src/vs/base/common/glob.js';
import type { IExtUri } from '../../upstream/src/vs/base/common/resources.js';
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

export function fileOptions(query: ICommonQueryProps<URI>, folder: IFolderQuery, identity: IExtUri): ResourceNavigationSearchOptions {
  // TODO：其余显式选项仍需迁齐目标 owner；未支持时不能悄悄弱化查询。
  if (query.ignoreGlobCase || folder.ignoreGlobCase) throw new Error('业务文件搜索尚未支持忽略 glob 大小写');
  if (folder.fileEncoding && !/^utf-?8$/i.test(folder.fileEncoding)) throw new Error('业务文件搜索只支持 UTF-8');
  if (folder.ignoreSymlinks) throw new Error('业务文件搜索尚未支持忽略符号链接');
  if (folder.disregardGlobalIgnoreFiles === false || folder.disregardParentIgnoreFiles === false) {
    throw new Error('业务文件搜索尚未支持全局或父目录 ignore 文件');
  }
  const globalIncludes = patterns(query.includePattern), folderIncludes = patterns(folder.includePattern);
  if (globalIncludes.length && folderIncludes.length) throw new Error('业务文件搜索尚未支持两层 include 模式的交集');
  const excludes = (folder.excludePattern ?? []).flatMap(exclude => {
    if (exclude.folder && !identity.isEqual(exclude.folder, folder.folder)) throw new Error('业务文件搜索尚未支持子目录限定的 exclude 模式');
    return patterns(exclude.pattern);
  });
  return { case_mode: 'sensitive', is_regex: false, whole_word: false, is_multiline: false, all_matches: false, context_lines: 0,
    include_globs: [...new Set([...globalIncludes, ...folderIncludes])].sort(), exclude_globs: [...new Set([...patterns(query.excludePattern), ...excludes])].sort(),
    ignore_mode: folder.disregardIgnoreFiles === undefined ? 'default' : folder.disregardIgnoreFiles ? 'none' : 'respect',
    excluded_resource_uris: [] };
}

/** 准入前消除重复扫描；根相对过滤或 ignore 继承不同的重叠根必须明确拆开查询。 */
function uniqueFolders(query: ICommonQueryProps<URI>, identity: IExtUri): IFolderQuery[] {
  const folders: IFolderQuery[] = [];
  for (const folder of query.folderQueries) {
    const options = fileOptions(query, folder, identity);
    let insertion = folders.length, duplicate = false;
    for (let index = 0; index < folders.length; index++) {
      const previous = folders[index];
      const child = identity.isEqualOrParent(folder.folder, previous.folder);
      const parent = identity.isEqualOrParent(previous.folder, folder.folder);
      if (!child && !parent) continue;
      const previousOptions = fileOptions(query, previous, identity);
      if (JSON.stringify(options) !== JSON.stringify(previousOptions)) throw new Error('重叠搜索目录具有冲突的局部选项，请分别查询');
      if (!identity.isEqual(folder.folder, previous.folder)) {
        const relative = identity.relativePath(parent ? folder.folder : previous.folder, parent ? previous.folder : folder.folder)!;
        if (options.include_globs.length || options.exclude_globs.length || options.ignore_mode === 'respect'
          || (options.ignore_mode === 'default' && relative.split('/').some(part => ['.git', 'node_modules', '.boxteam'].includes(part)))) {
          // TODO：支持重叠根的相对过滤和 ignore 继承时，须由同一文件 owner 分区扫描。
          throw new Error('重叠搜索目录的相对过滤或 ignore 边界不同，请分别查询');
        }
      }
      if (child) { duplicate = true; break; }
      insertion = Math.min(insertion, index);
    }
    if (duplicate) continue;
    // 后出现的父根取代子根，保留该范围最初的分支次序。
    const remaining = folders.filter(previous => !identity.isEqualOrParent(previous.folder, folder.folder));
    remaining.splice(insertion, 0, folder);
    folders.splice(0, folders.length, ...remaining);
  }
  return folders;
}

export function validateQuery<T extends ICommonQueryProps<URI>>(query: T, identity: IExtUri): T {
  if (!businessQuery(query)) throw new Error('业务文件 Provider 只接受明确的业务资源根');
  if (query.onlyFileScheme) throw new Error('业务搜索不能限定为原生 file scheme');
  if (query.maxResults !== undefined && (!Number.isSafeInteger(query.maxResults) || query.maxResults < 0)) {
    throw new Error('搜索结果上限必须是非负整数');
  }
  for (const folder of query.folderQueries) {
    workspaceFileSource(folder.folder);
    fileOptions(query, folder, identity);
  }
  for (const resource of query.extraFileResources ?? []) {
    workspaceFileSource(resource);
    if (!query.folderQueries.some(folder => identity.isEqualOrParent(resource, folder.folder))) {
      throw new Error('额外搜索文件不属于本次明确目录根');
    }
  }
  return { ...query, folderQueries: uniqueFolders(query, identity), extraFileResources: query.extraFileResources
    ? [...new Map(query.extraFileResources.map(resource => [identity.getComparisonKey(resource), resource])).values()] : undefined };
}

export function validateTextQuery(query: ITextQuery, identity: IExtUri): ITextQuery {
  query = validateQuery(query, identity);
  if (query.surroundingContext !== undefined && (!Number.isSafeInteger(query.surroundingContext) || query.surroundingContext < 0 || query.surroundingContext > 100)) {
    throw new Error('搜索前后文行数必须是 0～100 的整数');
  }
  if (!query.contentPattern.pattern) throw new Error('文件内容搜索不能为空');
  if (query.maxFileSize !== undefined && (!Number.isSafeInteger(query.maxFileSize) || query.maxFileSize < 1 || query.maxFileSize > 33554432)) {
    throw new Error('业务文件搜索的文件读取上限必须在 1～33554432 字节内');
  }
  return query;
}

export function resourceInQuery(resource: URI, query: ICommonQueryProps<URI>, identity: IExtUri): boolean {
  if (resource.scheme !== fileScheme) return false;
  return query.folderQueries.some(folder => {
    if (!identity.isEqualOrParent(resource, folder.folder)) return false;
    const relative = identity.relativePath(folder.folder, resource)!;
    const options = fileOptions(query, folder, identity);
    if ('contentPattern' in query && options.ignore_mode === 'default' && relative.split('/').some(part => ['.git', 'node_modules', '.boxteam'].includes(part))) return false;
    // 与原文件 owner 的 glob 根语义一致，也排除已被目录模式剪枝的子项。
    const match = (pattern: string, path: string) => glob.match('**/' + pattern.replace(/^\/+/, ''), path);
    const ancestors = relative.split('/').map((_, index, parts) => parts.slice(0, index + 1).join('/'));
    return (!options.include_globs.length || options.include_globs.some(pattern => match(pattern, relative)))
      && !options.exclude_globs.some(pattern => ancestors.some(path => match(pattern, path) || match(pattern, path + '/')));
  });
}

export function openResources(editors: IEditorService, query: ICommonQueryProps<URI>, identity: IExtUri): URI[] {
  const resources = new Map<string, URI>();
  for (const editor of editors.editors) {
    const resource = EditorResourceAccessor.getCanonicalUri(editor, { supportSideBySide: SideBySideEditor.PRIMARY });
    if (resource && resourceInQuery(resource, query, identity)) resources.set(identity.getComparisonKey(resource), resource);
  }
  return [...resources.values()];
}
