import React, { useEffect, useId, useState } from "react";
import { createPortal } from "react-dom";
import type { SessionForkCreationKind, SessionForkMode, SessionForkRequest, SessionForkResourceChoice } from "../../types/backend";
import type { SessionForkDialogSelection, SessionForkDialogState } from "../../hooks/shell/useSessionForkDialog";

export default function SessionContextForkDialog({
  state, onCancel, onSubmit, onSelectionChange,
}: {
  state: SessionForkDialogState | null;
  onCancel: () => void;
  onSubmit: (request: SessionForkRequest) => Promise<void>;
  onSelectionChange: (change: Partial<SessionForkDialogSelection>) => Promise<void>;
}): React.ReactNode {
  const labelId = useId();
  const [creationKind, setCreationKind] = useState<SessionForkCreationKind>("new_child_thread");
  const [title, setTitle] = useState("");
  const [resourceChoices, setResourceChoices] = useState<Record<string, "copy_snapshot" | "omit">>({});
  const [liveChoices, setLiveChoices] = useState<Set<string>>(() => new Set());
  const [includeGoal, setIncludeGoal] = useState(false);
  const [tokenBudget, setTokenBudget] = useState("");
  useEffect(() => {
    setCreationKind("new_child_thread");
    setResourceChoices({});
    setLiveChoices(new Set());
    setIncludeGoal(false);
    setTokenBudget("");
  }, [state?.idempotencyKey]);
  useEffect(() => {
    setTitle(state?.sourcePoint?.title ?? "");
  }, [state?.idempotencyKey, state?.sourcePoint?.source_uri]);
  useEffect(() => {
    if (!state?.sourcePoint || state.loading) return;
    const resources = state.sourcePoint.resources;
    setLiveChoices((prior) => new Set(state.sourcePoint!.live_resources
      .filter((resource) => prior.has(`${resource.kind}:${resource.resource_id}`)
        && resource.retention_scope === "workspace" && resource.reconciliation_status === "verified"
        && resource.available_actions.includes("attach"))
      .map((resource) => `${resource.kind}:${resource.resource_id}`)));
    setResourceChoices((prior) => {
      const current: Record<string, "copy_snapshot" | "omit"> = {};
      for (const resource of resources) {
        const choice = prior[resource.resource_uri];
        if ((choice === "omit" && !resource.required_for_context)
          || (choice === "copy_snapshot" && resource.availability === "available")) {
          current[resource.resource_uri] = choice;
        }
      }
      return current;
    });
  }, [state?.sourcePoint, state?.loading]);
  useEffect(() => {
    if (!state) return;
    const close = (event: KeyboardEvent) => { if (event.key === "Escape" && !state.submitting) onCancel(); };
    window.addEventListener("keydown", close);
    return () => window.removeEventListener("keydown", close);
  }, [onCancel, state]);
  if (!state) return null;
  const sourcePoint = state.sourcePoint;
  const { mode, anchorMode, targetContainerUri } = state;
  const locked = state.submitting || state.frozenRequest !== null;
  const resourcesReady = !state.loading && !state.error && sourcePoint?.resource_mode === mode
    && sourcePoint.resource_anchor_mode === anchorMode
    && (mode !== "view_prefix_fork" || Boolean(sourcePoint.anchor_item_id));
  const submit = (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!sourcePoint || state.submitting || (!state.frozenRequest && !resourcesReady)) return;
    void onSubmit(state.frozenRequest ?? {
      idempotency_key: state.idempotencyKey,
      source_uri: sourcePoint.source_uri,
      source_view_id: sourcePoint.source_view_id,
      source_view_revision: sourcePoint.source_view_revision,
      anchor_item_id: mode === "full_rollout_copy" ? null : mode === "context_fork" ? null : sourcePoint.anchor_item_id,
      mode,
      anchor_mode: mode === "view_prefix_fork" ? anchorMode : "inclusive",
      target_container_uri: targetContainerUri.trim(),
      creation_kind: creationKind,
      title: title.trim() || null,
      relationship: "detached",
      resources: [
        ...sourcePoint.resources.flatMap((resource) => resourceChoices[resource.resource_uri]
          ? [{ resource_uri: resource.resource_uri, action: resourceChoices[resource.resource_uri] }] : []),
        ...sourcePoint.live_resources.flatMap((resource): SessionForkResourceChoice[] =>
          liveChoices.has(`${resource.kind}:${resource.resource_id}`) && (resource.kind === "terminal" || resource.kind === "browser")
            ? [{ owner_reference: { kind: resource.kind, resource_id: resource.resource_id,
              workspace_id: resource.workspace_id, expected_revision: resource.owner_revision }, action: "reuse_reference" }]
            : []),
      ],
      goal: creationKind === "new_session_main" && includeGoal
        ? { token_budget: tokenBudget ? Number(tokenBudget) : undefined } : null,
    });
  };
  return createPortal(
    <div className="session-name-overlay" role="dialog" aria-modal="true" aria-labelledby={labelId}>
      <form className="session-name-dialog session-context-fork-dialog" onSubmit={submit}>
        <div className="session-name-header">
          <h2 id={labelId}>创建上下文分支</h2>
          <button type="button" className="session-name-close" aria-label="关闭" disabled={state.submitting} onClick={onCancel}>×</button>
        </div>
        <div className="session-context-fork-body">
        {state.loading ? <p role="status">{sourcePoint ? "正在读取所选范围的附件…" : "正在读取所选历史位置…"}</p> : null}
        {sourcePoint ? <>
          {creationKind === "new_session_main"
            ? <p>创建新会话并在主窗口中打开。</p>
            : <p>在所属会话中新建等待新消息的线程，并在主窗口右侧侧边栏打开。</p>}
          <p>来源会话：{state.sourceTitle ? `${state.sourceTitle} · ` : null}<code>{state.sourceSessionId}</code></p>
          <p style={{ overflowWrap: "anywhere" }}>来源线程：<code>{sourcePoint.source_uri}</code></p>
          <p>冻结历史位置：<code>{sourcePoint.source_view_id}</code>，版本 {sourcePoint.source_view_revision}</p>
          {mode === "view_prefix_fork" ? <p>截止消息：<code>{sourcePoint.anchor_item_id ?? "未选择消息"}</code></p> : null}
          {mode === "view_prefix_fork" && !sourcePoint.anchor_item_id ? <p role="status">当前来源没有可用的默认截止消息，请选择复制范围。</p> : null}
          <p>来源 Goal：{sourcePoint.goal_available ? "可选择继承到新会话" : "没有可继承的 Goal"}</p>
          <label className="session-name-field"><span>复制范围</span>
            <select aria-label="复制范围" value={mode} disabled={locked} onChange={(event) => {
              void onSelectionChange({ mode: event.target.value as SessionForkMode, anchorMode: "inclusive" });
            }}>
              <option value="view_prefix_fork">截至所选消息</option>
              <option value="context_fork">截至最近已完成的上下文</option>
              <option value="full_rollout_copy">完整历史</option>
            </select>
          </label>
          {mode === "view_prefix_fork" ? <label className="session-name-field"><span>截止位置</span>
            <select aria-label="截止位置" value={anchorMode} disabled={locked} onChange={(event) => {
              void onSelectionChange({ anchorMode: event.target.value as "inclusive" | "before" });
            }}>
              <option value="inclusive">包含所选消息</option><option value="before">所选消息之前</option>
            </select>
          </label> : null}
          <label className="session-name-field"><span>创建位置</span>
            <select aria-label="创建位置" value={creationKind} disabled={locked} onChange={(event) => {
              const kind = event.target.value as SessionForkCreationKind;
              setCreationKind(kind);
              void onSelectionChange({ targetContainerUri: kind === "new_child_thread" ? sourcePoint.child_container_uri : sourcePoint.main_container_uri });
              setIncludeGoal(false);
            }}>
              <option value="new_child_thread">同一会话的新线程</option><option value="new_session_main">新会话</option>
            </select>
          </label>
          <label className="session-name-field"><span>目标容器</span>
            <input value={targetContainerUri} disabled={locked} required onChange={(event) => {
              void onSelectionChange({ targetContainerUri: event.target.value });
            }} />
          </label>
          <label className="session-name-field"><span>目标名称</span>
            <input value={title} disabled={locked} onChange={(event) => setTitle(event.target.value)} autoFocus />
          </label>
          <fieldset disabled={locked || !resourcesReady}>
            <legend>附件继承</legend>
            <p>此处列出所选内容及其已登记依赖中的附件，创建时会再次核验完整依赖。</p>
            <p>共享存储域保留引用；跨存储域需明确选择继承快照。未继承的可选附件会显示不可用。</p>
            {sourcePoint.resources.length === 0 ? <p>所选范围没有已登记附件。</p> : sourcePoint.resources.map((resource) => (
              <label className="session-name-field" key={resource.resource_uri}>
                <span>{resource.name ?? resource.resource_uri} · {resource.required_for_context ? "上下文必需" : "历史可选"}
                  {resource.availability === "unavailable" ? " · 来源不可用" : " · 来源可用"}
                  {resource.content_type ? ` · ${resource.content_type}` : null}</span>
                <code style={{ overflowWrap: "anywhere" }}>{resource.resource_uri}</code>
                <select value={resourceChoices[resource.resource_uri] ?? "default"} onChange={(event) => {
                  const choice = event.target.value;
                  setResourceChoices((prior) => {
                    const next = { ...prior };
                    if (choice === "default") delete next[resource.resource_uri];
                    else next[resource.resource_uri] = choice as "copy_snapshot" | "omit";
                    return next;
                  });
                }}>
                  <option value="default">默认处理</option>
                  <option value="copy_snapshot" disabled={resource.availability === "unavailable"}>继承快照</option>
                  <option value="omit" disabled={resource.required_for_context}>不继承</option>
                </select>
              </label>
            ))}
          </fieldset>
          <fieldset disabled={locked || !resourcesReady}>
            <legend>运行资源关联</legend>
            <p>默认不关联终端或浏览器。勾选后由当前工作区的原资源关联新线程，继续使用同一实例。</p>
            {sourcePoint.live_resources.length === 0 ? <p>当前工作区没有运行资源。</p> : sourcePoint.live_resources.map((resource) => {
              const key = `${resource.kind}:${resource.resource_id}`;
              const eligible = resource.retention_scope === "workspace" && resource.reconciliation_status === "verified"
                && resource.available_actions.includes("attach");
              return <label className="session-name-field" key={key}>
                <span><input type="checkbox" aria-label={`关联${resource.kind === "terminal" ? "终端" : "浏览器"} ${resource.resource_id}`}
                  checked={liveChoices.has(key)} disabled={!eligible}
                  onChange={(event) => setLiveChoices((prior) => {
                    const next = new Set(prior);
                    if (event.target.checked) next.add(key); else next.delete(key);
                    return next;
                  })} />{resource.kind === "terminal" ? "终端" : "浏览器"} · {resource.resource_id}</span>
                <span>{resource.retention_scope === "workspace" ? "工作区保留" : "仅来源线程，不自动提升"} · {resource.status} · {resource.reconciliation_status}</span>
                <span>来源：{resource.source_thread.session_id} / {resource.source_thread.thread_id}</span>
                <span>当前关联：{resource.thread_associations.map((ref) => `${ref.session_id} / ${ref.thread_id}`).join("、") || "无"}</span>
                {resource.error ? <span role="status">{resource.error}</span> : null}
              </label>;
            })}
          </fieldset>
          {creationKind === "new_session_main" && sourcePoint.goal_available ? <>
            <label><input type="checkbox" checked={includeGoal} disabled={locked} onChange={(event) => setIncludeGoal(event.target.checked)} />继承目标（以暂停状态创建，累计 Token 和用时从 0 开始）</label>
            {includeGoal ? <label className="session-name-field"><span>新目标的 token 预算（可留空）</span>
              <input type="number" min="1" step="1" value={tokenBudget} disabled={locked} onChange={(event) => setTokenBudget(event.target.value)} />
            </label> : null}
          </> : null}
        </> : null}
        {state.error ? <div className="session-name-error" role="alert">{state.error}</div> : null}
        </div>
        <div className="session-name-actions">
          <button type="button" disabled={state.submitting} onClick={onCancel}>取消</button>
          <button type="submit" className="session-name-confirm" disabled={!sourcePoint || (!state.frozenRequest && !resourcesReady) || state.submitting || !targetContainerUri.trim()}>
            {state.submitting ? "创建中…" : state.frozenRequest ? "重试原请求" : "创建分支"}
          </button>
        </div>
      </form>
    </div>, document.body,
  );
}
