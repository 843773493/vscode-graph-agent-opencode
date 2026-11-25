import type { AttachmentRef, Message, TurnResponseItem } from "../../types/backend";

export type TimelineItem = {
  responseSources?: Array<{ id: string; kind: string; source: TurnResponseItem["source"]; projection?: TurnResponseItem["projection"]; final?: boolean; tool_call_id?: string | null }>;
} & (
  | { kind: "context_event"; id: string; notices: TurnResponseItem[] }
  | {
      kind: "message";
      id: string;
      role: Message["role"];
      content: string;
      attachments: AttachmentRef[];
      createdAt: string | null;
      metadata: Record<string, unknown>;
    }
  | {
      kind: "trace";
      id: string;
      eventType: string;
      payload: Record<string, unknown>;
      timestamp: string | null;
    }
  | {
      kind: "status";
      id: string;
      title: string;
      detail: string;
      timestamp: string | null;
    }
  | {
      kind: "aggregated_text";
      id: string;
      text: string;
      partKind: "markdown" | "reasoning" | "compaction" | "content";
      content?: TurnResponseItem["content"];
      carrierType?: string | null;
      truncated?: boolean;
      media?: { kind: "image" | "audio" | "video"; source: string };
      /**
       * reasoning 的稳定语义类别；不暴露 Provider carrier，但保留
       * detail、summary 和 encrypted/redacted 的边界供展示与诊断使用。
       */
      reasoningKind?: "reasoning" | "reasoning_summary" | "reasoning_encrypted";
      redacted?: boolean;
      active: boolean;
      timestamp: string | null;
      eventCount: number;
      rawEvents: Array<{ type: string; payload: Record<string, unknown> }>;
    }
  | {
      kind: "aggregated_tool";
      id: string;
      toolName: string;
      toolCallId?: string;
      inputText: string;
      resultText: string;
      timestamp: string | null;
      rawStart: Record<string, unknown>;
      rawEnd: Record<string, unknown>;
      active: boolean;
      detailsLoaded?: boolean;
      /** 只有调用声明；不把声明完整误认为已执行成功。 */
      declared?: boolean;
      failed?: boolean;
      incomplete?: boolean;
      outcomeUnknown?: boolean;
    }
  | {
      kind: "skill_summary";
      id: string;
      acceptedSkillLoads: string[];
      toolResults: Array<{
        toolName: string;
        skillNames: string[];
        invocationToolName?: string;
        resultText: string;
      }>;
      finalText: string;
      timestamp: string | null;
    }
  | {
      kind: "conversation_marker";
      id: string;
      label: string;
      jobId: string | null;
    });
