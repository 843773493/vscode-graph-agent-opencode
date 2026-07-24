import { describe, expect, test } from "bun:test";
import { decideSessionResourceDrop } from "./sessionResourceDrag";

describe("会话资源树拖放决策", () => {
  test("工作区拖到工作区时建立父子工作区关系", () => {
    expect(decideSessionResourceDrop(
      {
        kind: "workspace",
        nodeId: "gwn_child",
        workspaceId: "gw_child",
        parentWorkspaceId: null,
        parentNodeId: null,
      },
      {
        kind: "workspace",
        nodeId: "gwn_parent",
        workspaceId: "gw_parent",
        navigationParentNodeId: "gwn_folder",
      },
    )).toEqual({
      allowed: true,
      action: {
        kind: "set_workspace_parent",
        parentWorkspaceId: "gw_parent",
        navigationParentNodeId: "gwn_folder",
      },
    });
  });

  test("工作区文件夹可形成多层虚拟目录", () => {
    expect(decideSessionResourceDrop(
      {
        kind: "workspace_folder",
        nodeId: "gwn_source",
        parentNodeId: null,
      },
      { kind: "workspace_folder", nodeId: "gwn_target" },
    )).toEqual({
      allowed: true,
      action: {
        kind: "move_workspace_navigation",
        parentNodeId: "gwn_target",
      },
    });
  });

  test("嵌套工作区文件夹可拖回导航根", () => {
    expect(decideSessionResourceDrop(
      {
        kind: "workspace_folder",
        nodeId: "gwn_source",
        parentNodeId: "gwn_parent",
      },
      { kind: "navigation_root" },
    )).toEqual({
      allowed: true,
      action: {
        kind: "move_workspace_navigation",
        parentNodeId: null,
      },
    });
  });

  test("会话与文件夹按目标节点移动", () => {
    expect(decideSessionResourceDrop(
      {
        kind: "session",
        nodeId: "ses_child",
        sessionId: "ses_child",
        workspaceId: "gw_a",
        parentNodeId: null,
      },
      {
        kind: "session",
        nodeId: "ses_parent",
        sessionId: "ses_parent",
        workspaceId: "gw_a",
      },
    )).toEqual({
      allowed: true,
      action: { kind: "move_catalog_node", parentNodeId: "ses_parent" },
    });
    expect(decideSessionResourceDrop(
      {
        kind: "session_folder",
        nodeId: "fld_child",
        workspaceId: "gw_a",
        parentNodeId: null,
      },
      { kind: "session_folder", nodeId: "fld_parent", workspaceId: "gw_a" },
    )).toEqual({
      allowed: true,
      action: { kind: "move_catalog_node", parentNodeId: "fld_parent" },
    });
  });

  test("拒绝把会话资源拖到其它工作区", () => {
    expect(decideSessionResourceDrop(
      {
        kind: "session",
        nodeId: "ses_a",
        sessionId: "ses_a",
        workspaceId: "gw_a",
        parentNodeId: null,
      },
      { kind: "session_folder", nodeId: "fld_b", workspaceId: "gw_b" },
    )).toEqual({
      allowed: false,
      reason: "会话和会话文件夹不能拖到其他工作区",
    });
  });
});
