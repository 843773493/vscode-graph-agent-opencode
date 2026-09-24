import { describe, expect, test } from "bun:test";
import type { SessionCatalogNode, SessionCatalogPage } from "../../types/backend";
import {
  addCatalogOutboxIntent,
  applyCatalogOutboxReceipts,
  createCatalogOutbox,
  markCatalogOutboxOperationPersisted,
  type CatalogOutbox,
} from "./sessionCatalogOutbox";
import {
  catalogPendingNodeCount,
  projectCatalogBranch,
  projectCatalogBreadcrumb,
  projectCatalogSearch,
} from "./sessionCatalogProjection";
import type { SessionCatalogOperationReceipt } from "../../api/session/sessionCatalogOperations";

const PARTITION = { gatewayId: "local:8014", workspaceId: "workspace-1", principal: "guest" };

function opId(suffix: string): string {
  return `op_${suffix.padStart(32, "0")}`;
}

function node(overrides: Partial<SessionCatalogNode> & { node_id: string }): SessionCatalogNode {
  return {
    kind: "folder",
    name: overrides.node_id,
    parent_node_id: null,
    session_id: null,
    folder_id: null,
    has_children: false,
    storage_relative_path: null,
    created_at: null,
    updated_at: null,
    session: null,
    ...overrides,
  };
}

function page(items: SessionCatalogNode[], parentNodeId: string | null = null): SessionCatalogPage {
  return {
    revision: "rev-3",
    parent_node_id: parentNodeId,
    // 后端分页里的子节点一定带本页父节点；测试数据必须同构，否则会把「父链不符」
    // 误当成 pending 移出分支。
    items: items.map((item) => ({ ...item, parent_node_id: parentNodeId })),
    cursor: "cursor-abc",
    total: 42,
    consistency_warning: "warning-text",
  };
}

function receipt(
  operationId: string,
  overrides: Partial<SessionCatalogOperationReceipt> = {},
): SessionCatalogOperationReceipt {
  return {
    operation_id: operationId,
    client_sequence: 1,
    queue_seq: 1,
    kind: "rename_node",
    state: "queued",
    created_node_id: null,
    committed_catalog_revision: null,
    error_code: null,
    error_detail: null,
    pending_settlement: false,
    receipt_revision: 1,
    updated_at: "2026-09-24T00:00:00Z",
    ...overrides,
  };
}

function outboxWith(
  build: (outbox: CatalogOutbox) => CatalogOutbox,
): CatalogOutbox {
  return build(createCatalogOutbox(PARTITION));
}

describe("会话目录 pending 投影：改名", () => {
  test("pending 改名立即生效，但不改动后端 revision/cursor/total", () => {
    const confirmed = page([node({ node_id: "cnode_1", name: "旧名" })]);
    const outbox = outboxWith((base) => addCatalogOutboxIntent(
      base,
      opId("r"),
      { kind: "rename_node", targetNodeId: "cnode_1", name: "新名" },
      { baseCatalogRevision: 3, expectedRevision: 3 },
    ));
    const projection = projectCatalogBranch(confirmed, {
      confirmedNodes: confirmed.items,
      outbox,
    });
    expect(projection.items.map((item) => item.name)).toEqual(["新名"]);
    expect(projection.revision).toBe("rev-3");
    expect(projection.cursor).toBe("cursor-abc");
    expect(projection.total).toBe(42);
    expect(projection.consistency_warning).toBe("warning-text");
    expect(projection.pending_node_ids).toEqual(["cnode_1"]);
  });

  test("confirmed 镜像本身不得被 pending 改写", () => {
    const confirmed = page([node({ node_id: "cnode_1", name: "旧名" })]);
    const outbox = outboxWith((base) => addCatalogOutboxIntent(
      base,
      opId("r"),
      { kind: "rename_node", targetNodeId: "cnode_1", name: "新名" },
      { baseCatalogRevision: 3, expectedRevision: 3 },
    ));
    projectCatalogBranch(confirmed, { confirmedNodes: confirmed.items, outbox });
    expect(confirmed.items[0].name).toBe("旧名");
  });
});

describe("会话目录 pending 投影：移动不制造重复 node", () => {
  const sourcePage = page([node({ node_id: "cnode_1", name: "甲" })], "cnode_root");
  const targetPage = page([], "cnode_target");
  const confirmedNodes = [
    ...sourcePage.items,
    node({ node_id: "cnode_target" }),
    node({ node_id: "cnode_root" }),
  ];

  test("移动后源分支不再显示该 node，目标分支显示一次", () => {
    const outbox = outboxWith((base) => addCatalogOutboxIntent(
      base,
      opId("m"),
      { kind: "move_node", targetNodeId: "cnode_1", parentNodeId: "cnode_target" },
      { baseCatalogRevision: 3, expectedRevision: 3 },
    ));
    const source = projectCatalogBranch(sourcePage, { confirmedNodes, outbox });
    const target = projectCatalogBranch(targetPage, { confirmedNodes, outbox });
    expect(source.items.map((item) => item.node_id)).toEqual([]);
    expect(source.locally_removed_node_ids).toEqual(["cnode_1"]);
    expect(target.items.map((item) => item.node_id)).toEqual(["cnode_1"]);
    expect(target.items[0].parent_node_id).toBe("cnode_target");
    // 同一 node 只在一个分支出现，绝不重复计数。
    expect(target.items.filter((item) => item.node_id === "cnode_1").length).toBe(1);
  });

  test("pcursor/total 不被 pending 移动污染", () => {
    const outbox = outboxWith((base) => addCatalogOutboxIntent(
      base,
      opId("m"),
      { kind: "move_node", targetNodeId: "cnode_1", parentNodeId: "cnode_target" },
      { baseCatalogRevision: 3, expectedRevision: 3 },
    ));
    const target = projectCatalogBranch(targetPage, { confirmedNodes, outbox });
    expect(target.total).toBe(42);
    expect(target.cursor).toBe("cursor-abc");
    expect(target.revision).toBe("rev-3");
  });

  test("未加载目标分支时 pending 移动不进入其它分支", () => {
    const outbox = outboxWith((base) => addCatalogOutboxIntent(
      base,
      opId("m"),
      { kind: "move_node", targetNodeId: "cnode_1", parentNodeId: "cnode_unloaded" },
      { baseCatalogRevision: 3, expectedRevision: 3 },
    ));
    const source = projectCatalogBranch(sourcePage, { confirmedNodes, outbox });
    const target = projectCatalogBranch(targetPage, { confirmedNodes, outbox });
    expect(source.items.map((item) => item.node_id)).toEqual([]);
    // 目标分支尚未加载，pending 目标 node 不出现在任何已加载分支。
    expect(target.items.map((item) => item.node_id)).toEqual([]);
  });
});

describe("会话目录 pending 投影：新建 Folder 的 client_ref", () => {
  const confirmed = page([], "cnode_root");

  test("未确认 Folder 以 client_ref 出现且不与 canonical ID 重复", () => {
    const outbox = outboxWith((base) => addCatalogOutboxIntent(
      base,
      opId("f"),
      { kind: "create_folder", name: "新文件夹", parentNodeId: "cnode_root" },
      { baseCatalogRevision: 0 },
    ));
    const projection = projectCatalogBranch(confirmed, {
      confirmedNodes: confirmed.items,
      outbox,
    });
    expect(projection.items.map((item) => item.node_id)).toEqual([opId("f")]);
    expect(projection.items[0].client_ref).toBe(opId("f"));
    // 未确认阶段 node_id 只能等于 client_ref：绝不允许出现伪造的 canonical node ID。
    expect(projection.items[0].node_id).toBe(opId("f"));
    expect(projection.items[0].folder_id).toBeNull();
    expect(projection.items[0].session_id).toBeNull();
    expect(projection.items[0].pending_state).toBe("pending_local");
  });

  test("拿到 canonical ID 后 client_ref 清空，投影只保留一个 node", () => {
    let outbox = outboxWith((base) => addCatalogOutboxIntent(
      base,
      opId("f"),
      { kind: "create_folder", name: "新文件夹", parentNodeId: "cnode_root" },
      { baseCatalogRevision: 0 },
    ));
    outbox = markCatalogOutboxOperationPersisted(outbox, opId("f"));
    outbox = applyCatalogOutboxReceipts(outbox, [receipt(opId("f"), {
      kind: "create_folder",
      created_node_id: "cnode_new",
    })]);
    const projection = projectCatalogBranch(confirmed, {
      confirmedNodes: confirmed.items,
      outbox,
    });
    expect(projection.items.map((item) => item.node_id)).toEqual(["cnode_new"]);
    expect(projection.items[0].client_ref).toBeNull();
  });

  test("新建 Folder 已被后端提交进 confirmed 时不重复投影", () => {
    let outbox = outboxWith((base) => addCatalogOutboxIntent(
      base,
      opId("f"),
      { kind: "create_folder", name: "新文件夹", parentNodeId: "cnode_root" },
      { baseCatalogRevision: 0 },
    ));
    outbox = applyCatalogOutboxReceipts(outbox, [receipt(opId("f"), {
      kind: "create_folder",
      created_node_id: "cnode_new",
    })]);
    const committed = page([
      node({ node_id: "cnode_new", name: "新文件夹", parent_node_id: "cnode_root" }),
    ], "cnode_root");
    const projection = projectCatalogBranch(committed, {
      confirmedNodes: committed.items,
      outbox,
    });
    expect(projection.items.map((item) => item.node_id)).toEqual(["cnode_new"]);
    expect(projection.items.length).toBe(1);
  });

  test("移动 Session 进未确认 Folder 用 created_by_operation_id 解析", () => {
    const rootPage = page([node({ node_id: "ses_1", name: "会话" })], "cnode_root");
    const confirmedNodes = [...rootPage.items];
    let outbox = outboxWith((base) => addCatalogOutboxIntent(
      base,
      opId("f"),
      { kind: "create_folder", name: "F", parentNodeId: "cnode_root" },
      { baseCatalogRevision: 0 },
    ));
    outbox = addCatalogOutboxIntent(
      outbox,
      opId("m"),
      { kind: "move_node", targetNodeId: "ses_1", parentCreatedByOperationId: opId("f") },
      { baseCatalogRevision: 0, expectedRevision: 1 },
    );
    const source = projectCatalogBranch(rootPage, { confirmedNodes, outbox });
    // 源分支：session 已被 pending 移走，只留 pending Folder。
    expect(source.items.map((item) => item.node_id)).toEqual([opId("f")]);
    const folderPage = page([], opId("f"));
    const target = projectCatalogBranch(folderPage, { confirmedNodes, outbox });
    expect(target.items.map((item) => item.node_id)).toEqual(["ses_1"]);
    expect(target.items[0].parent_node_id).toBe(opId("f"));
  });
});

describe("会话目录 pending 投影：删除", () => {
  test("pending 删除 Folder 时其逻辑后代一并从投影消失", () => {
    const rootPage = page([node({ node_id: "cnode_f" })], "cnode_root");
    const childPage = page([node({ node_id: "ses_child", kind: "session", session_id: "ses_child" })], "cnode_f");
    const confirmedNodes = [
      ...rootPage.items,
      ...childPage.items,
      node({ node_id: "cnode_root" }),
    ];
    const outbox = outboxWith((base) => addCatalogOutboxIntent(
      base,
      opId("d"),
      { kind: "delete_folder", targetNodeId: "cnode_f", recursive: true },
      { baseCatalogRevision: 3 },
    ));
    const root = projectCatalogBranch(rootPage, { confirmedNodes, outbox });
    const child = projectCatalogBranch(childPage, { confirmedNodes, outbox });
    expect(root.items.map((item) => item.node_id)).toEqual([]);
    expect(child.items.map((item) => item.node_id)).toEqual([]);
    expect(catalogPendingNodeCount({ confirmedNodes, outbox })).toBe(0);
  });

  test("pending 删除会话只隐藏该会话，不关闭同层其它节点", () => {
    const rootPage = page([
      node({ node_id: "ses_a", kind: "session", session_id: "ses_a" }),
      node({ node_id: "ses_b", kind: "session", session_id: "ses_b" }),
    ], "cnode_root");
    const outbox = outboxWith((base) => addCatalogOutboxIntent(
      base,
      opId("d"),
      { kind: "delete_session", targetNodeId: "ses_a" },
      { baseCatalogRevision: 3 },
    ));
    const projection = projectCatalogBranch(rootPage, {
      confirmedNodes: rootPage.items,
      outbox,
    });
    expect(projection.items.map((item) => item.node_id)).toEqual(["ses_b"]);
  });
});

describe("会话目录 pending 投影：面包屑与搜索", () => {
  test("面包屑按 pending 父链重建，pending 新建 Folder 立即生效", () => {
    let outbox = outboxWith((base) => addCatalogOutboxIntent(
      base,
      opId("f"),
      { kind: "create_folder", name: "F", parentNodeId: "cnode_root" },
      { baseCatalogRevision: 0 },
    ));
    outbox = addCatalogOutboxIntent(
      outbox,
      opId("m"),
      { kind: "move_node", targetNodeId: "ses_1", parentCreatedByOperationId: opId("f") },
      { baseCatalogRevision: 0, expectedRevision: 1 },
    );
    const confirmedNodes = [
      node({ node_id: "cnode_root" }),
      node({ node_id: "ses_1", kind: "session", session_id: "ses_1", parent_node_id: "cnode_root" }),
    ];
    const breadcrumb = projectCatalogBreadcrumb(
      { revision: "rev-3", items: confirmedNodes },
      { confirmedNodes, outbox },
      "ses_1",
    );
    expect(breadcrumb.items.map((item) => item.node_id)).toEqual([
      "cnode_root",
      opId("f"),
      "ses_1",
    ]);
    // ses_1 的父链来自本地 pending 移动，因此它同样是 pending 展示项。
    expect(breadcrumb.pending_node_ids).toEqual([opId("f"), "ses_1"]);
  });

  test("缺失 node 时面包屑回落到最近有效祖先而不是悬空父链", () => {
    const confirmedNodes = [node({ node_id: "cnode_root" })];
    const outbox = createCatalogOutbox(PARTITION);
    const breadcrumb = projectCatalogBreadcrumb(
      { revision: "rev-3", items: confirmedNodes },
      { confirmedNodes, outbox },
      "cnode_missing_child",
    );
    expect(breadcrumb.items.map((item) => item.node_id)).toEqual(["cnode_root"]);
  });

  test("搜索结果只把 pending 命中作为本地投影，不改后端 cursor/total", () => {
    let outbox = outboxWith((base) => addCatalogOutboxIntent(
      base,
      opId("f"),
      { kind: "create_folder", name: "季度报告", parentNodeId: null },
      { baseCatalogRevision: 0 },
    ));
    outbox = addCatalogOutboxIntent(
      outbox,
      opId("f2"),
      { kind: "create_folder", name: "无关目录", parentNodeId: null },
      { baseCatalogRevision: 0 },
    );
    const search = projectCatalogSearch(
      { revision: "rev-3", cursor: "cursor-abc", total: 5 },
      { confirmedNodes: [], outbox },
      "报告",
    );
    expect(search.items.map((item) => item.name)).toEqual(["季度报告"]);
    expect(search.total).toBe(5);
    expect(search.cursor).toBe("cursor-abc");
    expect(search.revision).toBe("rev-3");
    expect(search.pending_node_ids).toEqual([opId("f")]);
  });

  test("空搜索关键字必须响亮失败", () => {
    expect(() => projectCatalogSearch(
      { revision: "rev-3", cursor: null, total: 0 },
      { confirmedNodes: [], outbox: createCatalogOutbox(PARTITION) },
      "   ",
    )).toThrow("会话目录搜索关键字不能为空");
  });
});

describe("会话目录投影的契约守卫", () => {
  test("confirmed 镜像出现重复 node_id 时必须响亮失败", () => {
    const duplicated = page([node({ node_id: "dup" }), node({ node_id: "dup" })]);
    expect(() => projectCatalogBranch(duplicated, {
      confirmedNodes: duplicated.items,
      outbox: createCatalogOutbox(PARTITION),
    })).toThrow("confirmed 镜像出现重复 node_id: dup");
  });

  test("pending 造成祖先环时投影必须响亮失败", () => {
    const confirmedNodes = [
      node({ node_id: "a", parent_node_id: "b" }),
      node({ node_id: "b", parent_node_id: "a" }),
    ];
    expect(() => projectCatalogBranch(page([], null), {
      confirmedNodes,
      outbox: createCatalogOutbox(PARTITION),
    })).toThrow("pending 投影包含祖先环");
  });
});
