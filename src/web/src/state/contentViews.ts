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
    description: "显示对话消息、推理过程和 trace 细节",
  },
  {
    id: "events",
    label: "事件视图",
    description: "查看前端收到的当前会话事件队列",
  },
  {
    id: "agent",
    label: "Agent 视图",
    description: "查看 Agent State messages 快照",
  },
  // TODO: 后续添加更多视图时，在这里扩展菜单项并接入对应面板。
];
