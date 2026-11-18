import { createContext } from "react";

/** 工作台接入现有窗口日志；独立组件环境仍将原始错误送到控制台。 */
export const ComposerDiagnosticsContext = createContext<(message: string) => void>(console.error);
