import { useEffect, useRef, useState } from "react";
import { t } from "../../../../shared/core/i18n/index";
import { reconcileMessageSubmissions, submitFrozenMessage } from "../../api/session/sessionMessages";
import { subscribeBrowserViews } from "../../state/browserViewStore";
import {
  loadMessageSubmissions, messageSubmissionKey, releaseMessageSubmission, resubmitRejectedMessage,
  type FrozenMessageSubmission, type MessageSubmissionOwner,
} from "../../state/messageSubmissions/storage";
import { errorMessage } from "../../utils/errorMessage";

/** 失败输入独立编辑，不覆盖用户后来写下的 Composer 草稿。 */
export default function MessageSubmissionRecovery({ owner, port, onAccepted }: {
  owner: MessageSubmissionOwner | null; port: number; onAccepted: () => void;
}) {
  const key = owner ? messageSubmissionKey(owner) : null;
  const currentKey = useRef(key);
  currentKey.current = key;
  const lifetime = useRef(0);
  const [loaded, setLoaded] = useState<{ key: string; records: FrozenMessageSubmission[] } | null>(null);
  const [edited, setEdited] = useState<Record<string, string>>({});
  const [failure, setFailure] = useState<{ key: string; text: string } | null>(null);
  const [busyKey, setBusyKey] = useState<string | null>(null);
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
      }).catch(error => { if (active && reading === revision) setFailure({ key, text: errorMessage(error) }); });
    };
    read();
    const unsubscribe = subscribeBrowserViews(changed => { if (changed === key) read(); });
    return () => { active = false; lifetime.current += 1; unsubscribe(); };
  }, [key]);
  const records = loaded?.key === key ? loaded.records : [];
  const run = (operation: () => Promise<boolean | void>) => {
    if (busy || !key) return;
    const generation = lifetime.current;
    setBusyKey(key); setFailure(null);
    void operation().then(accepted => { if (accepted && lifetime.current === generation && currentKey.current === key) onAccepted(); })
      .catch(error => { if (lifetime.current === generation && currentKey.current === key) setFailure({ key, text: errorMessage(error) }); })
      .finally(() => setBusyKey(current => current === key ? null : current));
  };
  if (!records.length && failure?.key !== key) return null;
  return <div aria-label={t("composer.submission.label")} className="composer-submission-recovery">
    {failure?.key === key && <p role="alert">{failure.text}</p>}
    {records.map(record => <div key={record.request.operation_id}>
      <p>{t(record.state === "rejected" ? "composer.submission.rejected" : "composer.submission.unknown")}</p>
      {record.state === "rejected" ? <textarea aria-label={t("composer.submission.input")}
        value={edited[record.request.operation_id] ?? record.request.message.content}
        onChange={event => setEdited(current => ({ ...current, [record.request.operation_id]: event.target.value }))} />
        : <p>{record.request.message.content}</p>}
      {record.error && <p role="alert">{record.error}</p>}
      <span>{t("composer.submission.attachments", record.request.message.attachments?.length ?? 0)}</span>
      <button type="button" disabled={busy} onClick={() => run(async () => {
        const input = record.state === "rejected" ? await resubmitRejectedMessage(record,
          edited[record.request.operation_id] ?? record.request.message.content) : record;
        await submitFrozenMessage(port, input); return true;
      })}>{t(record.state === "rejected" ? "composer.submission.resubmit" : "composer.submission.retry")}</button>
      {record.state === "rejected" ? <button type="button" disabled={busy}
        onClick={() => run(() => releaseMessageSubmission(record))}>{t("composer.submission.discard")}</button>
        : <button type="button" disabled={busy} onClick={() => run(async () => {
          const results = await reconcileMessageSubmissions(port, record.owner);
          return results.some(result => result.accepted);
        })}>{t("composer.submission.query")}</button>}
    </div>)}
  </div>;
}
