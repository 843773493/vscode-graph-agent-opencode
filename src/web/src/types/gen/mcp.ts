// 该文件由程序生成，请勿手写。
/* tslint:disable */
/* eslint-disable */
/**
/* This file was automatically generated from pydantic models by running pydantic2ts.
/* Do not modify it by hand - just update the pydantic models and then re-run the script
*/

export interface McpServerDTO {
  server_id: string;
  transport: "stdio" | "streamable_http";
  enabled: boolean;
  status: "disabled" | "ready";
  tools?: McpToolDTO[];
}
export interface McpToolDTO {
  tool_id: string;
  server_id: string;
  remote_name: string;
  description: string;
}
