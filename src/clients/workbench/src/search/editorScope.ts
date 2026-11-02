import type { ITextModel } from '../../upstream/src/vs/editor/common/model.js';
import type { ICommonQueryProps } from '../../upstream/src/vs/workbench/services/search/common/search.js';
import { URI } from '../../upstream/src/vs/base/common/uri.js';
import { businessQuery } from './query.js';
import { scopeFromQuery, type SearchScope } from './scope.js';

export type SearchEditorScope = (SearchScope & { kind: 'workspace' }) | { version: 1; kind: 'native' };

/** 仅校验这个持久字段的职责；缺省是旧历史，不能猜当前工作区。 */
export function parseSearchEditorScope(value: unknown): SearchEditorScope | undefined {
  if (value === undefined) return undefined;
  if (!value || typeof value !== 'object' || !('version' in value) || value.version !== 1 || !('kind' in value)) {
    throw new Error('搜索编辑器范围损坏或版本不受支持');
  }
  if (value.kind === 'native') return { version: 1, kind: 'native' };
  if (value.kind !== 'workspace' || !('workspaces' in value) || !Array.isArray(value.workspaces) || !value.workspaces.length) {
    throw new Error('搜索编辑器缺少明确工作区范围');
  }
  const workspaces = value.workspaces.map((target: unknown) => {
    if (!target || typeof target !== 'object' || !('gatewayId' in target) || typeof target.gatewayId !== 'string' || !target.gatewayId
      || !('workspaceId' in target) || typeof target.workspaceId !== 'string' || !target.workspaceId
      || !('rootUri' in target) || typeof target.rootUri !== 'string' || !target.rootUri
      || !('label' in target) || typeof target.label !== 'string' || !target.label) {
      throw new Error('搜索编辑器工作区范围字段损坏');
    }
    return { gatewayId: target.gatewayId, workspaceId: target.workspaceId, rootUri: target.rootUri, label: target.label };
  });
  return { version: 1, kind: 'workspace', workspaces };
}

/** 展示名称不是范围身份；不使用内容 hash 或额外缓存。 */
export function searchEditorScopeKey(scope: SearchEditorScope | undefined): string {
  return JSON.stringify(scope?.kind === 'workspace'
    ? scope.workspaces.map(target => [target.gatewayId, target.workspaceId, target.rootUri]).sort()
    : scope?.kind ?? null);
}

export function searchEditorScopeFromQuery(query: ICommonQueryProps<URI>): SearchEditorScope {
  return businessQuery(query) ? { ...scopeFromQuery(query), kind: 'workspace' } : { version: 1, kind: 'native' };
}

/** 同一 header 读取用于备份与磁盘；增加字段不能截掉第六行之后的选项。 */
export function searchEditorHeaderLines(model: ITextModel): string[] {
  const lines: string[] = [];
  for (let line = 1; line <= model.getLineCount(); line++) {
    const text = model.getLineContent(line);
    if (!text) break;
    lines.push(text);
  }
  return lines;
}
