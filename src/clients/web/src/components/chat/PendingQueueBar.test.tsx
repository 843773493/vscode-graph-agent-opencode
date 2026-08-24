import { describe, expect, test } from "bun:test";
import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";

import PendingQueueBar from "./PendingQueueBar";
import type { ConversationView } from "../../types/frontend";

function queuedConversation(
  conversationId: string,
  sequence: number,
  content: string,
  waitingReason?: string,
): ConversationView {
  return {
    conversationId,
    displayMode: "live",
    sessionId: "ses_queue",
    userMessage: {
      message_id: conversationId,
      session_id: "ses_queue",
      role: "user",
      content,
      attachments: [],
      metadata: {},
      created_at: "2026-08-22T00:00:00Z",
      updated_at: "2026-08-22T00:00:00Z",
    },
    assistantMessages: [],
    events: [],
    status: "queued",
    jobId: `job_${conversationId}`,
    pending: true,
    deliveryPolicy: "after_turn",
    enqueueSequence: sequence,
    waitingReason,
    source: "pending",
  };
}

describe("PendingQueueBar", () => {
  test("只显示仍在队列中的消息，并位于 Composer 前的队列区域", async () => {
    let cleared = 0;
    let renderer!: ReactTestRenderer;
    act(() => {
      renderer = create(
        <PendingQueueBar
          conversations={[
            {
              ...queuedConversation("active", 1, "已经发送"),
              activeJobOverlay: true,
            },
            queuedConversation("queued_a", 2, "等待第一条完成"),
            queuedConversation("queued_b", 3, "等待中断边界", "等待已提交的 interrupt 边界"),
          ]}
          onClear={async () => {
            cleared += 1;
          }}
          onUpdate={async () => undefined}
          onRemove={async () => undefined}
          onChangePolicy={async () => undefined}
        />,
      );
    });
    const activeRenderer = renderer;

    const hasText = (text: string) => activeRenderer.root.findAll(
      (node) => typeof node.type === "string" && node.children.join("") === text,
    );
    const queue = activeRenderer.root.findAll(
      (node) => typeof node.type === "string"
        && String(node.props["aria-label"] ?? "").startsWith("待处理消息队列"),
    );
    expect(queue).toHaveLength(1);
    expect(queue[0]?.props["data-queue-size"]).toBe(2);
    expect(hasText("已经发送")).toHaveLength(0);
    expect(hasText("等待第一条完成")).toHaveLength(1);
    expect(hasText("等待中断边界")).toHaveLength(1);

    expect(activeRenderer.root.findAllByProps({
      className: "chat-pending-direction-button",
    })).toHaveLength(2);
    const directionButtons = activeRenderer.root.findAllByProps({
      className: "chat-pending-direction-button",
    });
    act(() => {
      directionButtons[0]?.props.onClick();
    });
    expect(activeRenderer.root.findAllByProps({
      className: "chat-pending-direction-menu",
    })).toHaveLength(1);

    await act(async () => {
      activeRenderer.root.findByProps({ "aria-label": "全部撤回" }).props.onClick();
    });
    expect(cleared).toBe(1);
    activeRenderer.unmount();
  });
});
