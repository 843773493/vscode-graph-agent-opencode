import type { CreatableSessionConnectionKind } from "../types/frontend";

export interface CreatableSessionConnectionOption {
  kind: CreatableSessionConnectionKind;
  label: string;
  description: string;
  icon: string;
}

// 新的可接管运行环境（如手机模拟器、远程电脑）在这里注册，菜单无需了解 manager 路由。
export const CREATABLE_SESSION_CONNECTIONS: CreatableSessionConnectionOption[] = [
  {
    kind: "terminal",
    label: "新建终端",
    description: "创建持久终端并立即连接",
    icon: "codicon-terminal",
  },
  {
    kind: "browser",
    label: "新建浏览器",
    description: "创建空白浏览器页面并立即连接",
    icon: "codicon-globe",
  },
];
