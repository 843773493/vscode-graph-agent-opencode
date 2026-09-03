import { describe, expect, test } from "bun:test";
import { createSessionTurnTimeline } from "../session/turnTimeline";
import { shouldLoadDefaultViewChangesHint } from "../defaultViewChanges";

describe("主聊天空历史的变更提示加载条件", () => {
  test("只有 ready 且没有历史和 live 消息时才加载", () => {
    const timeline = createSessionTurnTimeline("workspace:session");
    expect(shouldLoadDefaultViewChangesHint({
      contentView: "default",
      sessionId: "session",
      timeline: {
        ...timeline,
        phase: "ready",
        projectionState: "ready",
      },
      conversationCount: 0,
    })).toBe(true);
    expect(shouldLoadDefaultViewChangesHint({
      contentView: "default",
      sessionId: "session",
      timeline: {
        ...timeline,
        phase: "ready",
        projectionState: "ready",
        orderedTurnIds: ["turn-1"],
      },
      conversationCount: 0,
    })).toBe(false);
    expect(shouldLoadDefaultViewChangesHint({
      contentView: "default",
      sessionId: "session",
      timeline: {
        ...timeline,
        phase: "ready",
        projectionState: "ready",
      },
      conversationCount: 1,
    })).toBe(false);
  });

  test("未准备好、无会话或非默认视图不加载", () => {
    const timeline = createSessionTurnTimeline("workspace:session");
    for (const candidate of [
      {
        contentView: "events" as const,
        sessionId: "session",
        timeline: { ...timeline, phase: "ready" as const },
      },
      {
        contentView: "default" as const,
        sessionId: null,
        timeline: { ...timeline, phase: "ready" as const },
      },
      {
        contentView: "default" as const,
        sessionId: "session",
        timeline: { ...timeline, phase: "bootstrapping" as const },
      },
      {
        contentView: "default" as const,
        sessionId: "session",
        timeline: {
          ...timeline,
          phase: "ready" as const,
          projectionState: "partial" as const,
        },
      },
    ]) {
      expect(shouldLoadDefaultViewChangesHint({
        ...candidate,
        conversationCount: 0,
      })).toBe(false);
    }
  });
});
