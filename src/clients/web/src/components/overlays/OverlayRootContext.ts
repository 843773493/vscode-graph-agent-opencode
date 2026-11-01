import { createContext } from "react";

/** 嵌入式会话的浮层与业务组件共享宿主；普通 Web 沿用 document.body。 */
export const OverlayRootContext = createContext<HTMLElement | null>(null);
