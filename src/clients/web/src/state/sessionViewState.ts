/** 纯展示数据由当前浏览器保存，不携带 Gateway 用户或租约身份。 */
export interface SessionViewState {
  workspace_id: string;
  session_id: string;
  turn_anchor: string | null;
  scroll_offset: number;
  follow_latest: boolean;
  projection_version: number;
  tool_details_expanded: boolean;
  updated_at: string;
}

export type SessionSelection = Pick<SessionViewState, "workspace_id" | "session_id">;
