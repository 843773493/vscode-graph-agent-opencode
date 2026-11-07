import { timeout } from '../../upstream/src/vs/base/common/async.js';
import type { IQuickInputService, IQuickPickItem, QuickPickInput } from '../../upstream/src/vs/platform/quickinput/common/quickInput.js';
import type { IProgressService } from '../../upstream/src/vs/platform/progress/common/progress.js';
import { ProgressLocation } from '../../upstream/src/vs/platform/progress/common/progress.js';
import type { INotificationService } from '../../upstream/src/vs/platform/notification/common/notification.js';
import { HttpRequestError } from '../../../web/src/api/http.js';
import {
  listMcpInstalledArtifacts, getMcpArtifactReferences, uninstallMcpArtifact, getMcpInstallationOperation,
  type McpInstallationRequestTarget,
} from '../../../web/src/api/mcpConnections.js';
import type {
  McpInstalledArtifactDTO, McpInstallationOperationDTO,
} from '../../../web/src/types/protocol_generated/boxteam/workspace/v2/public.js';

const states: Record<McpInstalledArtifactDTO['state'], string> = {
  published: '已发布', removing: '正在卸载', removal_failed: '卸载失败', removed: '已卸载',
};

/** 原 Quick Input 只消费安装 owner 的完整回执；引用和物理卸载不改工作区配置。 */
export async function manageMcpArtifacts(owner: McpInstallationRequestTarget, label: string,
  quickInput: IQuickInputService, progress: IProgressService, notifications: INotificationService,
  assertAccess: () => void, signal: AbortSignal): Promise<void> {
  const route = Object.freeze({ ...owner });
  const target = Object.freeze({ kind: route.kind, gateway_id: route.gateway_id, backend_id: route.backend_id });
  const title = `MCP 制品 · ${label}`;
  const placeHolder = `${target.gateway_id} · Backend ${target.backend_id} · 制品由同安装域共享，工作区配置各自独立`;

  async function settle(operation: McpInstallationOperationDTO, expectedId: string): Promise<void> {
    if (operation.operation_id !== expectedId) throw new Error('目标未返回原 MCP 操作，不能确认结果');
    await progress.withProgress({ location: ProgressLocation.Notification, title: `${title} · 查询操作 ${expectedId}` }, async report => {
      while (operation.status === 'running') {
        report.report({ message: operation.stage });
        await timeout(500);
        assertAccess();
        const receipt = await getMcpInstallationOperation(0, route, expectedId, signal);
        operation = receipt.data!;
        if (operation.operation_id !== expectedId) throw new Error('目标未返回原 MCP 操作，不能确认结果');
      }
      if (operation.status !== 'succeeded') throw new Error(`MCP 操作 ${expectedId}：${operation.status} · ${operation.error ?? '查看原操作日志'}`);
      notifications.info(`${title} · ${operation.kind === 'uninstall' ? '卸载' : '安装'}已完成 · 操作 ${expectedId}`);
    });
  }

  async function references(artifact: McpInstalledArtifactDTO): Promise<boolean> {
    assertAccess();
    const receipt = await getMcpArtifactReferences(0, route, artifact.binding!, signal);
    const retained = receipt.data!.references;
    if (!retained.length) return false;
    await quickInput.pick(retained.map(reference => ({
      label: `${reference.kind} · ${reference.status}`, description: reference.server_id ?? reference.workspace_id,
      detail: `${reference.workspace_id} · ${reference.reason}`,
    })), { title: `保留引用 · ${artifact.identifier} ${artifact.version}`, placeHolder: '这些实际引用仍需制品；结清后重新显式卸载' });
    return true;
  }

  for (;;) {
    assertAccess();
    const listing = await listMcpInstalledArtifacts(0, route, signal);
    type Choice = IQuickPickItem & { artifact?: McpInstalledArtifactDTO; query?: boolean };
    const items: QuickPickInput<Choice>[] = listing.data.map(artifact => ({
      label: `${artifact.identifier} ${artifact.version}`, description: `${artifact.registry_type} · ${states[artifact.state]}`,
      detail: `${artifact.source?.endpoint ?? artifact.source?.name} · generation ${artifact.binding?.generation}`,
      artifact,
    }));
    items.push({ type: 'separator' }, { label: '查询已有安装／卸载操作', description: '使用原 operation ID，不重新执行', query: true });
    const selected = await quickInput.pick(items, { title, placeHolder: listing.data.length ? placeHolder : `暂无制品 · ${placeHolder}` });
    if (!selected) return;
    assertAccess();
    if (selected.query) {
      const id = await quickInput.input({ title: '查询原 MCP 操作', prompt: placeHolder,
        validateInput: async value => value.trim() ? undefined : '请输入原 operation ID' });
      if (id === undefined) continue;
      assertAccess();
      const receipt = await getMcpInstallationOperation(0, route, id, signal);
      await settle(receipt.data!, id);
      continue;
    }
    const artifact = selected.artifact!;
    const action = await quickInput.pick([
      { label: '查看保留引用', action: 'references' },
      ...(artifact.state === 'published' || artifact.state === 'removal_failed'
        ? [{ label: '物理卸载此制品', description: '不删除工作区配置；实际引用必须全部结清', action: 'remove' }] : []),
      ...(artifact.removal_operation_id ? [{ label: '查询原卸载操作', description: artifact.removal_operation_id, action: 'operation' }] : []),
    ], { title: `${artifact.identifier} ${artifact.version} · ${states[artifact.state]}`, placeHolder });
    if (!action) continue;
    if (action.action === 'operation') {
      assertAccess();
      const receipt = await getMcpInstallationOperation(0, route, artifact.removal_operation_id!, signal);
      await settle(receipt.data!, artifact.removal_operation_id!);
      continue;
    }
    const retained = await references(artifact);
    if (action.action === 'references') {
      if (!retained) notifications.info(`${artifact.identifier} ${artifact.version} · 当前没有保留引用`);
      continue;
    }
    if (retained) continue;
    const confirmation = await quickInput.pick([
      { label: '返回', remove: false }, { label: '确认物理卸载', description: `${artifact.identifier} ${artifact.version} · generation ${artifact.binding!.generation}`, remove: true },
    ], { title: '卸载已发布 MCP 制品', placeHolder });
    if (!confirmation?.remove) continue;
    assertAccess();
    const operationId = crypto.randomUUID();
    try {
      const receipt = await uninstallMcpArtifact(0, route, { target, binding: artifact.binding, operation_id: operationId }, signal);
      await settle(receipt.data!, operationId);
    } catch (cause) {
      // 无论失败还是结果未知都回读唯一 owner；不自动重发 DELETE 或分配新操作。
      try { await listMcpInstalledArtifacts(0, route, signal); }
      catch (readError) { throw new AggregateError([cause, readError], `卸载及状态回读均未确认，请查询原操作 ${operationId}`); }
      if (cause instanceof HttpRequestError && cause.status >= 400 && cause.status < 500) {
        if (cause.status === 409) {
          try { await references(artifact); }
          catch (readError) { throw new AggregateError([cause, readError], `卸载被拒绝且引用回读失败，请查询原操作 ${operationId}`); }
        }
        throw cause;
      }
      throw new Error(`卸载未完成，请查询原操作 ${operationId}；不会自动重放：${cause instanceof Error ? cause.message : String(cause)}`, { cause });
    }
  }
}
