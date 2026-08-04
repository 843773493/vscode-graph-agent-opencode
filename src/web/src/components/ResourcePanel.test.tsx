import { describe, expect, test } from "bun:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import type { SessionResource } from "../types/backend";
import {
  actionLabelForKind,
  groupSessionResources,
  resourceAttentionGroup,
  resourceTreeStatus,
} from "../state/resourceDisplay";
import ResourcePanel from "./ResourcePanel";
import WarmConfirmProvider from "./WarmConfirmProvider";

function resource(
  index: number,
  overrides: Partial<SessionResource> = {},
): SessionResource {
  return {
    resource_id: `resource_${index.toString().padStart(2, "0")}_full_identifier`,
    session_id: "ses_resource_tree",
    kind: "browser",
    name: `浏览器 / 页面 ${index}`,
    status: "running",
    created_at: `2026-07-26T01:${index.toString().padStart(2, "0")}:00Z`,
    updated_at: `2026-07-26T02:${index.toString().padStart(2, "0")}:00Z`,
    started_at: null,
    ended_at: null,
    available_actions: ["cancel", "delete"],
    metadata: {
      title: `页面 ${index}`,
      url: `https://site-${index}.example/path`,
      resource_state: "background",
      client_count: 0,
    },
    ...overrides,
  };
}

describe("后台连接目录", () => {
  test("按用户注意力分组，并将当前预览资源置顶", () => {
    const resources = [
      resource(1),
      resource(2, { metadata: { title: "当前页面", url: "https://active.example", client_count: 1 } }),
      resource(3, { status: "failed", metadata: { error_message: "启动失败" } }),
      resource(4, { metadata: { resource_state: "frozen" } }),
      resource(5, { status: "closed" }),
    ];
    const groups = groupSessionResources(resources, "browser://resource_02_full_identifier");

    expect(groups.map((group) => group.key)).toEqual([
      "active",
      "attention",
      "available",
      "sleeping",
      "history",
    ]);
    expect(groups[0]?.resources[0]?.resource_id).toBe("resource_02_full_identifier");
    expect(resourceAttentionGroup(resources[3]!, null)).toBe("sleeping");
    expect(resourceAttentionGroup(resources[4]!, null)).toBe("history");
    expect(resourceAttentionGroup(
      resources[4]!,
      "browser://resource_05_full_identifier",
    )).toBe("history");
    expect(resourceTreeStatus(resource(6, {
      status: "closed",
      metadata: { resource_state: "background" },
    }))).toBe("已关闭");
    const recoverable = resource(7, {
      status: "lost",
      available_actions: ["resume", "delete"],
      metadata: {
        resource_state: "discarded",
        checkpoint: { version: 1 },
      },
    });
    expect(resourceAttentionGroup(recoverable, null)).toBe("sleeping");
    expect(resourceTreeStatus(recoverable)).toBe("已冷回收");
    expect(actionLabelForKind("browser", "resume")).toBe("重新打开");
  });

  test("20 个资源默认仅渲染活动目录单行，历史与挂起资源折叠", () => {
    const resources = Array.from({ length: 20 }, (_, offset) => {
      const index = offset + 1;
      if (index > 15) {
        return resource(index, { status: "closed", metadata: { title: `历史页面 ${index}` } });
      }
      if (index > 12) {
        return resource(index, { metadata: { title: `挂起页面 ${index}`, resource_state: "frozen" } });
      }
      return resource(index);
    });
    const html = renderToStaticMarkup(
      <WarmConfirmProvider>
        <ResourcePanel
          resources={resources}
          loading={false}
          error={null}
          loadedAt="2026-07-26T03:00:00Z"
          sessionId="ses_resource_tree"
          workspaceId="workspace_test"
          activePreviewPath="browser://resource_01_full_identifier"
          goal={{
            goal_id: "goal_resource_panel",
            session_id: "ses_resource_tree",
            objective: "统一放到右侧管理",
            status: "active",
            token_budget: 1000,
            tokens_used: 200,
            time_used_seconds: 12,
            created_at: "2026-07-26T00:00:00Z",
            updated_at: "2026-07-26T00:01:00Z",
          }}
          goalLoading={false}
          goalError={null}
          onRefresh={() => {}}
          onRefreshGoal={async () => null}
          onUpdateGoal={async () => {
            throw new Error("静态渲染不调用 onUpdateGoal");
          }}
          onClearGoal={async () => {
            throw new Error("静态渲染不调用 onClearGoal");
          }}
          onControl={async () => {}}
          onOpenTerminalPreview={() => {}}
          onOpenBrowserPreview={() => {}}
          onCloseResourcePreview={async () => {}}
          onShowConversation={() => {}}
          onCreateConnection={async () => {}}
        />
      </WarmConfirmProvider>,
    );

    expect(html).toContain("当前 Goal");
    expect(html).toContain("统一放到右侧管理");
    expect(html).toContain("后台连接 <span class=\"resource-total-count\">20</span>");
    expect(html).toContain("后台可用");
    expect(html).toContain("已挂起 / 可恢复");
    expect(html).toContain("历史记录");
    expect(html).toContain("页面 12");
    expect(html).not.toContain("挂起页面 13");
    expect(html).not.toContain("历史页面 16");
    expect(html).not.toContain("resource_01_full_identifier");
    expect(html).not.toContain("resource-card");
    expect(html).toContain("resource-tree-item is-selected");
    expect(html).toContain(">当前</span>");
    expect((html.match(/resource-tree-item/g) ?? []).length).toBe(12);
  });
});
