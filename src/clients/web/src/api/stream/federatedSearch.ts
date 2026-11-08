import type {
  FederatedSearchBranchKeyDTO,
  FederatedSearchEventDTO,
  FederatedSearchQueryDTO,
  FederatedSearchRequest,
  FederatedSearchSummaryDTO,
} from "../../types/protocol_generated/boxteam/search/v1/public";
import type { GatewayDiagnosticLogQueryResultDTO } from "../../types/protocol_generated/boxteam/gateway/v2/public";
import { consumeSseResponse, decodeJsonSseData, defineSseEvent } from "../../sse/sseClient";
import { validateFederatedSearchEvent } from "../../sse/sseRuntimeSchemas";
import { DEFAULT_API_REQUEST_TIMEOUT_MS, requestGatewayResponse } from "../http";

/** 未取得完整终态不等于搜索完成；请求身份来自原 HTTP 响应头。 */
export class IncompleteSearchStreamError extends Error {
  readonly code = "incomplete_stream";

  constructor(readonly requestId: string) {
    super("搜索连接已结束，但未收到完整终态");
    this.name = "IncompleteSearchStreamError";
  }
}

/** 真实正文读取失败保留原始原因，不与正常 EOF、取消或协议错误混用。 */
export class SearchStreamReadError extends Error {
  readonly code = "stream_read_failed";

  constructor(readonly requestId: string, readonly cause: unknown) {
    super(`搜索连接读取失败: ${cause instanceof Error ? cause.message : String(cause)}`);
    this.name = "SearchStreamReadError";
  }
}

export function searchBranchKey(branch: FederatedSearchBranchKeyDTO): string {
  if (branch.diagnostics) {
    if (branch.owner || branch.category !== "runtime_log" || !branch.diagnostics.scope) throw new Error("诊断搜索分支归属无效");
    return JSON.stringify(["diagnostics", branch.diagnostics.gateway_id, branch.diagnostics.scope.source,
      branch.diagnostics.scope.workspace_ids, branch.category, branch.operation]);
  }
  if (!branch.owner) throw new Error("搜索分支缺少 owner");
  return JSON.stringify([branch.owner.gateway_id, branch.owner.workspace_id, branch.category, branch.operation, branch.resource_uri ?? null]);
}

function diagnosticPageMatches(page: GatewayDiagnosticLogQueryResultDTO, branch: FederatedSearchBranchKeyDTO,
  query: FederatedSearchQueryDTO, allocated: number): boolean {
  const target = branch.diagnostics;
  if (!target?.scope || page.gateway_id !== target.gateway_id || page.query !== query.query
    || page.scope?.source !== target.scope.source || JSON.stringify(page.scope.workspace_ids) !== JSON.stringify(target.scope.workspace_ids)
    || page.items.length > allocated || page.complete !== (page.limited_reason == null) || page.complete && page.cursor != null) return false;
  const sources = new Map(page.sources.map(source => [source.log_id, source]));
  return sources.size === page.sources.length && page.sources.every(source => source.source === target.scope!.source
    && (source.source === "gateway" ? source.workspace_ids.length === 0
      : source.workspace_ids.some(id => target.scope!.workspace_ids.includes(id))))
    && page.items.every(item => item.reference?.gateway_id === target.gateway_id && item.reference.log_id === item.source?.log_id
      && item.source?.status !== "unavailable" && JSON.stringify(sources.get(item.source?.log_id ?? "")) === JSON.stringify(item.source));
}

/** 只消费 Gateway 的调度结果；不预检目标、不重试读分支、不另建跨目标队列。 */
export async function streamFederatedSearch(port: number, request: FederatedSearchRequest,
  options: { signal: AbortSignal; onEvent: (event: FederatedSearchEventDTO) => void },
): Promise<FederatedSearchSummaryDTO> {
  const abort = new AbortController();
  const cancel = () => abort.abort(options.signal.reason);
  options.signal.addEventListener("abort", cancel, { once: true });
  if (options.signal.aborted) cancel();
  const deadline = Math.min(request.deadline_at ?? Infinity, (Date.now() + DEFAULT_API_REQUEST_TIMEOUT_MS) / 1000);
  const timer = setTimeout(() => abort.abort(new DOMException("搜索总期限已到", "TimeoutError")), Math.max(0, deadline * 1000 - Date.now()));
  let response: Response | undefined;
  try {
    response = await requestGatewayResponse(port, "/api/gateway/search", {
      // 同一个总期限覆盖建连和正文；不再叠加仅到响应头的计时器。
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...request, deadline_at: deadline }), signal: abort.signal, timeoutMs: 0,
    });
    const requestId = response.headers.get("X-Request-ID");
    if (!requestId) throw new Error("搜索响应缺少 request_id");
    let sequence = 0, scoped = false;
    let expected = new Set<string>();
    const pages = new Set<string>(), terminals = new Set<string>();
    let summary: FederatedSearchSummaryDTO | undefined;
    await consumeSseResponse(response, { signal: abort.signal,
      readError: cause => new SearchStreamReadError(requestId, cause),
      events: { search: defineSseEvent((data, frame) => {
        const event = validateFederatedSearchEvent(decodeJsonSseData(data, frame));
        if (summary || event.sequence !== ++sequence || frame.id !== String(sequence) || event.request_id !== requestId) {
          throw new Error("搜索事件顺序或 request_id 不符，或终态后仍有事件");
        }
        return event;
      }, event => {
        if (event.type === "scope" && event.scope && !scoped) {
          scoped = true;
          const owners = event.scope.scope?.owners;
          if (!owners) throw new Error("搜索范围缺少冻结 owner");
          const requestedOwners = request.scope?.mode === "origin" ? [request.scope.origin]
            : request.scope?.mode === "selected" ? request.scope.owners
            : request.scope?.mode === "resume" ? request.scope.scope?.owners : undefined;
          if (requestedOwners && (owners.length !== requestedOwners.length || owners.some((owner, index) =>
            owner.gateway_id !== requestedOwners[index]?.gateway_id || owner.workspace_id !== requestedOwners[index]?.workspace_id))) {
            throw new Error("搜索实际范围改变了指定 owner 或顺序");
          }
          for (const query of request.queries) for (const target of query.diagnostic_targets) {
            const workspaces = owners.filter(owner => owner.gateway_id === target.gateway_id).map(owner => owner.workspace_id);
            if (!target.scope || !workspaces.length || target.scope.workspace_ids.some(id => !workspaces.includes(id))) {
              throw new Error("诊断搜索来源不属于冻结范围");
            }
          }
          expected = new Set(request.continuations.length ? request.continuations.map(item => {
            if (!item.branch) throw new Error("搜索续查缺少分支");
            return searchBranchKey(item.branch);
          }) : request.queries.flatMap(query => query.category === "runtime_log" ? query.diagnostic_targets.map(diagnostics =>
            searchBranchKey({ owner: undefined, diagnostics, category: query.category, operation: query.operation })) : owners.flatMap(owner => {
            const targets = query.file_targets?.length ? query.file_targets.filter(item => item.owner?.gateway_id === owner.gateway_id
              && item.owner.workspace_id === owner.workspace_id).map(item => item.target?.uri) : [undefined];
            return targets.map(resource_uri => searchBranchKey({ owner, category: query.category, operation: query.operation, resource_uri }));
          })));
        } else if (scoped && (event.type === "page" || event.type === "branch") && event.branch?.branch) {
          const branch = event.branch.branch, key = searchBranchKey(branch);
          if (!expected.has(key) || terminals.has(key)) throw new Error("搜索返回未知分支或重复终态");
          if (event.type === "page") {
            const valid = branch.diagnostics ? event.diagnostic_logs && !event.page
              && diagnosticPageMatches(event.diagnostic_logs, branch, request.queries.find(query => query.category === "runtime_log")!, event.branch.allocated_results)
              && event.diagnostic_logs.items.length === event.branch.returned_results
              : event.page && !event.diagnostic_logs && event.page.workspace_id === branch.owner?.workspace_id
                && event.page.category === branch.category && event.page.operation === branch.operation;
            if (pages.has(key) || !valid) {
              throw new Error("搜索领域页重复或归属不符");
            }
            pages.add(key);
          } else {
            if (["complete", "limited"].includes(event.branch.state) && event.branch.returned_results > 0 && !pages.has(key)) {
              throw new Error("搜索分支没有交付对应正文页");
            }
            terminals.add(key);
          }
        } else if (scoped && event.type === "end" && event.summary) {
          const keys = new Set(event.summary.branches.map(item => {
            if (!item.branch) throw new Error("搜索汇总缺少分支");
            return searchBranchKey(item.branch);
          }));
          if (terminals.size !== expected.size || keys.size !== expected.size
            || event.summary.branches.length !== expected.size || [...keys].some(key => !terminals.has(key))) {
            throw new Error("搜索汇总缺少分支终态");
          }
          summary = event.summary;
        } else throw new Error("搜索事件生命周期不符");
        options.onEvent(event);
      }) },
    });
    abort.signal.throwIfAborted();
    if (!summary) throw new IncompleteSearchStreamError(requestId);
    return summary;
  } catch (error) {
    if (abort.signal.aborted) throw abort.signal.reason;
    throw error;
  } finally {
    clearTimeout(timer);
    options.signal.removeEventListener("abort", cancel);
    // 协议错误发生在开始消费前时，同样回收响应体。
    if (response?.body && !response.bodyUsed && !response.body.locked) await response.body.cancel();
  }
}
