// 该文件由程序生成，请勿手写。
/* tslint:disable */
/* eslint-disable */
/**
/* This file was automatically generated from pydantic models by running pydantic2ts.
/* Do not modify it by hand - just update the pydantic models and then re-run the script
*/

export interface LLMRequestLogRecordDTO {
  session_id: string;
  job_id?: string | null;
  timestamp: number;
  file_name: string;
  file_path: string;
  request?: {
    [k: string]: unknown;
  };
  response?: {
    [k: string]: unknown;
  };
}
