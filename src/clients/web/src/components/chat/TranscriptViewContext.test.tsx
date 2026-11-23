import { expect, mock, test } from "bun:test";
import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { useDisclosureFocus, useTranscriptView, type TranscriptView } from "./TranscriptViewContext";
import type { SessionViewState } from "../../state/sessionViewState";

const saved: SessionViewState = {
  gateway_id: "gateway", workspace_id: "workspace", session_id: "session", thread_id: "thread",
  projection_version: 2, updated_at: "2026-11-23T00:00:00Z", tool_details_expanded: false,
  reading_anchor: null, follow_latest: false, disclosures: { group: true },
};

test("自动收起只收回被隐藏子树的焦点，不抢 Composer 焦点", () => {
  const child = {}, composer = {};
  const tree = {activeElement:composer};
  const focus = mock(() => {});
  const header = {focus};
  const root = {getRootNode:()=>tree,contains:(node:object)=>node===child};
  function Disclosure({open}:{open:boolean}) {
    const refs = useDisclosureFocus(open);
    return <section ref={refs.root}><button ref={refs.header} />{open ? <div /> : null}</section>;
  }
  let renderer!:ReactTestRenderer;
  act(()=>{renderer=create(<Disclosure open />, {createNodeMock:element=>element.type==="section" ? root : header});});
  try {
    act(()=>renderer.update(<Disclosure open={false} />));
    expect(focus).not.toHaveBeenCalled();
    act(()=>renderer.update(<Disclosure open />));
    tree.activeElement=child;
    act(()=>renderer.update(<Disclosure open={false} />));
    expect(focus).toHaveBeenCalledTimes(1);
    expect(focus).toHaveBeenCalledWith({preventScroll:true});
  } finally {act(()=>renderer.unmount());}
});

test("首次异步恢复折叠；同 Thread 两编辑器随后独立，不被持久默认值拉动", () => {
  const views = new Map<string, TranscriptView>();
  function Editor({ id, state }: { id: string; state: SessionViewState | null }) {
    views.set(id, useTranscriptView("thread", state).transcriptView);
    return null;
  }
  const pair = (state: SessionViewState | null) => <><Editor id="A" state={state} /><Editor id="B" state={state} /></>;
  let renderer!: ReactTestRenderer;
  act(() => { renderer = create(pair(null)); });
  try {
    act(() => renderer.update(pair(saved)));
    expect(views.get("A")!.disclosures.group).toBe(true);
    expect(views.get("B")!.disclosures.group).toBe(true);
    act(() => views.get("A")!.setDisclosure("group", false, false));
    expect(views.get("A")!.disclosures.group).toBeUndefined();
    expect(views.get("B")!.disclosures.group).toBe(true);
    act(() => renderer.update(pair({ ...saved, disclosures: { different: true } })));
    expect(views.get("A")!.disclosures).toEqual({});
    expect(views.get("B")!.disclosures).toEqual({ group: true });
  } finally { act(() => renderer.unmount()); }
});
