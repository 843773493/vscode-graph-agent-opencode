import { useState } from "react";
import type { GatewayRuntimeGroupRequest, GatewayWorkspace } from "../../../types/backend";
import { errorMessage } from "../../../utils/errorMessage";

export interface GatewayBackendRuntimeActions {
  onStart: (id: string) => Promise<void>;
  onStop: (id: string) => Promise<void>;
  onSafeRestart: (id: string, request?: GatewayRuntimeGroupRequest) => Promise<void>;
  onForceRestart: (id: string, request?: GatewayRuntimeGroupRequest) => Promise<void>;
}

export default function GatewayBackendRuntimePanel({
  workspaces, onStart, onStop, onSafeRestart, onForceRestart,
}: GatewayBackendRuntimeActions & { workspaces: GatewayWorkspace[] }) {
  const [selection, setSelection] = useState<{ generation: string; workspaceId: string; ids: string[] } | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const managed = workspaces.filter((workspace) => workspace.managed);
  const groups = new Map<string, GatewayWorkspace[]>();
  for (const workspace of managed) {
    const key = `${workspace.remote?.gateway_connection_id ?? "local"}:${workspace.backend_generation ?? workspace.workspace_id}`;
    const group = groups.get(key) ?? [];
    group.push(workspace);
    groups.set(key, group);
  }
  const run = async (action: () => Promise<void>) => {
    setBusy(true);
    setError(null);
    try { await action(); setSelection(null); }
    catch (failure) { setError(errorMessage(failure)); }
    finally { setBusy(false); }
  };
  return <section aria-label="Backend 运行时">
    {error ? <div className="gateway-console-alert" role="alert">{error}</div> : null}
    {[...groups.entries()].map(([key, group]) => {
      const target = group.find((item) => item.system_default) ?? group[0];
      const memberIds = target.backend_generation ? target.affected_workspace_ids : [target.workspace_id];
      const members = memberIds.map((id) => workspaces.find((item) => item.workspace_id === id));
      const selected = selection?.generation === target.backend_generation && selection?.workspaceId === target.workspace_id;
      const desired = selected && selection ? selection.ids : memberIds;
      const ready = group.every((item) => item.status === "ready");
      const protectedGroup = members.some((item) => item?.system_default || item?.removable === false);
      const candidates = managed.filter((item) =>
        memberIds.includes(item.workspace_id)
        || (item.status === "offline" && item.affected_workspace_ids.length <= 1
          && item.remote?.gateway_connection_id === target.remote?.gateway_connection_id),
      );
      const request = target.backend_generation ? {
        expected_backend_generation: target.backend_generation,
        expected_workspace_ids: memberIds,
        desired_workspace_ids: desired,
      } : undefined;
      return <article className="gateway-connection-section" key={key}>
        <h2>{members.map((item, index) => item?.name ?? memberIds[index]).join("、")}</h2>
        <p>{ready ? "运行中" : "离线或异常"} · {memberIds.length} 个工作区共用一个 Backend</p>
        <div className="gateway-control-actions">
          <button type="button" disabled={busy || ready} onClick={() => void run(() => onStart(target.workspace_id))}>启动完整组</button>
          <button type="button" disabled={busy || !ready || protectedGroup} onClick={() => void run(() => onStop(target.workspace_id))}>关闭完整组</button>
          <button type="button" disabled={busy || !target.backend_generation} onClick={() => void run(() => onSafeRestart(target.workspace_id))}>安全重启</button>
          <button type="button" className="danger-soft" disabled={busy || !target.backend_generation} onClick={() => void run(() => onForceRestart(target.workspace_id))}>强制重启</button>
          <button type="button" disabled={busy || !target.backend_generation} onClick={() => setSelection(
            selected ? null : { generation: target.backend_generation!, workspaceId: target.workspace_id, ids: memberIds },
          )}>调整成员</button>
        </div>
        {selected ? <fieldset disabled={busy}>
          <legend>重启后的完整成员</legend>
          {candidates.map((item) => <label key={item.workspace_id} style={{ display: "block" }}>
            <input type="checkbox" checked={desired.includes(item.workspace_id)}
              disabled={item.system_default || !item.removable}
              onChange={(event) => setSelection({
                generation: target.backend_generation!, workspaceId: target.workspace_id,
                ids: event.target.checked ? [...desired, item.workspace_id] : desired.filter((id) => id !== item.workspace_id),
              })} /> {item.name}{item.system_default ? "（默认工作区）" : ""}
          </label>)}
          <p>加入前请先关闭另一个 singleton；移出全部成员将关闭 Backend，其余变更会一起受控重启。</p>
          <button type="button" onClick={() => void run(() => onSafeRestart(target.workspace_id, request))}>{desired.length ? "确认成员并安全重启" : "排空并退出全部成员"}</button>
          <button type="button" className="danger-soft" onClick={() => void run(() => onForceRestart(target.workspace_id, request))}>{desired.length ? "确认成员并强制重启" : "中断任务并退出全部成员"}</button>
        </fieldset> : null}
      </article>;
    })}
    {managed.length === 0 ? <p>当前 Gateway 没有托管工作区。</p> : null}
    {busy ? <p role="status">正在操作完整组，请等待所有工作区就绪…</p> : null}
  </section>;
}
