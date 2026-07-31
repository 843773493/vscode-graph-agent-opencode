import {
  validateSessionExecutionSseDTO,
  validateSseErrorDTO,
  validateTraceEventDTO,
  validateWorkspaceFileChangeBatchDTO,
} from './sseRuntimeValidators.js';

function formatErrors(errors) {
  if (!errors?.length) {
    return '未知字段错误';
  }
  return errors
    .map((error) => `${error.instancePath || '/'} ${error.message ?? error.keyword}`)
    .join('; ');
}

function validateGenerated(name, validate, value) {
  if (!validate(value)) {
    throw new Error(`${name} 校验失败: ${formatErrors(validate.errors)}`);
  }
  return value;
}

export function validateTraceEvent(value) {
  const event = validateGenerated('TraceEventDTO', validateTraceEventDTO, value);
  if (Number.isNaN(Date.parse(event.timestamp))) {
    throw new Error('TraceEventDTO 校验失败: /timestamp 不是有效日期时间');
  }
  return event;
}

export function validateSessionExecutionSse(value) {
  const envelope = validateGenerated(
    'SessionExecutionSseDTO',
    validateSessionExecutionSseDTO,
    value,
  );
  if (Number.isNaN(Date.parse(envelope.event.time))) {
    throw new Error(
      'SessionExecutionSseDTO 校验失败: /event/time 不是有效日期时间',
    );
  }
  return envelope;
}

export function validateWorkspaceFileChangeBatch(value) {
  return validateGenerated(
    'WorkspaceFileChangeBatchDTO',
    validateWorkspaceFileChangeBatchDTO,
    value,
  );
}

export function validateSseError(value) {
  return validateGenerated('SseErrorDTO', validateSseErrorDTO, value);
}
