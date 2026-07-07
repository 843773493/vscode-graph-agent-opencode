import type { ConversationContentView } from "../types/frontend";

export type ViewOption = {
  id: ConversationContentView;
  label: string;
  description: string;
};

export const VIEW_OPTIONS: ViewOption[] = [
  {
    id: "default",
    label: "默认视图",
    description: "显示用户消息和最终回复，调试细节请切换事件、请求或 Agent 视图",
  },
  {
    id: "events",
    label: "事件视图",
    description: "查看前端收到的当前会话事件队列",
  },
  {
    id: "requests",
    label: "请求视图",
    description: "查看当前会话历史 LLM 请求记录",
  },
  {
    id: "resources",
    label: "资源视图",
    description: "查看当前会话后台任务和快捷操作",
  },
  {
    id: "agent",
    label: "Agent 调试",
    description: "查看原始 Agent State JSONL 快照",
  },
  // TODO: 后续添加更多视图时，在这里扩展菜单项并接入对应面板。
];
