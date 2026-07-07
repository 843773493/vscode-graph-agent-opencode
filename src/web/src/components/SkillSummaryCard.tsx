import React from "react";

import type { TimelineItem } from "../state/chatTimeline";

export type SkillSummaryRenderCard = (options: {
  title: string;
  kind: "trace";
  tone: "done";
  time: string;
  summary: string;
  content: string;
  collapsedContent: string;
  raw: Record<string, unknown>;
  index: number;
  defaultOpen: boolean;
}) => React.ReactNode;

export default function SkillSummaryCard({
  item,
  renderCard,
  displayTime,
  index,
}: {
  item: Extract<TimelineItem, { kind: "skill_summary" }>;
  renderCard: SkillSummaryRenderCard;
  displayTime: (value: unknown) => string;
  index: number;
}): React.ReactNode {
  const readSkills = item.readSkills.join(", ") || "（无）";
  const toolLines = item.toolResults.map((result) => {
    const skillNames = result.skillNames.join(", ");
    const suffix = skillNames ? `（${skillNames}）` : "";
    return `- ${result.toolName}${suffix} -> ${result.resultText}`;
  });
  const content = [
    `**已读取 Skill**\n${readSkills}`,
    `**技能工具已执行**\n${toolLines.join("\n")}`,
    `**最终文本**\n${item.finalText}`,
  ].join("\n\n");

  return renderCard({
    title: "Skill 执行验证",
    kind: "trace",
    tone: "done",
    time: displayTime(item.timestamp),
    summary: "Skill 加载、工具调用和最终文本已记录",
    content,
    collapsedContent: "Skill 执行验证已折叠",
    raw: {
      readSkills: item.readSkills,
      toolResults: item.toolResults,
      finalText: item.finalText,
    },
    index,
    defaultOpen: true,
  });
}
