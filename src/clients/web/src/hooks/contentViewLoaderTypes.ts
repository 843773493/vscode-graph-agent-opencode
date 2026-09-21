import type { Dispatch, SetStateAction } from "react";
import type { AppState } from "../types/frontend";

export type SetAppState = Dispatch<SetStateAction<AppState>>;
export type RefreshOptions = { silent?: boolean };
// 唯一来源，与 hooks.tsx 中 finishWorkspaceRefresh 的实现签名逐字一致。
// 历史成因：useGatewayWorkspaceMutations.ts 与 useGatewayWorkspaceRuntimeLifecycle.ts
// 曾各自收窄了一份同名类型，只覆盖单参数调用；收窄副本在调用方向变化时会给出
// 错误的类型保证，因此统一收敛到本模块。
export type FinishWorkspaceRefresh = (
  preferredSessionId?: string | null,
  options?: {
    checkGatewayWorkspaceHealth?: boolean;
    reuseCurrentUiSettings?: boolean;
  },
) => Promise<boolean>;
