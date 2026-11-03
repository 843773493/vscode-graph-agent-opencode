import { prepareQuery, scoreItemFuzzy, compareItemsByFuzzyScore, type FuzzyScorerCache } from '../../upstream/src/vs/base/common/fuzzyScorer.js';
import { URI } from '../../upstream/src/vs/base/common/uri.js';
import { quickPickItemScorerAccessor } from '../../upstream/src/vs/platform/quickinput/common/quickInput.js';
import type { IAnythingQuickPickItem } from '../../upstream/src/vs/workbench/contrib/search/browser/anythingQuickAccess.js';
import type { WorkspaceOwner } from '../../../web/src/api/workspaceIdentity.js';
import { ThreadInput } from '../threadInput.js';
import { workspaceFileSource } from '../workspaceFiles.js';
import { changeFileSource } from '../changeFiles.js';
import { sourceDocumentSource } from '../sourceDocuments.js';
import type { IRange } from '../../upstream/src/vs/editor/common/core/range.js';

export interface ResourcePick extends IAnythingQuickPickItem { id: string; range?: { selection: IRange; decoration: IRange } }

/** 只反转已有内部投影，公开资源 URI 仍交目标 owner 解析。 */
export function editorResourceOwner(resource: URI): WorkspaceOwner | undefined {
  if (resource.scheme === 'boxteam-thread') return ThreadInput.fromResource(resource).owner;
  if (resource.scheme === 'boxteam-file') return workspaceFileSource(resource);
  if (resource.scheme === 'boxteam-change') return changeFileSource(resource).owner;
  if (resource.scheme === 'boxteam-source') return sourceDocumentSource(resource);
  return undefined;
}

export function sameSearchOwner(left: WorkspaceOwner | undefined, right: WorkspaceOwner): boolean {
  return left?.gatewayId === right.gatewayId && left.workspaceId === right.workspaceId;
}

/** 原 fuzzy scorer 同时提供排序及高亮；稳定身份负责同分排序与展示去重。 */
export function rankResourcePicks(picks: ResourcePick[], filter: string, cache: FuzzyScorerCache, recency = false): ResourcePick[] {
  if (!filter) return picks;
  const query = prepareQuery(filter);
  const matches = picks.filter(pick => {
    const { score, labelMatch, descriptionMatch } = scoreItemFuzzy(pick, query, true, quickPickItemScorerAccessor, cache);
    pick.highlights = { label: labelMatch, description: descriptionMatch };
    return score > 0;
  });
  return recency ? matches : matches.sort((left, right) => compareItemsByFuzzyScore(left, right, query, true,
    quickPickItemScorerAccessor, cache) || left.id.localeCompare(right.id));
}
