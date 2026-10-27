import { useToolApprovals } from "../../hooks/sessionToolApprovals/useToolApprovals";
import type { ToolApproval } from "../../types/backend";
import ToolApprovalForm from "../chat/approval/ToolApprovalForm";

export default function ToolApprovalPanel({ apiPort, workspaceId, sessionId, threadId }: {
  apiPort: number; workspaceId?: string | null; sessionId: string; threadId: string;
}) {
  const state = useToolApprovals(apiPort, workspaceId, sessionId, threadId);
  const batches = new Map<string, ToolApproval[]>();
  for (const approval of state.approvals) {
    const key = `${approval.job_id}:${approval.checkpoint_id}`;
    batches.set(key, [...(batches.get(key) ?? []), approval]);
  }
  if (!state.error && batches.size === 0) return null;
  return <aside aria-label="待确认的工具操作" className="chat-tool-approvals">
    {state.error ? <div role="alert">{state.error}<button type="button" onClick={() => void state.retry()}>重新读取审批</button></div> : null}
    {[...batches].map(([key, approvals]) => {
      const accepted = state.receipt?.checkpoint_id === approvals[0].checkpoint_id && state.receipt.status !== "rejected";
      return <div key={key}>
        <ToolApprovalForm approvals={approvals} disabled={state.busy || accepted} onSubmit={state.submit} />
        {accepted ? <p role="status">决定已接收，等待执行结果。</p> : null}
      </div>;
    })}
  </aside>;
}
