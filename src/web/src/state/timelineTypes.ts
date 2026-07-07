import type { AttachmentRef, Message } from "../types/backend";

export type TimelineItem =
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
      phase: string;
      active: boolean;
      timestamp: string | null;
      eventCount: number;
      rawEvents: Array<{ type: string; payload: Record<string, unknown> }>;
    }
  | {
      kind: "aggregated_tool";
      id: string;
      toolName: string;
      inputText: string;
      resultText: string;
      timestamp: string | null;
      rawStart: Record<string, unknown>;
      rawEnd: Record<string, unknown>;
      failed?: boolean;
    }
  | {
      kind: "skill_summary";
      id: string;
      readSkills: string[];
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
    };
