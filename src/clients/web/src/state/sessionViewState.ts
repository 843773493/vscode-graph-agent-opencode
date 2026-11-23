import type { ReadingAnchor, DisclosureOverrides } from "./timeline/transcriptNodes";
/** 纯展示数据由当前浏览器保存，不携带 Gateway 用户或租约身份。 */
export interface SessionViewState {
  gateway_id: string;
  workspace_id: string;
  session_id: string;
  thread_id: string;
  reading_anchor: ReadingAnchor | null;
  disclosures: DisclosureOverrides;
  follow_latest: boolean;
  projection_version: number;
  tool_details_expanded: boolean;
  updated_at: string;
}

export type SessionSelection = Pick<SessionViewState, "gateway_id" | "workspace_id" | "session_id">;
