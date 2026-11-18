import { useContext, useEffect, useRef, useState } from "react";
import { t } from "../../../../shared/core/i18n/index";
import { reconcileMessageSubmissions, submitFrozenMessage } from "../../api/session/sessionMessages";
import { subscribeBrowserViews } from "../../state/browserViewStore";
import {
  loadMessageSubmissions, messageSubmissionKey, releaseMessageSubmission, resubmitRejectedMessage,
  type FrozenMessageSubmission, type MessageSubmissionOwner,
} from "../../state/messageSubmissions/storage";
import { errorMessage } from "../../utils/errorMessage";
import { ComposerDiagnosticsContext } from "./ComposerDiagnosticsContext";
import AnchoredOverlay from "../overlays/AnchoredOverlay";

/** 失败输入独立编辑，不覆盖用户后来写下的 Composer 草稿。 */
export default function MessageSubmissionRecovery({ owner, port, onAccepted, render }: {
  owner: MessageSubmissionOwner | null; port: number; onAccepted: () => void;
  render?: (state: { count: number; attention: boolean; content: React.ReactNode }) => React.ReactNode;
}) {
  const key = owner ? messageSubmissionKey(owner) : null;
  const report = useContext(ComposerDiagnosticsContext);
  const currentKey = useRef(key);
  currentKey.current = key;
  const lifetime = useRef(0);
  const [loaded, setLoaded] = useState<{ key: string; records: FrozenMessageSubmission[] } | null>(null);
  const [edited, setEdited] = useState<Record<string, string>>({});
  const [failure, setFailure] = useState<{ key: string; operationId?: string } | null>(null);
  const [busyKey, setBusyKey] = useState<string | null>(null);
  const [menuId, setMenuId] = useState<string | null>(null);
  const menuAnchor = useRef<HTMLButtonElement | null>(null);
  const busy = busyKey === key && key !== null;
  useEffect(() => {
    if (!owner || !key) return;
    lifetime.current += 1;
    let active = true;
    let revision = 0;
    const read = () => {
      const reading = ++revision;
      void loadMessageSubmissions(owner).then(records => {
        if (active && reading === revision) setLoaded({ key, records });
      }).catch(error => {
        if (active && reading === revision) {
          setFailure({ key }); report(`${t("composer.submission.label")}: ${errorMessage(error)}`);
        }
      });
    };
    read();
    const unsubscribe = subscribeBrowserViews(changed => { if (changed === key) read(); });
    return () => { active = false; lifetime.current += 1; unsubscribe(); };
  }, [key, report]);
  const records = loaded?.key === key ? loaded.records : [];
  const menuRecord = records.find(record => record.request.operation_id === menuId);
  useEffect(() => {
    if (loaded?.key === key) for (const record of loaded.records) {
      if (record.error) report(`operation_id=${record.request.operation_id}: ${record.error}`);
    }
  }, [loaded, key, report]);
  const run = (operationId: string, operation: () => Promise<boolean | void>) => {
    if (busy || !key) return;
    const generation = lifetime.current;
    setBusyKey(key); setFailure(null);
    void operation().then(accepted => { if (accepted && lifetime.current === generation && currentKey.current === key) onAccepted(); })
      .catch(error => {
        report(`operation_id=${operationId}: ${errorMessage(error)}`);
        if (lifetime.current === generation && currentKey.current === key) setFailure({ key, operationId });
      })
      .finally(() => setBusyKey(current => current === key ? null : current));
  };
  const attention = failure?.key === key || records.some(record => record.state === "rejected" || Boolean(record.error));
  const content = !records.length ? null : <div aria-label={t("composer.submission.label")} className="composer-submission-recovery" role="list">
    {records.map(record => {
      const failed = failure?.key === key && failure.operationId === record.request.operation_id;
      return <div key={record.request.operation_id} className="composer-submission-row" role="listitem"
      data-operation-id={record.request.operation_id}>
      <div className="composer-submission-message">
      {record.state === "rejected" ? <textarea aria-label={t("composer.submission.input")}
        rows={1}
        value={edited[record.request.operation_id] ?? record.request.message.content}
        onChange={event => setEdited(current => ({ ...current, [record.request.operation_id]: event.target.value }))} />
        : <span title={record.request.message.content}>{record.request.message.content}</span>}
      </div>
      <span className={`chat-pending-composer-item-status${failed ? " needs-attention" : ""}`}
        role={failed ? "alert" : undefined}
        title={[t(record.state === "rejected" ? "composer.submission.rejected" : "composer.submission.unknown"),
          ...(failed ? [t("composer.operationFailed")] : [])].join("\n")}>
        {t(record.state === "rejected" ? "composer.submission.rejectedStatus" : "composer.submission.waitingStatus")}
      </span>
      <button type="button" className="composer-submission-action" disabled={busy}
        aria-label={t(record.state === "rejected" ? "composer.submission.resubmit" : "composer.submission.retry")}
        title={t(record.state === "rejected" ? "composer.submission.resubmit" : "composer.submission.retry")}
        onClick={() => run(record.request.operation_id, async () => {
        const input = record.state === "rejected" ? await resubmitRejectedMessage(record,
          edited[record.request.operation_id] ?? record.request.message.content) : record;
        await submitFrozenMessage(port, input); return true;
      })}><span className={`codicon ${record.state === "rejected" ? "codicon-arrow-up" : "codicon-refresh"}`} aria-hidden="true" /></button>
      {record.state === "rejected" ? <button type="button" className="composer-submission-action" disabled={busy}
        aria-label={t("composer.submission.discard")} title={t("composer.submission.discard")}
        onClick={() => run(record.request.operation_id, () => releaseMessageSubmission(record))}>
        <span className="codicon codicon-trash" aria-hidden="true" /></button>
        : <button type="button" className="composer-submission-action" disabled={busy}
          aria-label={t("composer.status.more")} title={t("composer.status.more")} aria-haspopup="menu"
          aria-expanded={menuId === record.request.operation_id}
          onClick={event => { menuAnchor.current = event.currentTarget; setMenuId(current => current === record.request.operation_id ? null : record.request.operation_id); }}>
          <span className="codicon codicon-ellipsis" aria-hidden="true" /></button>}
    </div>; })}
    <AnchoredOverlay open={Boolean(menuRecord)} anchorRef={menuAnchor} placement="top-end" manageFocus onClose={() => setMenuId(null)}>
      {menuRecord ? <div className="composer-view-menu" role="menu" aria-label={t("composer.status.more")}
        onKeyDown={event => {
          if (event.key === "Escape") {
            event.preventDefault(); event.stopPropagation(); setMenuId(null);
          } else if (["ArrowUp", "ArrowDown", "Home", "End"].includes(event.key)) {
            event.preventDefault(); event.currentTarget.querySelector<HTMLButtonElement>("button")?.focus();
          }
        }}>
        <button type="button" role="menuitem" className="composer-view-menu-item"
          onClick={() => {
            setMenuId(null);
            run(menuRecord.request.operation_id, async () => {
              const results = await reconcileMessageSubmissions(port, menuRecord.owner);
              return results.some(result => result.accepted);
            });
          }}>{t("composer.submission.query")}</button>
      </div> : null}
    </AnchoredOverlay>
  </div>;
  return render ? render({ count: records.length, attention, content }) : content;
}
