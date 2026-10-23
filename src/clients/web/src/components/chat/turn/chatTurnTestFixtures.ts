import type { ConversationView } from "../../../types/frontend";
import type { ChatTurnProps } from "./ChatTurn";

export const pendingActionProps = {
  onRemovePending: async () => {},
  onChangePendingPolicy: async () => {},
};

export const mediaProps = {
  apiPort: 8014,
  workspaceId: "gw_test",
};

export function conversation(
  status: ConversationView["status"],
  eventType: ConversationView["events"][number]["type"] = "job_completed",
): ConversationView {
  return {
    conversationId: "msg_user",
    displayMode: "history",
    sessionId: "ses_web_replay",
    userMessage: {
      message_id: "msg_user",
      session_id: "ses_web_replay",
      thread_id: "thr_web_replay",
      role: "user",
      content: "原始问题",
      attachments: [],
      metadata: {},
      created_at: "2026-07-16T00:00:00Z",
      updated_at: "2026-07-16T00:00:00Z",
    },
    assistantMessages: [{
      message_id: "msg_assistant",
      session_id: "ses_web_replay",
      thread_id: "thr_web_replay",
      role: "assistant",
      content: "原始回答",
      attachments: [],
      metadata: {},
      created_at: "2026-07-16T00:00:01Z",
      updated_at: "2026-07-16T00:00:01Z",
    }],
    responseParts: [{
      part_id: "part_final",
      kind: "final_text",
      projection: "detail",
      status: "completed",
      source: { message_sequence: 2 },
      text: "原始回答",
      final: true,
    }],
    events: [{
      event_id: "evt_1",
      session_id: "ses_web_replay",
      thread_id: "thr_web_replay",
      job_id: "job_1",
      step_id: null,
      agent_id: "default",
      timestamp: "2026-07-16T00:00:01Z",
      type: eventType,
      payload: {},
    }],
    status,
    jobId: "job_1",
    pending: false,
    source: "turn",
  };
}

export const replayTurn = async () => {};
export const loadAgentStateMessageRawContent = async () => "<system_reminder>原始消息</system_reminder>";

export function chatTurnProps(value: ConversationView): ChatTurnProps {
  return {
    ...mediaProps,
    conversation: value,
    showRawDetails: false,
    isLastTurn: false,
    sessionBusy: false,
    onLoadAgentStateMessageRawContent: loadAgentStateMessageRawContent,
    onReplayTurn: replayTurn,
    ...pendingActionProps,
  };
}
