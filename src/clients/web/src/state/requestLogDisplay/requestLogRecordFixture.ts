import type { LLMRequestLogRecord } from "../../types/backend";

export function requestLogRecordFixture(
  overrides: Partial<LLMRequestLogRecord> = {},
): LLMRequestLogRecord {
  return {
    session_id: "ses_request_log_fixture",
    timestamp: 0,
    file_name: "request-log.json",
    request: {},
    response: {},
    upstream: {},
    ...overrides,
  };
}
