// 该文件由程序生成，请勿手写。
/* tslint:disable */
/* eslint-disable */
/**
/* This file was automatically generated from pydantic models by running pydantic2ts.
/* Do not modify it by hand - just update the pydantic models and then re-run the script
*/

export interface ToolDTO {
  tool_id: string;
  name: string;
  description?: string | null;
  parameters?: {
    [k: string]: unknown;
  };
  category?: string | null;
}
export interface ToolInvokeRequest {
  parameters?: {
    [k: string]: unknown;
  };
}
export interface ToolInvokeResultDTO {
  tool_id: string;
  status: string;
  result: string;
  parameters?: {
    [k: string]: unknown;
  };
}
