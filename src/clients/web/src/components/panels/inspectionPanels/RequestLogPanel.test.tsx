import { describe, expect, test } from "bun:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import {
  buildRequestLogKeyFlow,
  buildRequestLogKeyFlowFromOrderedLogs,
} from "../../../state/requestLogDisplay";
import { requestLogRecordFixture } from "../../../state/requestLogDisplay/requestLogRecordFixture";
import RequestLogPanel, {
  INITIAL_VISIBLE_REQUEST_LOG_COUNT,
} from "./RequestLogPanel";

describe("RequestLogPanel", () => {
  test("有序日志的关键流转复用顺序且保留时间语义", () => {
    const older = requestLogRecordFixture({
      timestamp: 1,
      response: { result: [{ content: "较早回复" }] },
    });
    const newer = requestLogRecordFixture({
      timestamp: 2,
      response: { result: [{ content: "最新回复" }] },
    });
    const orderedLogs = [older, newer];
    const orderedFlow = buildRequestLogKeyFlowFromOrderedLogs(orderedLogs);

    expect(orderedFlow).toEqual(buildRequestLogKeyFlow([newer, older]));
    expect(orderedFlow.finalText).toBe("最新回复");
  });

  test("默认按时间正序渲染最新一批请求", () => {
    const logCount = INITIAL_VISIBLE_REQUEST_LOG_COUNT + 2;
    const html = renderToStaticMarkup(
      <RequestLogPanel
        apiPort={8011} workspaceId="workspace-test"
        logs={Array.from({ length: logCount }, (_, index) => {
          const timestamp = index + 1;
          return { session_id: "ses_requests", recorded_at: timestamp, size_bytes: timestamp * 1024,
            file_name: `${timestamp}.json` };
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
    expect(html).not.toContain("第 1 次");
    expect(html).not.toContain("第 2 次");
    expect(html.indexOf("第 3 次")).toBeLessThan(html.indexOf("第 12 次"));
    expect(html).toContain("展开读取正文");
    expect(html).toContain("已显示 10");
  });

  test("加载失败时给出可见错误与重试入口，而不是只留一句失败文案", () => {
    const html = renderToStaticMarkup(
      <RequestLogPanel
        apiPort={8011} workspaceId="workspace-test"
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
