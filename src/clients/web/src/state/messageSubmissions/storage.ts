import type { MessageRunRequest } from "../../types/backend";
import { workspaceOwnerFromKey } from "../../api/workspaceIdentity";
import { browserViewKey, readBrowserView, updateBrowserView } from "../browserViewStore";

export interface MessageSubmissionOwner {
  workspaceKey: string;
  sessionId: string;
  threadId: string;
}

export interface FrozenMessageSubmission {
  owner: MessageSubmissionOwner;
  request: MessageRunRequest;
  createdAt: string;
  state: "unconfirmed" | "rejected";
  error?: string;
}

export function messageSubmissionKey(owner: MessageSubmissionOwner): string {
  const workspace = workspaceOwnerFromKey(owner.workspaceKey);
  return browserViewKey(workspace.gatewayId, "message-submissions", workspace.workspaceId, owner.sessionId, owner.threadId);
}

export async function loadMessageSubmissions(owner: MessageSubmissionOwner): Promise<FrozenMessageSubmission[]> {
  return await readBrowserView<FrozenMessageSubmission[]>(messageSubmissionKey(owner)) ?? [];
}

/** 正文和内联附件一次冻结；列表只含未交接操作，不能当作消息历史缓存淘汰。 */
export async function freezeMessageSubmission(owner: MessageSubmissionOwner, request: MessageRunRequest): Promise<FrozenMessageSubmission> {
  const frozen: FrozenMessageSubmission = {
    owner: { ...owner }, request: structuredClone(request), createdAt: new Date().toISOString(), state: "unconfirmed",
  };
  for (const attachment of frozen.request.message.attachments ?? []) {
    if (attachment.file_id.startsWith("blob:") || attachment.data_url?.startsWith("blob:")) {
      throw new Error("消息附件尚未保存为可恢复输入，不能发送临时页面 URL");
    }
  }
  await updateBrowserView<FrozenMessageSubmission[]>(messageSubmissionKey(owner), current => {
    if (current?.some(item => item.request.operation_id === request.operation_id)) {
      throw new Error("消息操作身份已冻结，重试必须使用原输入");
    }
    return [...current ?? [], frozen];
  });
  return frozen;
}

export async function rejectMessageSubmission(record: FrozenMessageSubmission, error: string): Promise<void> {
  await updateBrowserView<FrozenMessageSubmission[]>(messageSubmissionKey(record.owner), current => current?.map(item =>
    item.request.operation_id === record.request.operation_id ? { ...item, state: "rejected", error } : item,
  ));
}

export async function releaseMessageSubmission(record: FrozenMessageSubmission): Promise<void> {
  await updateBrowserView<FrozenMessageSubmission[]>(messageSubmissionKey(record.owner), current => {
    const remaining = current?.filter(item => item.request.operation_id !== record.request.operation_id);
    return remaining?.length ? remaining : undefined;
  });
}

/** 失败输入直接交给用户明确的新操作；新冻结和旧记录释放在同一事务完成。 */
export async function resubmitRejectedMessage(record: FrozenMessageSubmission, content: string): Promise<FrozenMessageSubmission> {
  if (record.state !== "rejected") throw new Error("结果未知的消息不能改写原输入");
  const next: FrozenMessageSubmission = { ...record, state: "unconfirmed", error: undefined,
    createdAt: new Date().toISOString(), request: { ...record.request,
      operation_id: `op_${crypto.randomUUID().replaceAll("-", "")}`,
      message: { ...record.request.message, content } } };
  await updateBrowserView<FrozenMessageSubmission[]>(messageSubmissionKey(record.owner), current => {
    if (!current?.some(item => item.request.operation_id === record.request.operation_id && item.state === "rejected")) {
      throw new Error("原失败输入已由另一窗口处理，请刷新");
    }
    return current.map(item => item.request.operation_id === record.request.operation_id ? next : item);
  });
  return next;
}
