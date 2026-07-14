// 该文件由程序生成，请勿手写。
/* tslint:disable */
/* eslint-disable */
/**
/* This file was automatically generated from pydantic models by running pydantic2ts.
/* Do not modify it by hand - just update the pydantic models and then re-run the script
*/

export interface SessionResourceControlRequest {
  action: "pause" | "resume" | "cancel" | "delete";
  params?: {
    [k: string]: unknown;
  };
}
export interface SessionResourceControlResultDTO {
  session_id: string;
  resource_id: string;
  kind: "background_task" | "terminal" | "browser";
  action: "pause" | "resume" | "cancel" | "delete";
  status: string;
  resource?: SessionResourceDTO | null;
}
/**
 * 会话后台连接。
 *
 * 这里只描述可保留、可重新打开或可连接的长生命周期对象，例如持久终端、
 * 浏览器页面和持续后台任务。一次性 agent job 属于执行状态/事件流，不进入该列表。
 */
export interface SessionResourceDTO {
  resource_id: string;
  session_id: string;
  kind: "background_task" | "terminal" | "browser";
  name: string;
  status: string;
  created_at: string;
  updated_at: string;
  started_at?: string | null;
  ended_at?: string | null;
  available_actions?: ("pause" | "resume" | "cancel" | "delete")[];
  metadata?: {
    [k: string]: unknown;
  };
}
export interface SessionResourceListDTO {
  session_id: string;
  items: SessionResourceDTO[];
}
