import type { TraceEvent } from "../types/backend";
import type { ConversationView } from "../types/frontend";
import { buildTraceTimelineItems } from "./chatTimeline";
import type { TimelineItem } from "./timelineTypes";
import { isSkillInternalToolItem } from "./toolDisplay";

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) {
    throw new Error(message);
  }
}

function event(
  index: number,
  type: TraceEvent["type"],
  payload: Record<string, unknown>,
): TraceEvent {
  return {
    event_id: `evt_${index}`,
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
  event(1, "tool_call_start", {
    tool_name: "read_file",
    args: { file_path: "/.boxteam/skills/test-tool-skill/SKILL.md" },
  }),
  event(2, "tool_call_end", {
    tool_name: "read_file",
    result: "---\nname: test-tool-skill\nallowed-tools: test_tool_2\n---",
  }),
  event(3, "tool_call_start", {
    tool_name: "test_tool_2",
    args: {},
    skill_names: ["test-tool-skill"],
  }),
  event(4, "tool_call_end", {
    tool_name: "test_tool_2",
    result: "4568",
    skill_names: ["test-tool-skill"],
  }),
  event(5, "text_end", { text: "4568" }),
  event(6, "agent_end", { final_text: "4568" }),
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
assert(summary.readSkills.includes("test-tool-skill"), "摘要未记录已读取的 skill");
assert(summary.finalText === "4568", "摘要最终文本应等于隐藏工具返回值");
assert(
  summary.toolResults.some(
    (result) =>
      result.toolName === "test_tool_2" &&
      result.resultText === "4568" &&
      result.skillNames.includes("test-tool-skill"),
  ),
  "摘要未记录 test_tool_2 的 skill 来源和返回值",
);

const finalText = items.find(
  (item): item is Extract<TimelineItem, { kind: "aggregated_text" }> =>
    item.kind === "aggregated_text" && item.phase !== "reasoning",
);
assert(finalText?.text === "4568", "默认视图缺少最终回复文本 4568");

const toolItems = aggregatedTools(items);
assert(toolItems.length === 2, "测试事件应聚合出 read_file 和 test_tool_2 两个工具项");
assert(
  toolItems.every(isSkillInternalToolItem),
  "skill 内部工具项应被默认消息区识别为隐藏项",
);

const defaultVisibleItems = items.filter(
  (item) => item.kind !== "aggregated_tool" || !isSkillInternalToolItem(item),
);
assert(
  defaultVisibleItems.some((item) => item.kind === "skill_summary"),
  "默认可见项中应保留 Skill 执行验证摘要",
);
assert(
  defaultVisibleItems.some(
    (item) => item.kind === "aggregated_text" && item.text === "4568",
  ),
  "默认可见项中应保留最终回复",
);
