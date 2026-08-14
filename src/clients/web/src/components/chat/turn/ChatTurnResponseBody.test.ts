import { describe, expect, test } from "bun:test";
import type { ConversationView } from "../../../types/frontend";
import {
  assistantFallback,
  LEGACY_FRAGMENTATION_MAX_LENGTH,
} from "./ChatTurnResponseBody";

describe("ChatTurnResponseBody 大型投影正文", () => {
  test("权威 final_response 直接复用且不进入全文 split 启发", () => {
    const content = "x".repeat(LEGACY_FRAGMENTATION_MAX_LENGTH * 2);
    const value = {
      conversationId: "job_large",
      turnId: "job_large",
      turnRevision: 1,
      turnItemsView: "full",
      sessionId: "session-large",
      userMessage: null,
      assistantMessages: [{
        message_id: "job_large:assistant",
        session_id: "session-large",
        role: "assistant",
        content,
        attachments: [],
        metadata: { source: "turn_projection", summary: false },
        created_at: "2026-07-28T00:00:00Z",
        updated_at: "2026-07-28T00:00:00Z",
      }],
      events: [],
      status: "done",
      jobId: "job_large",
      pending: false,
      source: "turn",
    } satisfies ConversationView;
    const originalSplit = String.prototype.split;
    let splitCalls = 0;
    const monitoredSplit = function split(
      this: string,
      separator?: unknown,
      limit?: number,
    ): string[] {
      splitCalls += 1;
      return Reflect.apply(originalSplit, this, [separator, limit]) as string[];
    };
    String.prototype.split = monitoredSplit as typeof String.prototype.split;
    try {
      expect(assistantFallback(value)).toBe(content);
      expect(splitCalls).toBe(0);
    } finally {
      String.prototype.split = originalSplit;
    }
  });
});
