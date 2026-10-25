import { describe, expect, test } from "bun:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import {
  act,
  create,
  type ReactTestInstance,
  type ReactTestRenderer,
} from "react-test-renderer";
import { LARGE_STRING_DISPLAY_LIMIT } from "../../../utils/jsonDisplay";
import type { FrontendReceivedEvent } from "../../../types/frontend";
import EventQueuePanel, { INITIAL_VISIBLE_EVENT_COUNT } from "./EventQueuePanel";

const historyProps = {
  scopeKey: "gw_demo:ses_events",
  historyLoading: false,
  historyLoadingOlder: false,
  historyHasMore: false,
  historyError: null,
  onLoadOlderHistory: async () => 0,
  onRetryHistory: () => undefined,
};

function traceItem(index: number): FrontendReceivedEvent {
  const timestamp = new Date(Date.UTC(2026, 6, 25, 0, 0, index)).toISOString();
  return {
    id: `initial_load:evt_${index}`,
    kind: "trace",
    sessionId: "ses_events",
    receivedAt: timestamp,
    source: "initial_load",
    event: {
      event_id: `evt_${index}`,
      session_id: "ses_events",
      thread_id: "thr_00000000700070008000000000000001",
      job_id: "job_events",
      type: "job_started",
      phase: "job",
      title: "任务已开始",
      content: `事件 ${index}`,
      timestamp,
      payload: {},
    },
  };
}

function textDeltaItem(index: number, text: string): FrontendReceivedEvent {
  const base = traceItem(index);
  if (base.kind !== "trace") {
    throw new Error("测试 traceItem 必须返回 trace 事件");
  }
  return {
    ...base,
    event: {
      ...base.event,
      type: "text_delta",
      phase: "text",
      title: "文本流",
      content: text,
      payload: { text, kind: "reasoning" },
      raw: { payload: { text, kind: "reasoning" } },
    },
  };
}

function firstEventCard(renderer: ReactTestRenderer): ReactTestInstance {
  const card = renderer.root.findAllByType("details").find((details) =>
    details.findAllByType("summary")[0]?.props.className === "event-queue-card-summary",
  );
  if (!card) throw new Error("事件卡片尚未渲染");
  return card;
}

function detailsByTitle(renderer: ReactTestRenderer, title: string): ReactTestInstance {
  const details = renderer.root.findAllByType("details").find((candidate) =>
    candidate.findAllByType("summary")[0]?.children.join("") === title,
  );
  if (!details) throw new Error(`未找到事件详情: ${title}`);
  return details;
}

function openDetails(details: ReactTestInstance): void {
  const onToggle = details.props.onToggle;
  if (typeof onToggle !== "function") throw new Error("详情元素缺少 toggle 处理器");
  const event = {
    currentTarget: { open: true },
  } as unknown as React.SyntheticEvent<HTMLDetailsElement>;
  act(() => onToggle(event));
}

describe("EventQueuePanel", () => {
  test("默认只渲染最新一批事件且卡片保持折叠", () => {
    const itemCount = INITIAL_VISIBLE_EVENT_COUNT + 10;
    const html = renderToStaticMarkup(
      <EventQueuePanel
        {...historyProps}
        items={Array.from({ length: itemCount }, (_, index) => traceItem(index))}
        limit={200}
        sessionId="ses_events"
        active
      />,
    );

    expect(html.match(/event-queue-card-summary/g)?.length).toBe(
      INITIAL_VISIBLE_EVENT_COUNT,
    );
    expect(html).toContain("向上滚动加载更早事件");
    expect(html).not.toContain(">#10<");
    expect(html).toContain(`>#${itemCount}<`);
    expect(html).not.toContain("<details open");
  });

  test("合并 text_delta 使用普通事件卡布局并保留正文预览", () => {
    const preview = "这段模型增量正文应显示在折叠行";
    const html = renderToStaticMarkup(
      <EventQueuePanel
        {...historyProps}
        items={[
          textDeltaItem(1, preview),
          textDeltaItem(2, preview),
        ]}
        limit={200}
        sessionId="ses_events"
        active
      />,
    );
    const summary = html.match(
      /<summary class="event-queue-card-summary">([\s\S]*?)<\/summary>/,
    )?.[1];

    expect(summary).toContain("text_delta × 2");
    expect(summary).toContain(preview);
    expect(html).toContain(
      'class="panel-card event-queue-card event-type-text_delta event-queue-group-card"',
    );
  });

  test("服务端仍有旧页时展示独立分页入口和透明错误", () => {
    const html = renderToStaticMarkup(
      <EventQueuePanel
        {...historyProps}
        items={[traceItem(1)]}
        limit={200}
        sessionId="ses_events"
        active
        historyHasMore
        historyError="Trace 历史游标已失效"
      />,
    );

    expect(html).toContain("加载更早事件");
    expect(html).toContain("Trace 历史游标已失效");
    expect(html).toContain("重新加载事件历史");
  });
});

describe("EventQueuePanel 超大 payload 兜底", () => {
  test("卡片和 payload 折叠时不格式化大 JSON，展开后仍限制文本长度", () => {
    const huge = "X".repeat(500_000);
    const base = traceItem(1);
    if (base.kind !== "trace") throw new Error("traceItem 必须返回 trace 事件");
    const element = (
      <EventQueuePanel
        {...historyProps}
        items={[{ ...base, event: { ...base.event, payload: { result: huge } } }]}
        limit={200}
        sessionId="ses_events"
        active
      />
    );
    const html = renderToStaticMarkup(element);

    expect(html).not.toContain(huge);
    expect(html).not.toContain("已截断展示");
    expect(html).not.toContain("<pre>");
    expect(html.length).toBeLessThan(20_000);

    let renderer!: ReactTestRenderer;
    act(() => { renderer = create(element); });
    const card = firstEventCard(renderer);
    expect(card.findAllByType("pre")).toHaveLength(0);

    openDetails(card);
    const payloadDetails = detailsByTitle(renderer, "payload");
    expect(payloadDetails.findAllByType("pre")).toHaveLength(0);

    openDetails(payloadDetails);
    const formattedPayload = payloadDetails.findByType("pre").children.join("");
    expect(formattedPayload).toContain("已截断展示");
    expect(formattedPayload).not.toContain(huge);
    renderer.unmount();
  });

  test("text_delta 摘要只保留有界预览并报告完整原文长度", () => {
    const firstText = "X".repeat(LARGE_STRING_DISPLAY_LIMIT + 9);
    const secondText = "后续文本";
    const totalLength = firstText.length + secondText.length;
    const html = renderToStaticMarkup(
      <EventQueuePanel
        {...historyProps}
        items={[textDeltaItem(1, firstText), textDeltaItem(2, secondText)]}
        limit={200}
        sessionId="ses_events"
        active
      />,
    );

    expect(html).toContain(
      `${"X".repeat(LARGE_STRING_DISPLAY_LIMIT)}…（已截断展示，原文 ${totalLength} 字符）`,
    );
    expect(html).not.toContain(firstText);
    expect(html).not.toContain("<pre>");
  });
});

describe("EventQueuePanel 空态与失败态口径", () => {
  test("历史读取失败时不得同时显示「当前会话还没有事件」", () => {
    const html = renderToStaticMarkup(
      <EventQueuePanel
        {...historyProps}
        items={[]}
        limit={200}
        sessionId="ses_events"
        active
        historyError="Trace 历史读取失败：连接被拒绝"
      />,
    );

    expect(html).toContain("Trace 历史读取失败：连接被拒绝");
    expect(html).not.toContain("当前会话还没有事件");
  });
});
