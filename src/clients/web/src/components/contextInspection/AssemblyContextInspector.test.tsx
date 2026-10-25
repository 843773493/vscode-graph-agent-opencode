import { afterEach, expect, spyOn, test } from "bun:test";
import React from "react";
import { act, create, type ReactTestInstance, type ReactTestRenderer } from "react-test-renderer";

import * as sessionContext from "../../api/session/sessionContext";
import type { SessionContextReadResultDTO } from "../../types/protocol_generated/boxteam/workspace/v2/public";
import { JSON_DISPLAY_TEXT_LIMIT } from "../../utils/jsonDisplay";
import type { ContextItem } from "../../state/contextInspection/pagination";
import AssemblyContextInspector from "./AssemblyContextInspector";
import type { InspectionOwner } from "../../hooks/contextInspection/useContextInspection";

interface PendingRead {
  session: InspectionOwner["session"];
  view: string;
  assemblyRef?: string | null;
  resolve: (page: SessionContextReadResultDTO) => void;
}

const owner: InspectionOwner = {
  port: 49123,
  gatewayWorkspaceId: "gateway-workspace",
  session: {
    session_id: "session",
    resource_identity: "session-identity",
    uri: "boxteam://workspace/backend-workspace/resources/session/session",
    scope: "workspace",
  } as InspectionOwner["session"],
  active: true,
};

let renderer: ReactTestRenderer | undefined;
let restoreReader = () => {};

afterEach(() => {
  act(() => renderer?.unmount());
  renderer = undefined;
  restoreReader();
});

function controlledReader(): PendingRead[] {
  const pending: PendingRead[] = [];
  const reader = spyOn(sessionContext, "readSessionContext").mockImplementation(
    (_port, _workspace, session, view, options) => new Promise((resolve) => {
      pending.push({
        session,
        view,
        assemblyRef: options.assemblyRef,
        resolve,
      });
    }),
  );
  restoreReader = () => reader.mockRestore();
  return pending;
}

function makePage(
  session: InspectionOwner["session"],
  view: string,
  items: ContextItem[],
  assemblyRef?: string,
): SessionContextReadResultDTO {
  if (!session) throw new Error("测试 Session 缺失");
  return {
    resource_identity: session.resource_identity,
    uri: session.uri,
    scope: session.scope,
    view,
    assembly_ref: assemblyRef,
    revision: "revision-1",
    items,
    partial_errors: [],
    has_more: false,
  };
}

function assemblyItem(session: InspectionOwner["session"]): ContextItem {
  if (!session) throw new Error("测试 Session 缺失");
  return {
    kind: "assembly",
    resource_identity: session.resource_identity,
    uri: session.uri,
    scope: session.scope,
    assembly_ref: "api-assembly-ref",
    data: { assembly_id: "assembly-1" },
    tool_summary: [],
    tool_calls: [],
    tool_results: [],
    resource_refs: [],
  };
}

function selectionItem(session: InspectionOwner["session"], payload: string): ContextItem {
  if (!session) throw new Error("测试 Session 缺失");
  return {
    kind: "selection",
    resource_identity: session.resource_identity,
    uri: session.uri,
    scope: session.scope,
    assembly_ref: "api-assembly-ref",
    data: {
      plan_ordinal: 1,
      ref: { ref_id: "ref-1", ref_type: "resource" },
      included: true,
      selection_kind: "resource",
      visibility: "visible",
      protection: "none",
      availability: "available",
      payload,
    },
    tool_summary: [],
    tool_calls: [],
    tool_results: [],
    resource_refs: [],
  };
}

function detailsBySummary(renderer: ReactTestRenderer, summary: string): ReactTestInstance {
  const details = renderer.root.findAllByType("details").find((candidate) =>
    candidate.findAllByType("summary")[0]?.children.join("") === summary,
  );
  if (!details) throw new Error(`未找到上下文详情: ${summary}`);
  return details;
}

function openDetails(details: ReactTestInstance): void {
  setDetailsOpen(details, true);
}

function setDetailsOpen(details: ReactTestInstance, open: boolean): void {
  const onClick = details.findAllByType("summary")[0]?.props.onClick;
  if (typeof onClick !== "function") throw new Error("上下文详情缺少展开处理器");
  if (Boolean(details.props.open) === open) return;
  let defaultPrevented = false;
  const event = { preventDefault: () => { defaultPrevented = true; } } as unknown as React.MouseEvent<HTMLElement>;
  act(() => onClick(event));
  expect(defaultPrevented).toBe(true);
}

test("冻结上下文 JSON 折叠时不格式化，展开后使用有界展示", async () => {
  const pending = controlledReader();
  const oversizedPayload = "x".repeat(500_000);

  await act(async () => {
    renderer = create(<AssemblyContextInspector {...owner} />);
  });
  expect(pending).toHaveLength(1);

  await act(async () => {
    pending[0]!.resolve(makePage(owner.session, "assemblies", [assemblyItem(owner.session)]));
  });

  const assemblySelect = renderer!.root.findByType("select");
  const onChange = assemblySelect.props.onChange as (event: { target: { value: string } }) => void;
  act(() => onChange({ target: { value: "api-assembly-ref" } }));
  expect(pending).toHaveLength(2);

  await act(async () => {
    pending[1]!.resolve(makePage(
      owner.session,
      "assembly",
      [selectionItem(owner.session, oversizedPayload)],
      "api-assembly-ref",
    ));
  });

  const selectionDetails = detailsBySummary(renderer!, "来源与 selection manifest");
  expect(selectionDetails.findAllByType("pre")).toHaveLength(0);

  openDetails(selectionDetails);
  const formatted = selectionDetails.findByType("pre").children.join("");
  expect(formatted).toContain("已截断展示");
  expect(formatted.length).toBeLessThanOrEqual(JSON_DISPLAY_TEXT_LIMIT);
  expect(formatted).not.toContain(oversizedPayload);
});

test("冻结上下文长正文默认只渲染预览，全文可收起并重新展开", async () => {
  const pending = controlledReader();
  const fullText = "😀正文".repeat(2_000);
  const message: ContextItem = {
    kind: "message",
    resource_identity: owner.session!.resource_identity,
    uri: owner.session!.uri,
    scope: owner.session!.scope,
    role: "user",
    data: { message_id: "message-1" },
    text: fullText,
    tool_summary: [],
    tool_calls: [],
    tool_results: [],
    resource_refs: [],
  };

  await act(async () => {
    renderer = create(<AssemblyContextInspector {...owner} />);
  });
  await act(async () => {
    pending[0]!.resolve(makePage(owner.session, "assemblies", [assemblyItem(owner.session)]));
  });

  const assemblySelect = renderer!.root.findByType("select");
  const onChange = assemblySelect.props.onChange as (event: { target: { value: string } }) => void;
  act(() => onChange({ target: { value: "api-assembly-ref" } }));
  await act(async () => {
    pending[1]!.resolve(makePage(owner.session, "assembly", [message], "api-assembly-ref"));
  });

  const messageRow = renderer!.root.findByProps({ "data-message-id": "message-1" });
  const collapsedDetails = messageRow.findByType("details");
  expect(messageRow.findAllByType("p").map((paragraph) => paragraph.children.join(""))).toEqual([
    `${[...fullText].slice(0, 2_000).join("")}…`,
  ]);

  setDetailsOpen(collapsedDetails, true);
  expect(messageRow.findAllByType("p").map((paragraph) => paragraph.children.join(""))).toContain(fullText);

  setDetailsOpen(messageRow.findByType("details"), false);
  expect(messageRow.findAllByType("p").map((paragraph) => paragraph.children.join(""))).not.toContain(fullText);

  setDetailsOpen(messageRow.findByType("details"), true);
  expect(messageRow.findAllByType("p").map((paragraph) => paragraph.children.join(""))).toContain(fullText);
});

test("关闭 reasoning 后移除全文，重新打开时恢复预览", async () => {
  const pending = controlledReader();
  const fullReasoning = "🤖推理步骤".repeat(800);
  const message: ContextItem = {
    kind: "message",
    resource_identity: owner.session!.resource_identity,
    uri: owner.session!.uri,
    scope: owner.session!.scope,
    role: "assistant",
    data: { message_id: "message-reasoning" },
    text: "答复",
    reasoning: fullReasoning,
    tool_summary: [],
    tool_calls: [],
    tool_results: [],
    resource_refs: [],
  };

  await act(async () => {
    renderer = create(<AssemblyContextInspector {...owner} />);
  });
  await act(async () => {
    pending[0]!.resolve(makePage(owner.session, "assemblies", [assemblyItem(owner.session)]));
  });
  const assemblySelect = renderer!.root.findByType("select");
  const onChange = assemblySelect.props.onChange as (event: { target: { value: string } }) => void;
  act(() => onChange({ target: { value: "api-assembly-ref" } }));
  await act(async () => {
    pending[1]!.resolve(makePage(owner.session, "assembly", [message], "api-assembly-ref"));
  });

  const messageRow = renderer!.root.findByProps({ "data-message-id": "message-reasoning" });
  const reasoningDetails = messageRow.findByType("details");
  setDetailsOpen(reasoningDetails, true);
  const fullTextDetails = messageRow.findAllByType("details").find((details) =>
    details.findAllByType("summary")[0]?.children.join("").startsWith("查看完整 reasoning"),
  );
  if (!fullTextDetails) throw new Error("未找到 reasoning 正文详情");
  setDetailsOpen(fullTextDetails, true);
  expect(messageRow.findAllByType("p").map((paragraph) => paragraph.children.join(""))).toContain(fullReasoning);

  setDetailsOpen(reasoningDetails, false);
  expect(messageRow.findAllByType("p").map((paragraph) => paragraph.children.join(""))).not.toContain(fullReasoning);
  expect(messageRow.findAllByType("details")).toHaveLength(1);

  setDetailsOpen(reasoningDetails, true);
  expect(messageRow.findAllByType("p").map((paragraph) => paragraph.children.join(""))).not.toContain(fullReasoning);
  expect(messageRow.findAllByType("p").map((paragraph) => paragraph.children.join(""))).toContain(
    `${[...fullReasoning].slice(0, 2_000).join("")}…`,
  );
});
