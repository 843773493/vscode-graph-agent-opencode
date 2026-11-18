import { t } from '../../../../shared/core/i18n/index.js';
import { useContext } from "react";
import { OverlayRootContext } from "./OverlayRootContext";
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
  const overlayRoot = useContext(OverlayRootContext);
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
      anchor_item_id: mode === "full_rollout_copy" ? null : mode === "last_completed_turn_fork" ? null : sourcePoint.anchor_item_id,
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
      <form className="session-name-dialog session-context-fork-dialog" aria-busy={state.loading} onSubmit={submit}>
        <div className="session-name-header">
          <h2 id={labelId}>{t('navigation.fork.title')}</h2>
          <button type="button" className="session-name-close" aria-label={t('navigation.close')} disabled={state.submitting} onClick={onCancel}>×</button>
        </div>
        <div className="session-context-fork-body">

        {sourcePoint ? <>
          {creationKind === "new_session_main"
            ? <p>{t('navigation.fork.newSessionDescription')}</p>
            : <p>{t('navigation.fork.newThreadDescription')}</p>}
          <p>{t('navigation.fork.sourceSession')}{state.sourceTitle ? `${state.sourceTitle} · ` : null}<code>{state.sourceSessionId}</code></p>
          <p style={{ overflowWrap: "anywhere" }}>{t('navigation.fork.sourceThread')}<code>{sourcePoint.source_uri}</code></p>
          <p>{t('navigation.fork.historyPosition')}<code>{sourcePoint.source_view_id}</code>{t('navigation.fork.observedRevision')}{sourcePoint.source_view_revision}</p>
          {mode === "view_prefix_fork" ? <p>{t('navigation.fork.anchorMessage')}<code>{sourcePoint.anchor_item_id ?? t('navigation.fork.noAnchor')}</code></p> : null}
          {mode === "view_prefix_fork" && !sourcePoint.anchor_item_id ? <p role="status">{t('navigation.fork.noDefaultAnchor')}</p> : null}
          <p>{t('navigation.fork.sourceGoal')}{sourcePoint.goal_available ? t('navigation.fork.goalAvailable') : t('navigation.fork.goalUnavailable')}</p>
          <label className="session-name-field"><span>{t('navigation.fork.copyRange')}</span>
            <select aria-label={t('navigation.fork.copyRange')} value={mode} disabled={locked} onChange={(event) => {
              void onSelectionChange({ mode: event.target.value as SessionForkMode, anchorMode: "inclusive" });
            }}>
              <option value="view_prefix_fork">{t('navigation.fork.selectedMessage')}</option>
              <option value="last_completed_turn_fork">{t('navigation.fork.lastCompleted')}</option>
              <option value="full_rollout_copy">{t('navigation.fork.fullHistory')}</option>
            </select>
          </label>
          {mode === "view_prefix_fork" ? <label className="session-name-field"><span>{t('navigation.fork.anchorPosition')}</span>
            <select aria-label={t('navigation.fork.anchorPosition')} value={anchorMode} disabled={locked} onChange={(event) => {
              void onSelectionChange({ anchorMode: event.target.value as "inclusive" | "before" });
            }}>
              <option value="inclusive">{t('navigation.fork.includeAnchor')}</option><option value="before">{t('navigation.fork.beforeAnchor')}</option>
            </select>
          </label> : null}
          <label className="session-name-field"><span>{t('navigation.fork.creationLocation')}</span>
            <select aria-label={t('navigation.fork.creationLocation')} value={creationKind} disabled={locked} onChange={(event) => {
              const kind = event.target.value as SessionForkCreationKind;
              setCreationKind(kind);
              void onSelectionChange({ targetContainerUri: kind === "new_child_thread" ? sourcePoint.child_container_uri : sourcePoint.main_container_uri });
              setIncludeGoal(false);
            }}>
              <option value="new_child_thread">{t('navigation.fork.newThread')}</option><option value="new_session_main">{t('navigation.sessions.newDefaultTitle')}</option>
            </select>
          </label>
          <label className="session-name-field"><span>{t('navigation.fork.targetContainer')}</span>
            <input value={targetContainerUri} disabled={locked} required onChange={(event) => {
              void onSelectionChange({ targetContainerUri: event.target.value });
            }} />
          </label>
          <label className="session-name-field"><span>{t('navigation.fork.targetName')}</span>
            <input value={title} disabled={locked} onChange={(event) => setTitle(event.target.value)} autoFocus />
          </label>
          <fieldset disabled={locked || !resourcesReady}>
            <legend>{t('navigation.fork.attachments')}</legend>
            <p>{t('navigation.fork.attachmentsDescription')}</p>
            <p>{t('navigation.fork.attachmentStorageDescription')}</p>
            {sourcePoint.resources.length === 0 ? <p>{t('navigation.fork.noAttachments')}</p> : sourcePoint.resources.map((resource) => (
              <label className="session-name-field" key={resource.resource_uri}>
                <span>{resource.name ?? resource.resource_uri} · {resource.required_for_context ? t('navigation.fork.requiredAttachment') : t('navigation.fork.optionalAttachment')}
                  {resource.availability === "unavailable" ? t('navigation.fork.sourceUnavailable') : t('navigation.fork.sourceAvailable')}
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
                  <option value="default">{t('navigation.fork.defaultAction')}</option>
                  <option value="copy_snapshot" disabled={resource.availability === "unavailable"}>{t('navigation.fork.copySnapshot')}</option>
                  <option value="omit" disabled={resource.required_for_context}>{t('navigation.fork.omit')}</option>
                </select>
              </label>
            ))}
          </fieldset>
          <fieldset disabled={locked || !resourcesReady}>
            <legend>{t('navigation.fork.runtimeResources')}</legend>
            <p>{t('navigation.fork.runtimeResourcesDescription')}</p>
            {sourcePoint.live_resources.length === 0 ? <p>{t('navigation.fork.noRuntimeResources')}</p> : sourcePoint.live_resources.map((resource) => {
              const key = `${resource.kind}:${resource.resource_id}`;
              const eligible = resource.retention_scope === "workspace" && resource.reconciliation_status === "verified"
                && resource.available_actions.includes("attach");
              return <label className="session-name-field" key={key}>
                <span><input type="checkbox" aria-label={t('navigation.fork.attachResource', resource.kind === "terminal" ? t('navigation.fork.terminal') : t('navigation.fork.browser'), resource.resource_id)}
                  checked={liveChoices.has(key)} disabled={!eligible}
                  onChange={(event) => setLiveChoices((prior) => {
                    const next = new Set(prior);
                    if (event.target.checked) next.add(key); else next.delete(key);
                    return next;
                  })} />{resource.kind === "terminal" ? t('navigation.fork.terminal') : t('navigation.fork.browser')} · {resource.resource_id}</span>
                <span>{resource.retention_scope === "workspace" ? t('navigation.fork.workspaceRetained') : t('navigation.fork.sourceThreadOnly')} · {resource.status} · {resource.reconciliation_status}</span>
                <span>{t('navigation.fork.source')}{resource.source_thread.session_id} / {resource.source_thread.thread_id}</span>
                <span>{t('navigation.fork.currentAssociations')}{resource.thread_associations.map((ref) => `${ref.session_id} / ${ref.thread_id}`).join(t('navigation.listSeparator')) || t('navigation.fork.none')}</span>
                {resource.error ? <span role="status">{resource.error}</span> : null}
              </label>;
            })}
          </fieldset>
          {creationKind === "new_session_main" && sourcePoint.goal_available ? <>
            <label><input type="checkbox" checked={includeGoal} disabled={locked} onChange={(event) => setIncludeGoal(event.target.checked)} />{t('navigation.fork.inheritGoal')}</label>
            {includeGoal ? <label className="session-name-field"><span>{t('navigation.fork.tokenBudget')}</span>
              <input type="number" min="1" step="1" value={tokenBudget} disabled={locked} onChange={(event) => setTokenBudget(event.target.value)} />
            </label> : null}
          </> : null}
        </> : null}
        {state.error ? <div className="session-name-error" role="alert">{state.error}</div> : null}
        </div>
        <div className="session-name-actions">
          <button type="button" disabled={state.submitting} onClick={onCancel}>{t('navigation.cancel')}</button>
          <button type="submit" className="session-name-confirm" disabled={!sourcePoint || (!state.frozenRequest && !resourcesReady) || state.submitting || !targetContainerUri.trim()}>
            {state.submitting ? t('navigation.fork.creating') : state.frozenRequest ? t('navigation.fork.retry') : t('navigation.fork.create')}
          </button>
        </div>
      </form>
    </div>, overlayRoot ?? document.body,
  );
}
