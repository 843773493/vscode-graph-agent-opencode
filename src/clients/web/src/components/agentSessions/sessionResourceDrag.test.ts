import { describe, expect, test } from "bun:test";
import {
  decideSessionResourceDrop,
  workspaceDropZoneForPointer,
} from "./sessionResourceDrag";

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
        parentWorkspaceId: null,
      },
    )).toEqual({
      allowed: true,
      action: {
        kind: "set_workspace_parent",
        parentWorkspaceId: "gw_parent",
        navigationParentNodeId: "gwn_folder",
        placement: { mode: "last" },
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
      {
        kind: "workspace_folder",
        nodeId: "gwn_target",
        parentNodeId: null,
      },
    )).toEqual({
      allowed: true,
      action: {
        kind: "move_workspace_navigation",
        parentNodeId: "gwn_target",
        placement: { mode: "last" },
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
        placement: { mode: "last" },
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

  test("工作区行按上四分之一、中间、下四分之一分配拖放区域", () => {
    expect(workspaceDropZoneForPointer(100, 100, 40)).toBe("before");
    expect(workspaceDropZoneForPointer(109, 100, 40)).toBe("before");
    expect(workspaceDropZoneForPointer(120, 100, 40)).toBe("inside");
    expect(workspaceDropZoneForPointer(131, 100, 40)).toBe("after");
    expect(workspaceDropZoneForPointer(140, 100, 40)).toBe("after");
  });

  test("工作区文件夹可插入工作区之前", () => {
    expect(decideSessionResourceDrop(
      {
        kind: "workspace_folder",
        nodeId: "gwn_source",
        parentNodeId: null,
      },
      {
        kind: "workspace",
        nodeId: "gwn_workspace",
        workspaceId: "gw_workspace",
        navigationParentNodeId: "gwn_parent",
        parentWorkspaceId: null,
      },
      "before",
    )).toEqual({
      allowed: true,
      action: {
        kind: "move_workspace_navigation",
        parentNodeId: "gwn_parent",
        placement: { mode: "before", targetNodeId: "gwn_workspace" },
      },
    });
  });

  test("工作区插入子工作区旁时继承相同父工作区", () => {
    expect(decideSessionResourceDrop(
      {
        kind: "workspace",
        nodeId: "gwn_source",
        workspaceId: "gw_source",
        parentWorkspaceId: null,
        parentNodeId: null,
      },
      {
        kind: "workspace",
        nodeId: "gwn_child",
        workspaceId: "gw_child",
        navigationParentNodeId: "gwn_folder",
        parentWorkspaceId: "gw_parent",
      },
      "after",
    )).toEqual({
      allowed: true,
      action: {
        kind: "set_workspace_parent",
        parentWorkspaceId: "gw_parent",
        navigationParentNodeId: "gwn_folder",
        placement: { mode: "after", targetNodeId: "gwn_child" },
      },
    });
  });

  test("工作区文件夹不能插入子工作区列表", () => {
    expect(decideSessionResourceDrop(
      {
        kind: "workspace_folder",
        nodeId: "gwn_source",
        parentNodeId: null,
      },
      {
        kind: "workspace",
        nodeId: "gwn_child",
        workspaceId: "gw_child",
        navigationParentNodeId: null,
        parentWorkspaceId: "gw_parent",
      },
      "before",
    )).toEqual({
      allowed: false,
      reason: "工作区文件夹不能插入子工作区列表",
    });
  });
});
