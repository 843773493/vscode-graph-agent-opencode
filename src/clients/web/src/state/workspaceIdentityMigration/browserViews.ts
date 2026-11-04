import { browserViewKey } from "../browserViewStore";
import { gatewayWorkspaceKey } from "../../api/workspaceIdentity";
import { record, recoveredWorkspaceKey, recoverWorkspaceOwner, text, workspaceUuid, type WorkspaceIdentityRecovery } from "./material";

export interface RecoveryIssue { key: string; message: string }
export interface ViewChange { before: string; after: string; value: unknown }

function migrateSettings(value: unknown, material: WorkspaceIdentityRecovery): unknown {
  const settings = record(value, "浏览器设置");
  const sidebar = record(settings.session_sidebar, "会话侧边栏设置");
  const tree = record(settings.workspace_file_tree, "工作区文件树设置");
  const list = (input: unknown, convert: (value: string) => string): string[] => {
    if (!Array.isArray(input)) throw new Error("工作区展示引用必须为数组");
    return [...new Set(input.map(value => convert(text(value, "展示引用"))))];
  };
  const expanded = record(tree.expanded_paths_by_workspace ?? {}, "文件树展开记录");
  const paths: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(expanded)) {
    const next = recoveredWorkspaceKey(material, key);
    if (next in paths && JSON.stringify(paths[next]) !== JSON.stringify(value)) throw new Error(`文件树恢复引用冲突：${key}`);
    paths[next] = value;
  }
  return { ...settings, session_sidebar: { ...sidebar,
    collapsed_workspace_ids: list(sidebar.collapsed_workspace_ids ?? [], value => recoveredWorkspaceKey(material, value)),
    expanded_root_tree_ids: list(sidebar.expanded_root_tree_ids ?? [], value => value.startsWith("workspace:")
      ? `workspace:${recoveredWorkspaceKey(material, value.slice("workspace:".length))}` : value),
  }, workspace_file_tree: { ...tree, expanded_paths_by_workspace: paths } };
}

function migrateView(key: string, value: unknown, material: WorkspaceIdentityRecovery): ViewChange | null {
  const draftPrefix = `draft:${material.registry_gateway_id}::`;
  if (key.startsWith(draftPrefix)) {
    const suffix = key.slice(draftPrefix.length), end = suffix.indexOf("::");
    if (end < 1) throw new Error("草稿缺少原工作区和 Session 引用");
    const workspace = recoveredWorkspaceKey(material, decodeURIComponent(suffix.slice(0, end)));
    return { before: key, after: draftPrefix + encodeURIComponent(workspace) + suffix.slice(end), value };
  }
  if (!key.startsWith("[")) return null;
  const parts: unknown = JSON.parse(key);
  if (!Array.isArray(parts) || parts[0] !== material.registry_gateway_id) return null;
  const kind = parts[1];
  if (kind === "settings") return { before: key, after: key, value: migrateSettings(value, material) };
  if (kind === "selection" || kind === "session") {
    const view = record(value, "会话视图");
    const reference = text(view.workspace_id, "会话工作区");
    const owner = view.gateway_id === undefined ? recoverWorkspaceOwner(material, reference)
      : { gatewayId: text(view.gateway_id, "会话 Gateway"), workspaceId: workspaceUuid(reference) };
    if (kind === "session") {
      if (parts.length !== 5 || text(parts[3], "Session") !== view.session_id) throw new Error("会话视图键与 Session 不一致");
      const keyOwner = recoverWorkspaceOwner(material, text(parts[2], "会话视图工作区"));
      if (gatewayWorkspaceKey(keyOwner) !== gatewayWorkspaceKey(owner)) throw new Error("会话视图键与原 owner 不一致");
    }
    text(view.session_id, "Session");
    return { before: key, after: kind === "selection" ? key
      : browserViewKey(material.registry_gateway_id, "session", gatewayWorkspaceKey(owner), parts[3] as string, text(parts[4], "Thread")),
    value: { ...view, gateway_id: owner.gatewayId, workspace_id: owner.workspaceId } };
  }
  if (kind === "thread-history") {
    const snapshot = record(value, "历史快照"), owner = record(snapshot.owner, "历史 owner");
    if (snapshot.version !== 1 || parts.length !== 5 || owner.gatewayId !== parts[0] || owner.workspaceId !== parts[2]
      || owner.sessionId !== parts[3] || owner.threadId !== parts[4]) throw new Error("历史快照与原 owner 不一致");
    // 此格式原本使用真实 owner；不按登记 Gateway 或当前活动项重新绑定历史正文。
    workspaceUuid(owner.workspaceId);
  }
  return null;
}

/** 每个展示记录单独诊断；可确认项与原键删除在同一个事务内提交。 */
export function planBrowserViewRecovery(entries: ReadonlyMap<string, unknown>, material: WorkspaceIdentityRecovery): { changes: ViewChange[]; issues: RecoveryIssue[] } {
  const changes: ViewChange[] = [], issues: RecoveryIssue[] = [];
  const targets = new Map(entries);
  for (const [key, value] of entries) {
    try {
      const change = migrateView(key, value, material);
      if (!change || change.before === change.after && JSON.stringify(value) === JSON.stringify(change.value)) continue;
      if (change.before !== change.after && targets.has(change.after)
        && JSON.stringify(targets.get(change.after)) !== JSON.stringify(change.value)) throw new Error(`恢复目标已有不同记录：${change.after}`);
      changes.push(change);
      targets.set(change.after, change.value);
    } catch (error) {
      issues.push({ key, message: error instanceof Error ? error.message : String(error) });
    }
  }
  return { changes, issues };
}
