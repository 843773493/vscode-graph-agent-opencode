import { describe, expect, test } from "bun:test";
import React from "react";
import { act, create, type ReactTestRenderer, type ReactTestInstance } from "react-test-renderer";
import { renderToStaticMarkup } from "react-dom/server";
import ChatTurn from "./ChatTurn";
import { TurnActivitySummary } from "./TurnActivitySummary";
import { chatTurnProps, conversation } from "./chatTurnTestFixtures";
import { TranscriptTestView } from "../../../tests/fixtures/transcriptView";
import type { TurnResponseItem } from "../../../types/backend";
import { MessageSearchLocationContext } from "../messageSearchLocation";
import type { ThreadItemLocationDTO } from "../../../types/protocol_generated/boxteam/workspace/v2/public";

function reasoning(id: string, ordinal: number): TurnResponseItem {
  return {id:id, kind:"reasoning", projection:"detail", status:"completed", text:`正文 ${id}`,
    source:{item_id:id, item_sequence:ordinal, logical_item_ordinal:ordinal, message_sequence:ordinal, }};
}
function processButton(renderer: ReactTestRenderer) {
  return renderer.root.findAllByType("button").find(node => {
    const key = node.props["data-transcript-node"];
    return key && JSON.parse(key).source_ref.kind === "turn" && JSON.parse(key).slot === "header";
  })!;
}
async function click(button: ReactTestInstance) { await act(async () => button.props.onClick()); }

describe("ChatTurn 历史的三层过程", () => {
  test("只有final且过程计数为零时，也能在折叠之外续读更早Item", async () => {
    let value: ReturnType<typeof conversation> = {...conversation("done"), activityStats:{duration_ms:100,item_count:0},
      activityOutline:undefined, historyTruncated:true};
    const calls: Parameters<NonNullable<ReturnType<typeof chatTurnProps>["onLoadTurnDetails"]>>[] = [];
    const render = () => <TranscriptTestView><ChatTurn {...chatTurnProps(value)}
      onLoadTurnDetails={async (...args) => { calls.push(args); }} /></TranscriptTestView>;
    const renderer = create(render());
    const more = () => renderer.root.findAllByType("button").find(button =>
      button.children.includes("加载更早内容"));
    expect(processButton(renderer).props.disabled).toBe(true);
    expect(more()).toBeDefined();
    await click(more()!);
    expect(calls[0]![5]).toEqual({sourceViewId:"view_fixture",sourceViewRevision:1,executionId:undefined,beforeCursor:undefined});
    expect(calls[0]![3]).toEqual(["activity_outline","assistant_text"]);
    value = {...value,activityOutline:{beforeCursor:"older-final"}};
    await act(async () => renderer.update(render()));
    await click(more()!);
    expect(calls[1]![5]?.beforeCursor).toBe("older-final");
    value = {...value,activityOutline:{beforeCursor:null}};
    await act(async () => renderer.update(render()));
    expect(more()).toBeUndefined();
    expect(JSON.stringify(renderer.toJSON())).toContain("原始回答");
    renderer.unmount();
  });

  test("摘要使用结构化用量，未报告用量不伪造零", () => {
    const value = conversation("done");
    value.activityStats = {duration_ms:16000,item_count:1};
    const renderer = create(<TranscriptTestView><TurnActivitySummary conversation={value} groups={[]} showRawDetails={false}
      usage={{modelUsage:{finalModelId:"provider/model"}, tokenUsage:{inputTokens:32000,outputTokens:843,totalTokens:32843,
        cacheReadInputTokens:12000,modelCalls:1,reportedModelCalls:1}}} /></TranscriptTestView>);
    const description = processButton(renderer).props["aria-description"] as string;
    for (const detail of ["耗时 16s","Item 1 项","输入：32,000","输出：843","缓存命中：12,000","最后一次请求模型：provider/model"]) {
      expect(description).toContain(detail);
    }
    renderer.update(<TranscriptTestView><TurnActivitySummary conversation={value} groups={[]} showRawDetails={false}
      usage={{modelUsage:{finalModelId:"provider/model"},tokenUsage:null}} /></TranscriptTestView>);
    expect(processButton(renderer).props["aria-description"]).toContain("用量未报告");
    expect(renderer.root.findAllByProps({className:"chat-usage-context"})).toHaveLength(0);
    renderer.unmount();
  });

  test("过程、连续活动组和单项逐层展开，迟到数据不重开已关闭的层", async () => {
    let value = conversation("done");
    value.responseItems = [reasoning("item_reasoning",2),...value.responseItems!];
    const render = () => <TranscriptTestView><ChatTurn {...chatTurnProps(value)} /></TranscriptTestView>;
    const renderer = create(render());
    expect(renderer.root.findAllByProps({className:"chat-activity-group"})).toHaveLength(0);
    await click(processButton(renderer));
    expect(renderer.root.findAllByProps({className:"chat-activity-item"})).toHaveLength(0);
    await click(renderer.root.findByProps({className:"chat-activity-group"}).findByType("button"));
    expect(JSON.stringify(renderer.toJSON())).not.toContain("正文 item_reasoning");
    const item = renderer.root.findByProps({className:"chat-activity-item"});
    await click(item.findByType("button"));
    expect(JSON.stringify(renderer.toJSON())).toContain("正文 item_reasoning");
    await click(item.findByType("button"));
    value = {...value,responseItems:[{...value.responseItems![0],text:"迟到的正文"},...value.responseItems!.slice(1)]};
    await act(async () => renderer.update(render()));
    expect(JSON.stringify(renderer.toJSON())).not.toContain("迟到的正文");
    renderer.unmount();
  });

  test("交错回执合行后只补读该工具的输入和结果，并保留结果独立定位", async () => {
    const value = conversation("done");
    const call: TurnResponseItem = {id:"call",kind:"tool_call",projection:"summary",status:"completed",tool_name:"read_file",tool_call_id:"tool_1",
      source:{item_id:"item_call",item_sequence:2,message_sequence:2,call_index:0,assistant_message_sequence:2}};
    const result: TurnResponseItem = {...call,id:"result",kind:"tool_result",source:{...call.source,item_id:"item_result",item_sequence:3,message_sequence:3}};
    value.responseItems = [call,reasoning("item_between",3),result,...value.responseItems!];
    value.activityOutline = undefined;
    const calls: Parameters<NonNullable<ReturnType<typeof chatTurnProps>["onLoadTurnDetails"]>>[] = [];
    const renderer = create(<TranscriptTestView><ChatTurn {...chatTurnProps(value)} onLoadTurnDetails={async (...args) => {calls.push(args);}} /></TranscriptTestView>);
    await click(processButton(renderer));
    expect(calls).toHaveLength(1);
    expect(calls[0]![3]).toEqual(["activity_outline","assistant_text"]);
    await click(renderer.root.findByProps({className:"chat-activity-group"}).findByType("button"));
    expect(calls).toHaveLength(1);
    await click(renderer.root.findByProps({className:"chat-tool-summary"}));
    expect(calls).toHaveLength(2);
    expect(calls[1]![5]).toEqual({sourceViewId:"view_fixture",sourceViewRevision:1,executionId:undefined,itemIds:["item_call","item_result"]});
    expect(calls[1]![3]).toContain("tool_result");
    const toolNodes = renderer.root.findAll(node => node.type === "div" && node.props["data-transcript-node"])
      .map(node => JSON.parse(node.props["data-transcript-node"]));
    expect(toolNodes).toContainEqual({source_ref:{kind:"item",id:"item_call"},slot:"tool_input"});
    expect(toolNodes).toContainEqual({source_ref:{kind:"item",id:"item_result"},slot:"tool_output"});
    await click(processButton(renderer));
    expect(renderer.root.findAllByProps({className:"chat-tool-summary"})).toHaveLength(0);
    renderer.unmount();
  });

  test("中间文本、压缩、活动组保留 canonical 顺序，final 始终独立", async () => {
    const value = conversation("done");
    value.responseItems![0] = {...value.responseItems![0],source:{...value.responseItems![0].source,item_id:"final"}};
    value.responseItems = [reasoning("before",2),
      {...reasoning("middle",3),kind:"text",text:"普通中间文本"},
      {...reasoning("compaction",4),kind:"compaction_summary",text:"压缩正文"},
      reasoning("after",5),...value.responseItems!];
    const renderer = create(<TranscriptTestView><ChatTurn {...chatTurnProps(value)} /></TranscriptTestView>);
    const members = JSON.parse(processButton(renderer).props["data-transcript-members"]);
    expect(members).toContainEqual({source_ref:{kind:"item",id:"compaction"},slot:"body"});
    expect(members.some((member: {source_ref:{id:string}}) => member.source_ref.id === "final")).toBe(false);
    expect(JSON.stringify(renderer.toJSON())).toContain("原始回答");
    expect(JSON.stringify(renderer.toJSON())).not.toContain("普通中间文本");
    await click(processButton(renderer));
    const kinds = renderer.root.findByProps({className:"chat-thinking-body"}).children
      .filter((node): node is Exclude<typeof node,string> => typeof node !== "string")
      .map(node => node.props.group?.kind ?? node.props.item?.partKind);
    expect(kinds).toEqual(["work","markdown","compaction","work"]);
    expect(JSON.stringify(renderer.toJSON())).toContain("普通中间文本");
    expect(JSON.stringify(renderer.toJSON())).not.toContain("正文 before");
    const compactionHeader = renderer.root.findByProps({className:"chat-compaction-summary"}).findByType("button");
    expect(JSON.parse(compactionHeader.props["data-transcript-members"])).toEqual([
      {source_ref:{kind:"item",id:"compaction"},slot:"body"}, {source_ref:{kind:"item",id:"compaction"},slot:"header"},
    ]);
    await click(renderer.root.findAllByProps({className:"chat-activity-group"})[0].findByType("button"));
    const reasoningHeader = renderer.root.findAllByProps({className:"chat-activity-item"})[0].findByType("button");
    expect(JSON.parse(reasoningHeader.props["data-transcript-members"])).toEqual([
      {source_ref:{kind:"item",id:"before"},slot:"body"}, {source_ref:{kind:"item",id:"before"},slot:"header"},
    ]);
    expect(renderer.root.findAllByProps({"aria-label":"模型回复"})).toHaveLength(2);
    renderer.unmount();
  });

  test.each(["process", "group", "reasoning", "tool", "compaction"] as const)("显式定位后手动收起 %s，迟到数据和重新挂载不重开，新定位仍可打开", async layer => {
    let value = conversation("done");
    const part: TurnResponseItem = {...reasoning("target",2),
      ...(layer === "compaction" ? {kind:"compaction_summary" as const}
        : layer === "tool" ? {kind:"tool_call" as const,tool_name:"read_file",tool_call_id:"tool-1",arguments:'{"path":"a.ts"}'} : {})};
    value.responseItems = [part,...value.responseItems!];
    const location: ThreadItemLocationDTO = {item_id:"target",semantic_kind:layer === "tool" ? "tool_call" : "assistant_output",
      turn_id:"turn_fixture",execution_id:"exec",content_hash:"hash",byte_length:12};
    let navigate!: (target: ThreadItemLocationDTO) => void;
    let closed = 0;
    let childKey = "initial";
    function Harness() {
      const [target,setTarget] = React.useState<ThreadItemLocationDTO | undefined>(location);
      navigate = setTarget;
      return <TranscriptTestView><MessageSearchLocationContext.Provider value={{item:target,
        close:() => {closed++;setTarget(undefined);},onMount:() => {}}}>
        <ChatTurn key={childKey} {...chatTurnProps(value)} />
      </MessageSearchLocationContext.Provider></TranscriptTestView>;
    }
    let renderer!: ReactTestRenderer;
    await act(async () => {renderer = create(<Harness />);});
    const header = () => layer === "process" ? processButton(renderer)
      : layer === "tool" ? renderer.root.findByProps({className:"chat-tool-summary"})
      : renderer.root.findByProps({className:layer === "group" ? "chat-activity-group"
        : layer === "compaction" ? "chat-compaction-summary" : "chat-activity-item"}).findAllByType("button")[0];
    expect(header().props["aria-expanded"]).toBe(true);
    await click(header());
    expect(closed).toBe(1);
    expect(header().props["aria-expanded"]).toBe(false);
    value = {...value,responseItems:value.responseItems!.map(entry => ({...entry,text:"迟到正文"}))};
    await act(async () => renderer.update(<Harness />));
    expect(header().props["aria-expanded"]).toBe(false);
    childKey = "remounted";
    await act(async () => renderer.update(<Harness />));
    expect(header().props["aria-expanded"]).toBe(false);
    await act(async () => navigate({...location}));
    expect(header().props["aria-expanded"]).toBe(true);
    renderer.unmount();
  });

  test("定位独立 final 不打开无关过程", async () => {
    const value = conversation("done");
    value.responseItems = [reasoning("thinking",2),{...value.responseItems![0],source:{message_sequence:3,item_id:"final",}}];
    const location: ThreadItemLocationDTO = {item_id:"final",semantic_kind:"assistant_output",turn_id:"turn_fixture",
      execution_id:"exec",content_hash:"hash",byte_length:12};
    let renderer!: ReactTestRenderer;
    await act(async () => { renderer = create(<TranscriptTestView><MessageSearchLocationContext.Provider value={{item:location,close:()=>{},onMount:()=>{}}}>
      <ChatTurn {...chatTurnProps(value)} /></MessageSearchLocationContext.Provider></TranscriptTestView>); });
    expect(processButton(renderer).props["aria-expanded"]).toBe(false);
    expect(JSON.stringify(renderer.toJSON())).toContain("原始回答");
    renderer.unmount();
  });

  test("Job／attempt 完成不收起过程，真实 Turn 终态才使用 settled 默认", async () => {
    let value = conversation("running", "job_completed");
    value.displayMode = "live";
    value.messageStream = {connectionStatus:"connected",streamStatus:"open",lastEventSeq:1,failure:null,resumable:true};
    value.responseItems = [reasoning("running_reasoning",2)];
    const render = () => <TranscriptTestView><ChatTurn {...chatTurnProps(value)} /></TranscriptTestView>;
    const renderer = create(render());
    expect(processButton(renderer).props["aria-expanded"]).toBe(true);
    value = {...value,status:"done"};
    await act(async () => renderer.update(render()));
    expect(processButton(renderer).props["aria-expanded"]).toBe(true);
    value = {...value,turnStatus:"completed"};
    await act(async () => renderer.update(render()));
    expect(processButton(renderer).props["aria-expanded"]).toBe(false);
    await click(processButton(renderer));
    await act(async () => renderer.update(render()));
    expect(processButton(renderer).props["aria-expanded"]).toBe(true);
    renderer.unmount();
  });

  test("零活动不创建空展开区，encrypted-only 仍有可展开的存在性", async () => {
    let value = conversation("done");
    value.activityStats = {duration_ms:50,item_count:0};
    const render = () => <TranscriptTestView><ChatTurn {...chatTurnProps(value)} /></TranscriptTestView>;
    const renderer = create(render());
    expect(processButton(renderer).props.disabled).toBe(true);
    expect(renderer.root.findAllByProps({className:"chat-thinking-body"})).toHaveLength(0);
    value = {...value,responseItems:[{...reasoning("encrypted",2),kind:"reasoning_encrypted",text:""}]};
    await act(async () => renderer.update(render()));
    await click(processButton(renderer));
    await click(renderer.root.findByProps({className:"chat-activity-group"}).findByType("button"));
    await click(renderer.root.findByProps({className:"chat-activity-item"}).findByType("button"));
    expect(JSON.stringify(renderer.toJSON())).toContain("已隐藏");
    renderer.unmount();
  });

  test("失败、超时、中断保留真实原因，工具失败不升级为整轮失败", () => {
    const value = conversation("done");
    value.responseItems = [{...reasoning("tool",2),kind:"tool_call",status:"failed",tool_call_id:"call",tool_name:"read_file",text:"Permission denied"}];
    const render = () => renderToStaticMarkup(<TranscriptTestView><ChatTurn {...chatTurnProps(value)} /></TranscriptTestView>);
    expect(render()).not.toContain('data-status-kind="turn-failed"');
    value.turnStatus = "failed";
    value.turnFailure = {code:"unknown_error",message:"embedding.model_ref: Field required",details:{stage:"execution"}};
    expect(render()).toContain("embedding.model_ref: Field required");
    expect(render()).toContain('data-status-kind="turn-failed"');
    value.turnStatus = "timed_out";
    expect(render()).toContain('data-status-kind="turn-timed-out"');
    value.turnStatus = "cancelled";
    value.responseItems = [{...reasoning("partial",2),kind:"text",partial:true,completion_reason:"user_interrupt"}];
    expect(render()).toContain('data-status-kind="user-interrupted"');
    expect(render()).not.toContain("重试失败轮次");
  });
});
