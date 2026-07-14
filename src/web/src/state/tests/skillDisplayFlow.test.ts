import type { TraceEvent } from "../../types/backend";
import type { ConversationView } from "../../types/frontend";
import { buildTraceTimelineItems } from "../chatTimeline";
import type { TimelineItem } from "../timelineTypes";
import { isSkillInternalToolItem } from "../toolDisplay";

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) {
    throw new Error(message);
  }
}

function event(
  index: number,
  type: TraceEvent["type"],
  payload: Record<string, unknown>,
  partId: string | null = null,
): TraceEvent {
  return {
    event_id: `evt_${index}`,
    part_id: partId,
    session_id: "ses_skill_display",
    job_id: "job_skill_display",
    type,
    phase: type.startsWith("tool_") ? "tool" : "text",
    title: type,
    content: "",
    timestamp: `2026-07-06T00:00:0${index}.000Z`,
    payload,
    raw: { payload },
  };
}

function aggregatedTools(items: TimelineItem[]) {
  return items.filter(
    (item): item is Extract<TimelineItem, { kind: "aggregated_tool" }> =>
      item.kind === "aggregated_tool",
  );
}

const skillEvents: TraceEvent[] = [
  event(1, "text_start", { kind: "reasoning" }, "reasoning_1"),
  event(2, "text_delta", {
    text: "我先查看工作区扩展工具索引。",
    kind: "reasoning",
  }, "reasoning_1"),
  event(3, "text_end", { kind: "reasoning", text: "我先查看工作区扩展工具索引。" }, "reasoning_1"),
  event(4, "tool_call_start", {
    tool_name: "glob",
    args: { pattern: "**/AGENTS.md" },
  }, "tool_glob"),
  event(5, "tool_call_end", {
    tool_name: "glob",
    result: "['/.boxteam/AGENTS.md', '/.boxteam/skills/test-tool-2/AGENTS.md', '/AGENTS.md']",
  }, "tool_glob"),
  event(6, "text_start", { kind: "reasoning" }, "reasoning_2"),
  event(7, "text_delta", {
    text: "我已经找到 skill 目录，接着读取具体说明。",
    kind: "reasoning",
  }, "reasoning_2"),
  event(8, "text_end", { kind: "reasoning", text: "我已经找到 skill 目录，接着读取具体说明。" }, "reasoning_2"),
  event(9, "tool_call_start", {
    tool_name: "read_file",
    args: { file_path: "/.boxteam/AGENTS.md" },
  }, "tool_read_agents"),
  event(10, "tool_call_end", {
    tool_name: "read_file",
    result: "当用户要求调用 `test_tool_2` 时，读取 `/.boxteam/skills/test-tool-2/SKILL.md`。",
  }, "tool_read_agents"),
  event(11, "tool_call_start", {
    tool_name: "read_file",
    args: { file_path: "/.boxteam/skills/test-tool-2/SKILL.md" },
  }, "tool_read_skill"),
  event(12, "tool_call_end", {
    tool_name: "read_file",
    result: "---\nname: test-tool-2\nallowed-tools: test_tool_2\n---",
  }, "tool_read_skill"),
  event(13, "tool_call_start", {
    tool_name: "test_tool_2",
    args: {},
    skill_names: ["test-tool-2", "test-tool-2"],
    invocation_tool_name: "invoke_custom_tool",
  }, "run_test_tool_2"),
  event(14, "tool_call_end", {
    tool_name: "test_tool_2",
    result: "4568",
    skill_names: ["test-tool-2"],
    invocation_tool_name: "invoke_custom_tool",
  }, "run_test_tool_2"),
  event(15, "text_start", { kind: "markdown" }, "final_1"),
  event(16, "text_delta", { kind: "markdown", text: "4568" }, "final_1"),
  event(17, "text_end", { kind: "markdown", text: "4568" }, "final_1"),
  event(18, "agent_end", { final_text: "4568" }),
];

const conversation: ConversationView = {
  conversationId: "conv_skill_display",
  sessionId: "ses_skill_display",
  userMessage: null,
  events: skillEvents,
  status: "done",
  jobId: "job_skill_display",
  pending: false,
  source: "messages",
};

const items = buildTraceTimelineItems([conversation]);
const summary = items.find(
  (item): item is Extract<TimelineItem, { kind: "skill_summary" }> =>
    item.kind === "skill_summary",
);
assert(summary, "默认视图时间线缺少 Skill 执行验证摘要");
assert(summary.readSkills.includes("test-tool-2"), "摘要未记录已读取的 skill");
assert(summary.finalText === "4568", "摘要最终文本应等于扩展工具返回值");
assert(
  summary.toolResults.some(
    (result) =>
      result.toolName === "test_tool_2" &&
      result.invocationToolName === "invoke_custom_tool" &&
      result.resultText === "4568" &&
      result.skillNames.includes("test-tool-2"),
  ),
  "摘要未记录 test_tool_2 的 skill 来源和返回值",
);

const finalText = items.find(
  (item): item is Extract<TimelineItem, { kind: "aggregated_text" }> =>
    item.kind === "aggregated_text" && item.partKind === "markdown",
);
assert(finalText?.text === "4568", "默认视图缺少最终回复文本 4568");

const toolItems = aggregatedTools(items);
assert(
  toolItems.length === 4,
  "测试事件应聚合出 glob、两个 read_file 和 test_tool_2 四个工具项",
);
assert(
  toolItems.some((item) => item.toolName === "glob"),
  "完整时间线应保留 glob 工具，供事件/请求视图使用",
);
assert(
  toolItems.some(isSkillInternalToolItem),
  "skill 内部工具项应被识别，供默认视图过滤",
);

assert(
  items.some((item) => item.kind === "skill_summary"),
  "默认可见项中应保留 Skill 执行验证摘要",
);
assert(
  items.some(
    (item) => item.kind === "aggregated_text" && item.text === "4568",
  ),
  "默认可见项中应保留最终回复",
);
assert(
  items.filter((item) => item.kind === "aggregated_tool").length === 4,
  "默认视图应保留工具调用卡片，由折叠状态控制密度，而不是隐藏卡片",
);
assert(
  items.some(
    (item) => item.kind === "aggregated_text" && item.partKind === "reasoning",
  ),
  "默认视图应保留推理卡片，由折叠状态控制密度，而不是隐藏卡片",
);
assert(
  summary.toolResults.every((result) => result.skillNames.length === 1),
  "Skill 摘要中的 skill 名称应去重",
);
