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
    description: "显示用户消息和最终回复，内部细节请切换事件、请求或上下文状态视图",
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
    id: "changes",
    label: "变更",
    description: "在右侧更改栏和文件预览区查看本会话可审查变更",
  },
  {
    id: "resources",
    label: "后台连接",
    description: "查看可保留、可重新打开或可连接的持久终端、浏览器页面和周期/常驻后台任务",
  },
  {
    id: "agent",
    label: "上下文状态",
    description: "按卡片查看 Agent 当前内部消息、工具调用和元数据",
  },
  // TODO: 后续添加更多视图时，在这里扩展菜单项并接入对应面板。
];

export const COMPOSER_VIEW_OPTIONS = VIEW_OPTIONS.filter(
  (option) => option.id !== "changes",
);
