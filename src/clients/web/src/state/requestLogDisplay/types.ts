// 请求日志展示族的展示模型类型：只描述「后端日志 → 前端展示」的投影结果。

export interface RequestLogDisplayModel {
  model: string;
  responseText: string;
  phaseLabel: string;
  toolNames: string[];
  calledToolNames: string[];
  messageCount: number;
  responseMessageCount: number;
}

export interface RequestPromptComponentDisplay {
  source: string;
  label: string;
  operation: "append" | "replace";
  contentBlocks: unknown[];
  blockCount: number;
  charCount: number;
}

export interface RequestToolDefinitionDisplay {
  name: string;
  description: string;
  schemaFieldCount: number;
  definition: unknown;
}

export interface RequestReplayDisplay {
  schemaVersion: number | null;
  legacy: boolean;
  promptComponents: RequestPromptComponentDisplay[];
  tools: RequestToolDefinitionDisplay[];
  messageCount: number;
  systemPromptCharCount: number;
  toolSchemaCharCount: number;
}

export interface RequestLogKeyFlow {
  readSkills: string[];
  customInvokerNames: string[];
  customToolNames: string[];
  customToolResults: Array<{ toolName: string; invocationToolName: string; resultText: string }>;
  finalText: string;
}

export interface UpstreamAttemptDisplay {
  callType: string;
  provider: string;
  model: string;
  apiBase: string;
  request: unknown;
  response: unknown;
  error: unknown;
}
