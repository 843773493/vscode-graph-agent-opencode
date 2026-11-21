import {
  validateFederatedSearchEventDTO,
  validateSseErrorDTO,
  validateTraceEventDTO,
  validateWorkspaceFileChangeBatchDTO,
} from './sseRuntimeValidators.js';

export function validateFederatedSearchEvent(value) {
  const event = validateGenerated('FederatedSearchEventDTO', validateFederatedSearchEventDTO, value);
  if (event.page != null && event.diagnostic_logs != null) throw new Error('搜索事件不能携带两类领域页');
  const actual = [event.scope != null, event.branch != null, event.page != null || event.diagnostic_logs != null, event.summary != null].join(',');
  const expected = { scope: 'true,false,false,false', page: 'false,true,true,false',
    branch: 'false,true,false,false', end: 'false,false,false,true' };
  if (actual !== expected[event.type]) throw new Error('搜索事件类型与 payload 不符');
  const branch = event.branch?.branch;
  if (branch && ((branch.owner == null) === (branch.diagnostics == null)
    || (branch.category === 'runtime_log') !== (branch.diagnostics != null))) {
    throw new Error('搜索分支必须属于真实工作区或 diagnostics 来源');
  }
  if (event.type === 'page' && (branch.diagnostics != null) !== (event.diagnostic_logs != null)) {
    throw new Error('搜索领域页与分支 owner 不符');
  }
  return event;
}

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

/** 轻量失效通知：null 为当前范围重读，其余只包含已变化资源身份，不含业务快照。 */
export function validateResourceChanges(value) {
  if (!value || typeof value !== 'object' || !Object.hasOwn(value, 'resource_ids')
    || (value.resource_ids !== null && (!Array.isArray(value.resource_ids)
      || !value.resource_ids.every(id => typeof id === 'string' && id.length > 0)))) {
    throw new Error('资源变化通知缺少有效的 resource_ids');
  }
  return value.resource_ids;
}
