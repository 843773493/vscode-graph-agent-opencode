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

/**
 * decideSessionResourceDrop 的拒绝分支契约表：每条用例锁定一个拒绝理由，
 * 防止任何一条拒绝分支被改坏后静默放行成非法移动。
 */
describe("会话资源拖放拒绝契约", () => {
  const cases: Array<{
    name: string;
    source: Parameters<typeof decideSessionResourceDrop>[0];
    target: Parameters<typeof decideSessionResourceDrop>[1];
    zone?: Parameters<typeof decideSessionResourceDrop>[2];
    reason: string;
  }> = [
    {
      name: "工作区文件夹拖到工作区行中间时拒绝",
      source: { kind: "workspace_folder", nodeId: "gwn_src", parentNodeId: null },
      target: {
        kind: "workspace",
        nodeId: "gwn_ws",
        workspaceId: "gw_1",
        navigationParentNodeId: null,
        parentWorkspaceId: null,
      },
      reason: "工作区文件夹不能放入工作区",
    },
    {
      name: "工作区文件夹拖到会话节点时拒绝",
      source: { kind: "workspace_folder", nodeId: "gwn_src", parentNodeId: null },
      target: { kind: "session", nodeId: "cnode_1", sessionId: "ses_1", workspaceId: "gw_1" },
      reason: "工作区文件夹只能放入或插入工作区文件夹层级",
    },
    {
      name: "工作区文件夹拖到自身时拒绝",
      source: { kind: "workspace_folder", nodeId: "gwn_same", parentNodeId: null },
      target: { kind: "workspace_folder", nodeId: "gwn_same", parentNodeId: null },
      reason: "工作区文件夹不能放入自身",
    },
    {
      name: "工作区拖到自身时拒绝成为自己的子工作区",
      source: {
        kind: "workspace",
        nodeId: "gwn_a",
        workspaceId: "gw_a",
        parentWorkspaceId: null,
        parentNodeId: null,
      },
      target: {
        kind: "workspace",
        nodeId: "gwn_a",
        workspaceId: "gw_a",
        navigationParentNodeId: null,
        parentWorkspaceId: null,
      },
      reason: "工作区不能成为自己的子工作区",
    },
    {
      name: "工作区拖到会话节点时拒绝",
      source: {
        kind: "workspace",
        nodeId: "gwn_a",
        workspaceId: "gw_a",
        parentWorkspaceId: null,
        parentNodeId: null,
      },
      target: { kind: "session", nodeId: "cnode_1", sessionId: "ses_1", workspaceId: "gw_a" },
      reason: "工作区只能放入父工作区或工作区文件夹",
    },
    {
      name: "会话拖到导航根时拒绝",
      source: {
        kind: "session",
        nodeId: "cnode_1",
        sessionId: "ses_1",
        workspaceId: "gw_a",
        parentNodeId: null,
      },
      target: { kind: "navigation_root" },
      reason: "会话资源只能在所属工作区的会话树内移动",
    },
    {
      name: "会话拖到自身节点时拒绝",
      source: {
        kind: "session",
        nodeId: "cnode_same",
        sessionId: "ses_same",
        workspaceId: "gw_a",
        parentNodeId: null,
      },
      target: { kind: "session", nodeId: "cnode_same", sessionId: "ses_same", workspaceId: "gw_a" },
      reason: "会话资源不能放入自身",
    },
    {
      name: "会话已经位于目标文件夹下时拒绝",
      source: {
        kind: "session",
        nodeId: "cnode_child",
        sessionId: "ses_child",
        workspaceId: "gw_a",
        parentNodeId: "cnode_target",
      },
      target: { kind: "session_folder", nodeId: "cnode_target", workspaceId: "gw_a" },
      reason: "会话资源已经位于该位置",
    },
  ];

  for (const testCase of cases) {
    test(testCase.name, () => {
      expect(decideSessionResourceDrop(
        testCase.source,
        testCase.target,
        testCase.zone,
      )).toEqual({ allowed: false, reason: testCase.reason });
    });
  }
});
