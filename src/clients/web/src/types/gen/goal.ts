// 该文件由程序生成，请勿手写。
/* tslint:disable */
/* eslint-disable */
/**
/* This file was automatically generated from pydantic models by running pydantic2ts.
/* Do not modify it by hand - just update the pydantic models and then re-run the script
*/

export type GoalStatus = "active" | "paused" | "blocked" | "usage_limited" | "budget_limited" | "complete";

export interface GoalJobAccountingDTO {
  tokens?: number;
  elapsed_seconds?: number;
  time_closed?: boolean;
}
export interface SessionGoalClearResultDTO {
  session_id: string;
  cleared: boolean;
}
export interface SessionGoalDTO {
  goal_id: string;
  session_id: string;
  objective: string;
  status: GoalStatus;
  token_budget?: number | null;
  tokens_used?: number;
  time_used_seconds?: number;
  revision?: number;
  created_at: string;
  updated_at: string;
  last_accounted_job_id?: string | null;
  last_continued_job_id?: string | null;
  accounted_jobs?: {
    [k: string]: GoalJobAccountingDTO;
  };
}
export interface SessionGoalSetRequest {
  objective?: string | null;
  status?: GoalStatus | null;
  token_budget?: number | null;
  replace?: boolean;
}
