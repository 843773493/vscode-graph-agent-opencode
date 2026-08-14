import { describe, expect, test } from "bun:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import type { LLMRequestLogRecord } from "../types/backend";
import RequestLogPanel, {
  INITIAL_VISIBLE_REQUEST_LOG_COUNT,
} from "./RequestLogPanel";

function requestLog(index: number): LLMRequestLogRecord {
  return {
    session_id: "ses_requests",
    job_id: `job_${index}`,
    timestamp: index,
    file_name: `${index}.json`,
    file_path: `/logs/${index}.json`,
    request: { model_name: `model-${index}`, messages: [] },
    response: {},
    upstream: {},
  };
}

describe("RequestLogPanel", () => {
  test("默认按时间正序渲染最新一批请求", () => {
    const logCount = INITIAL_VISIBLE_REQUEST_LOG_COUNT + 2;
    const html = renderToStaticMarkup(
      <RequestLogPanel
        logs={Array.from({ length: logCount }, (_, index) => requestLog(index + 1))}
        loading={false}
        error={null}
        loadedAt={null}
        sessionId="ses_requests"
        active
      />,
    );

    expect(html).toContain("向上滚动加载更早请求");
    expect(html).not.toContain(">model-1<");
    expect(html).not.toContain(">model-2<");
    expect(html.indexOf("model-3")).toBeLessThan(html.indexOf("model-12"));
    expect(html).toContain("已显示 10");
  });
});
