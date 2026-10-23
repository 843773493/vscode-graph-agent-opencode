import { describe, expect, test } from "bun:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { requestLogRecordFixture } from "../../../state/requestLogDisplay/requestLogRecordFixture";
import RequestLogPanel, {
  INITIAL_VISIBLE_REQUEST_LOG_COUNT,
} from "./RequestLogPanel";

describe("RequestLogPanel", () => {
  test("默认按时间正序渲染最新一批请求", () => {
    const logCount = INITIAL_VISIBLE_REQUEST_LOG_COUNT + 2;
    const html = renderToStaticMarkup(
      <RequestLogPanel
        logs={Array.from({ length: logCount }, (_, index) => {
          const timestamp = index + 1;
          return requestLogRecordFixture({
            session_id: "ses_requests",
            job_id: `job_${timestamp}`,
            timestamp,
            file_name: `${timestamp}.json`,
            request: { model_name: `model-${timestamp}`, messages: [] },
          });
        })}
        loading={false}
        error={null}
        loadedAt={null}
        sessionId="ses_requests"
        scopeKey="gw_demo:ses_requests"
        active
        onRetryRequestLogs={() => {}}
      />,
    );

    expect(html).toContain("向上滚动加载更早请求");
    expect(html).not.toContain(">model-1<");
    expect(html).not.toContain(">model-2<");
    expect(html.indexOf("model-3")).toBeLessThan(html.indexOf("model-12"));
    expect(html).toContain("已显示 10");
  });

  test("加载失败时给出可见错误与重试入口，而不是只留一句失败文案", () => {
    const html = renderToStaticMarkup(
      <RequestLogPanel
        logs={[]}
        loading={false}
        error="请求超时: /api/v1/sessions/ses_requests/llm-request-logs"
        loadedAt={null}
        sessionId="ses_requests"
        scopeKey="gw_demo:ses_requests"
        active
        onRetryRequestLogs={() => {}}
      />,
    );

    expect(html).toContain('role="alert"');
    expect(html).toContain("请求超时");
    expect(html).toContain("重新读取请求日志");
  });
});
