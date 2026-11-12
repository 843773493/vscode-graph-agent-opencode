import { t } from "../../../shared/core/i18n/index.js";
import type { CreatableSessionConnectionKind } from "../types/frontend";

export interface CreatableSessionConnectionOption {
  kind: CreatableSessionConnectionKind;
  label: string;
  description: string;
  icon: string;
}

// 新的可接管运行环境（如手机模拟器、远程电脑）在这里注册，菜单无需了解 manager 路由。
export const CREATABLE_SESSION_CONNECTION_OPTIONS: CreatableSessionConnectionOption[] = [
  {
    kind: "terminal",
    label: t("resources.create.terminal"),
    description: t("resources.create.terminalDescription"),
    icon: "codicon-terminal",
  },
  {
    kind: "browser",
    label: t("resources.create.browser"),
    description: t("resources.create.browserDescription"),
    icon: "codicon-globe",
  },
];
