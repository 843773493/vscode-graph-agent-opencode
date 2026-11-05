import {
  cancelMcpOperation, getMcpCatalog, listMcpOperations, submitMcpOperation,
  type McpConnectionOperationDTO, type McpServerDTO,
} from '../../../web/src/api/mcpConnections.js';
import type { McpServerListResponseDTO } from '../../../web/src/types/protocol_generated/boxteam/workspace/v2/public.js';
import { gatewayWorkspaceKey, type GatewayWorkspaceRequestTarget } from '../../../web/src/api/workspaceIdentity.js';
import { authorizeMcpInWindow } from './authorizationWindow.js';

export interface McpCatalogSnapshot {
  catalog?: McpServerListResponseDTO;
  operations: readonly McpConnectionOperationDTO[];
  error?: string;
  actionError?: string;
}

export const pendingMcpOperation = (operation: McpConnectionOperationDTO | undefined) =>
  operation !== undefined && ['pending', 'waiting_user', 'verifying'].includes(operation.status);

/** 只保存原 owner 的完整回执；不维护第二份配置、连接或目录事实。 */
export class WorkspaceMcpCatalog {
  readonly target: GatewayWorkspaceRequestTarget;
  readonly key: string;
  snapshot: McpCatalogSnapshot = { operations: [] };
  private generation = 0;
  private busy = false;
  private reading?: { promise: Promise<void>; refresh: boolean };
  private readonly controller = new AbortController();

  constructor(target: GatewayWorkspaceRequestTarget, readonly label: string, private readonly changed: () => void) {
    this.target = Object.freeze({ gateway_id: target.gateway_id, workspace_id: target.workspace_id });
    this.key = gatewayWorkspaceKey(this.target);
  }

  server(serverId: string): McpServerDTO | undefined { return this.snapshot.catalog?.data.find(server => server.server_id === serverId); }
  operation(serverId: string): McpConnectionOperationDTO | undefined { return this.snapshot.operations.find(operation => operation.server_id === serverId); }
  get pending(): boolean { return this.snapshot.operations.some(pendingMcpOperation); }
  get isBusy(): boolean { return this.busy; }

  refresh({ refresh = false }: { refresh?: boolean } = {}): Promise<void> {
    if (this.controller.signal.aborted) return Promise.reject(new Error('MCP 工作区目标已移除'));
    if (this.reading) {
      if (!refresh || this.reading.refresh) return this.reading.promise;
      // 被动读取无论成功与否，显式刷新都要在其后真正访问原 owner；并发刷新由下一次 reading 归并。
      return this.reading.promise.then(() => this.refresh({ refresh: true }), () => this.refresh({ refresh: true }));
    }
    const promise = this.read(this.generation, refresh).finally(() => { if (this.reading?.promise === promise) this.reading = undefined; });
    this.reading = { promise, refresh };
    return promise;
  }

  private async read(generation: number, refresh: boolean): Promise<void> {
    try {
      const [catalog, operations] = await Promise.all([
        getMcpCatalog(0, this.target, { refresh, signal: this.controller.signal }), listMcpOperations(0, this.target, this.controller.signal),
      ]);
      if (generation !== this.generation || this.controller.signal.aborted) return;
      this.snapshot = { catalog, operations, actionError: this.snapshot.actionError };
      this.changed();
    } catch (error) {
      if (generation !== this.generation || this.controller.signal.aborted) return;
      this.snapshot = { ...this.snapshot, error: error instanceof Error ? error.message : String(error) };
      this.changed();
      throw error;
    }
  }

  async act(serverId: string, action: 'connect' | 'disconnect' | 'authorize' | 'cancel'): Promise<void> {
    if (this.controller.signal.aborted) throw new Error('MCP 工作区目标已移除');
    if (this.busy) throw new Error('此工作区的 MCP 管理请求尚未完成，请读取实际状态后重试');
    if (!this.server(serverId)) throw new Error('此 MCP 服务已不在目标工作区目录中');
    this.busy = true;
    ++this.generation;
    this.snapshot = { ...this.snapshot, actionError: undefined };
    this.changed();
    const operation = this.operation(serverId);
    let failure: unknown;
    try {
      let result: McpConnectionOperationDTO;
      if (action === 'authorize' || action === 'cancel') {
        if (!operation || !pendingMcpOperation(operation)) throw new Error('此服务没有可继续的 MCP 连接操作');
        result = action === 'authorize'
          ? await authorizeMcpInWindow(0, this.target, operation, this.controller.signal)
          : await cancelMcpOperation(0, this.target, operation.operation_id, this.controller.signal);
      } else {
        result = await submitMcpOperation(0, this.target, serverId, action, this.controller.signal);
      }
      if (!this.controller.signal.aborted) {
        this.snapshot = { ...this.snapshot, operations: [result, ...this.snapshot.operations.filter(item => item.server_id !== serverId)], error: undefined };
        this.changed();
      }
    } catch (error) {
      failure = error;
      this.snapshot = { ...this.snapshot, actionError: error instanceof Error ? error.message : String(error) };
      this.changed();
    }
    try {
      // 写入前的读取可继续收尾，但不能越过操作代次发布；失败同样重取唯一 owner。
      try { await this.reading?.promise; }
      catch (error) { failure = failure === undefined ? error : new AggregateError([failure, error], 'MCP 操作与并发读取均失败'); }
      try { await this.refresh(); }
      catch (error) { failure = failure === undefined ? error : new AggregateError([failure, error], 'MCP 操作与状态重读均失败'); }
    } finally {
      this.busy = false;
      this.changed();
    }
    if (failure !== undefined) throw failure;
  }

  dispose(): void { ++this.generation; this.controller.abort(); }
}
