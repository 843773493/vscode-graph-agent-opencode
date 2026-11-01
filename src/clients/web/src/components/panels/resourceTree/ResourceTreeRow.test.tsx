import { expect, test } from "bun:test";
import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import type { ResourceThreadRef } from "../../../api/workspaceResources";
import { workspaceResourceDisplay } from "../../../state/display/workspaceResourceDisplay";
import ResourceTreeRow from "./ResourceTreeRow";

test("工作区 Browser 展示全部跨会话关联，并解除用户指定的精确 Thread", () => {
  const source = { session_id: "ses_source", thread_id: "thr_source" };
  const other = { session_id: "ses_other", thread_id: "thr_other" };
  const detached: ResourceThreadRef[] = [];
  let renderer!: ReactTestRenderer;
  act(() => { renderer = create(<ResourceTreeRow previewContext="workspace" selected={false} busy={false}
    resource={workspaceResourceDisplay({ resource_id: "browser_shared", workspace_id: "workspace", kind: "browser",
      retention_scope: "workspace", source_thread: source, thread_associations: [source, other],
      owner_revision: 3, status: "running", reconciliation_status: "verified", available_actions: ["detach"],
      metadata: { title: "共享浏览器" }, created_at: "2026-10-30T00:00:00Z", updated_at: "2026-10-30T00:00:00Z" })}
    onControl={() => {}} onCopy={() => {}} onOpenTerminal={() => {}} onOpenBrowser={() => {}}
    onOwnerControl={(_, action, thread) => {
      expect(action).toBe("detach");
      if (!thread) throw new Error("解除关联必须携带精确 Thread");
      detached.push(thread);
    }} />); });
  try {
    act(() => renderer.root.findByProps({ className: "resource-tree-chevron" }).props.onClick());
    const button = renderer.root.findByProps({ title: `${other.session_id} / ${other.thread_id}` }).findByType("button");
    expect(button.children).toEqual(["解除关联"]);
    act(() => button.props.onClick());
    expect(detached).toEqual([other]);
  } finally { act(() => renderer.unmount()); }
});
