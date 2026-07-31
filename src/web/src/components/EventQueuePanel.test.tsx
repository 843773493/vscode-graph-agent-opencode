import { describe, expect, test } from "bun:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import type { FrontendReceivedEvent } from "../types/frontend";
import EventQueuePanel, { INITIAL_VISIBLE_EVENT_COUNT } from "./EventQueuePanel";

const historyProps = {
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
