// 该文件由程序生成，请勿手写。
/* tslint:disable */
/* eslint-disable */
/**
/* This file was automatically generated from pydantic models by running pydantic2ts.
/* Do not modify it by hand - just update the pydantic models and then re-run the script
*/

export interface SessionNetworkWaitDTO {
  id: string;
  session_id: string;
  message: string;
  restored: boolean;
  created_at: string;
  restored_at?: string | null;
}
export interface SessionObservationStateDTO {
  session_id: string;
  active_job_id?: string | null;
  is_streaming?: boolean;
  is_idle?: boolean;
}
export interface SessionStatusDTO {
  session_id: string;
  status: "idle" | "busy" | "question" | "permission" | "retry" | "offline";
  message?: string | null;
  active_job_id?: string | null;
  waiting?: SessionNetworkWaitDTO | null;
}
