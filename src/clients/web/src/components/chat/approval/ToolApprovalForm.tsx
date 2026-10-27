import { useRef, useState } from "react";
import type { ToolApproval, ToolApprovalDecision, ToolApprovalDecisionType, ToolApprovalSubmitRequest } from "../../../types/backend";
import { errorMessage } from "../../../utils/errorMessage";

const labels: Record<ToolApprovalDecisionType, string> = {
  approve: "批准执行", edit: "编辑后执行", reject: "拒绝执行", respond: "直接提供结果",
};

export default function ToolApprovalForm({ approvals, disabled, onSubmit }: {
  approvals: ToolApproval[]; disabled: boolean;
  onSubmit: (jobId: string, request: ToolApprovalSubmitRequest) => Promise<void>;
}) {
  const [error, setError] = useState<string | null>(null);
  const command = useRef<{ fingerprint: string; id: string } | null>(null);
  const [decisions, setDecisions] = useState<Record<string, string>>({});
  return <form aria-label="工具操作审批" onSubmit={async (event) => {
    event.preventDefault();
    if (disabled) return;
    const data = new FormData(event.currentTarget);
    try {
      const responses = approvals.map((approval) => ({ approval_id: approval.approval_id,
        decisions: approval.actions.map((action, index): ToolApprovalDecision => {
          const key = `${approval.approval_id}:${index}`;
          const selected = action.allowed_decisions.find((type) => type === data.get(`${key}:type`));
          if (!selected) throw new Error(`请为 ${action.name} 选择决定`);
          const result: ToolApprovalDecision = { tool_call_id: action.tool_call_id, type: selected };
          if (selected === "edit") {
            const args: unknown = JSON.parse(String(data.get(`${key}:args`)));
            if (!args || Array.isArray(args) || typeof args !== "object") throw new Error("编辑后的参数必须是 JSON 对象");
            result.edited_action = { name: String(data.get(`${key}:name`)), args: args as Record<string, unknown> };
          }
          if (selected === "reject" || selected === "respond") result.message = String(data.get(`${key}:message`) ?? "");
          return result;
        }),
      }));
      const fingerprint = JSON.stringify(responses);
      if (command.current?.fingerprint !== fingerprint) command.current = { fingerprint, id: crypto.randomUUID() };
      setError(null);
      await onSubmit(approvals[0].job_id, { command_id: command.current.id,
        checkpoint_id: approvals[0].checkpoint_id, responses });
    } catch (failure) { setError(errorMessage(failure)); }
  }}>
    <h3>确认工具操作</h3>
    {approvals.flatMap((approval) => approval.actions.map((action, index) => {
      const key = `${approval.approval_id}:${index}`;
      const selected = decisions[key];
      return <fieldset key={key} disabled={disabled} data-tool-call-id={action.tool_call_id}>
        <legend>{action.name}</legend>
        <pre>{JSON.stringify(action.args, null, 2)}</pre>
        <label>操作决定<select required name={`${key}:type`} value={selected ?? ""}
          onChange={(event) => setDecisions((previous) => ({ ...previous, [key]: event.target.value }))}>
          <option value="" disabled>请选择</option>
          {action.allowed_decisions.map((type) => <option key={type} value={type}>{labels[type]}</option>)}
        </select></label>
        {selected === "edit" ? <>
          <label>工具名称<input required name={`${key}:name`} defaultValue={action.name} /></label>
          <label>参数 JSON<textarea required name={`${key}:args`} defaultValue={JSON.stringify(action.args, null, 2)} rows={5} /></label>
        </> : null}
        {selected === "reject" || selected === "respond" ? <label>{selected === "respond" ? "提供结果" : "拒绝原因"}
          <textarea name={`${key}:message`} required={selected === "respond"} rows={2} />
        </label> : null}
      </fieldset>;
    }))}
    {error ? <p role="alert">{error}</p> : null}
    <button type="submit" disabled={disabled}>提交审批决定</button>
  </form>;
}
