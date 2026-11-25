import type { ReadingAnchor, DisclosureOverrides } from "./timeline/transcriptNodes";
/** 当前客户端阅读格式；与 IndexedDB 版本及历史缓存格式独立。 */
export const SESSION_VIEW_PROJECTION_VERSION = 3;
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
