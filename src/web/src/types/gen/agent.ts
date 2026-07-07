// 该文件由程序生成，请勿手写。
/* tslint:disable */
/* eslint-disable */
/**
/* This file was automatically generated from pydantic models by running pydantic2ts.
/* Do not modify it by hand - just update the pydantic models and then re-run the script
*/

export interface AgentDTO {
  agent_id: string;
  name: string;
  description?: string | null;
  model: string;
  tools: string[];
  capabilities: string[];
}
