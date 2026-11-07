import { CancellationError } from '../../upstream/src/vs/base/common/errors.js';
import { timeout } from '../../upstream/src/vs/base/common/async.js';
import type { CancellationToken } from '../../upstream/src/vs/base/common/cancellation.js';
import type { IGalleryMcpServer } from '../../upstream/src/vs/platform/mcp/common/mcpManagement.js';
import type { IQuickInputService } from '../../upstream/src/vs/platform/quickinput/common/quickInput.js';
import type { IProgress } from '../../upstream/src/vs/platform/progress/common/progress.js';
import type { GatewayWorkspaceRequestTarget } from '../../../web/src/api/workspaceIdentity.js';
import {
  getMcpInstallationCapabilities, inspectMcpInstallationCandidate, planMcpInstallation,
  installMcpArtifact, getMcpInstallationOperation, cancelMcpInstallationOperation,
} from '../../../web/src/api/mcpConnections.js';
import type {
  McpCandidateSourceDTO, McpCandidatePlanRequestDTO, McpInstalledArtifactDTO,
} from '../../../web/src/types/protocol_generated/boxteam/workspace/v2/public.js';

export function galleryInstallationSource(gallery: IGalleryMcpServer): McpCandidateSourceDTO {
  // 来源字段由同一 gallery owner 使用其正式版本 URI 模板生成，不能在 UI 拼寻址 grammar。
  const source = (gallery as IGalleryMcpServer & {
    readonly installationSource?: { endpoint: string; protocol: string; entryUrl: string };
  }).installationSource;
  if (!source || !['v0.1', 'v0'].includes(source.protocol)) throw new Error('该目录条目缺少可核验的精确来源，不能安装展示投影');
  return { kind: 'gallery', endpoint: source.endpoint, protocol: source.protocol, entry_url: source.entryUrl,
    name: gallery.name, version: gallery.version, entry_id: gallery.id };
}

export interface PublishedMcpArtifact {
  artifact: McpInstalledArtifactDTO;
  candidate: McpCandidatePlanRequestDTO;
}

/** 原 install seam 的制品步骤；返回原生 owner 完整回执，不写配置或创建另一 MCP client。 */
export async function publishMcpArtifact(gallery: IGalleryMcpServer, workspace: GatewayWorkspaceRequestTarget, label: string,
  quickInput: IQuickInputService, progress: IProgress<{ message?: string }>, token: CancellationToken, signal: AbortSignal): Promise<PublishedMcpArtifact> {
  workspace = Object.freeze({ ...workspace });
  const source = galleryInstallationSource(gallery);
  signal.throwIfAborted();
  const capabilities = await getMcpInstallationCapabilities(0, workspace, signal);
  if (!capabilities.data) throw new Error('目标未返回制品安装能力');
  const target = Object.freeze({ ...capabilities.target! });
  const inspected = await inspectMcpInstallationCandidate(0, workspace, { target, source }, signal);
  if (!inspected.data) throw new Error('目标未返回候选原件');
  const selected = await quickInput.pick(inspected.data.variants.map(variant => ({
    label: `${variant.registry_type ?? variant.transport}: ${variant.identifier ?? source.name} ${variant.version ?? source.version}`,
    description: `${variant.ref!.kind} #${variant.ref!.index} · ${inspected.data!.platform?.operating_system}/${inspected.data!.platform?.architecture}`,
    variant,
  })), { title: `安装 MCP 制品至 ${label}`, placeHolder: `显式选择运行方式 · Backend ${target.backend_id}` }, token);
  signal.throwIfAborted();
  if (!selected) throw new CancellationError();
  if (!['pypi', 'npm'].includes(selected.variant.registry_type ?? '') || selected.variant.ref?.kind !== 'package') {
    throw new Error('本批支持 PyPI wheel 与 npm 制品；HTTP 服务须经工作区配置入口，其它生态尚未支持');
  }
  const npm = selected.variant.registry_type === 'npm';
  if (npm ? !capabilities.data.npm_available : !capabilities.data.pypi_wheel_available) {
    throw new Error((npm ? capabilities.data.npm_unavailable_reason : capabilities.data.unavailable_reason)
      ?? `目标尚无可用的 ${npm ? 'npm' : 'PyPI wheel'} 安装能力`);
  }
  const candidate: McpCandidatePlanRequestDTO = { target, source, manifest_digest: inspected.data.manifest_digest,
    variant: selected.variant.ref, inputs: {} };
  const planned = await planMcpInstallation(0, workspace, candidate, signal);
  if (!planned.data || planned.data.disposition !== 'requires_artifact') throw new Error('目标计划没有可安装制品');
  const entrypoint = await quickInput.input({ title: `验证 ${selected.variant.identifier} 的入口`,
    prompt: `输入${npm ? '根 package 的 bin 名称' : '根 distribution 的 console entry point'}；制品安装后不会自动创建工作区配置`,
    placeHolder: '例如 mcp-server-fetch', validateInput: async value => /^[a-zA-Z0-9][a-zA-Z0-9_.-]*$/.test(value) ? undefined : '请输入明确的入口名称' }, token);
  signal.throwIfAborted();
  if (entrypoint === undefined) throw new CancellationError();
  if (token.isCancellationRequested) throw new CancellationError();
  const operationId = crypto.randomUUID();
  let receipt;
  try {
    receipt = await installMcpArtifact(0, workspace, { candidate, plan_digest: planned.data.plan_digest, operation_id: operationId, entrypoint }, signal);
  } catch (cause) {
    throw new Error(`安装请求未确认，不能自动重放。请查询原操作 ${operationId}`, { cause });
  }
  let cancellationSent = false;
  while (receipt.data?.status === 'running') {
    signal.throwIfAborted();
    if (token.isCancellationRequested && !cancellationSent) {
      try { receipt = await cancelMcpInstallationOperation(0, workspace, target, operationId, signal); }
      catch (cause) { throw new Error(`取消请求未确认，请查询原安装操作 ${operationId}`, { cause }); }
      cancellationSent = true;
      if (receipt.data?.status !== 'running') break;
    }
    progress.report({ message: `${label} · ${receipt.data.cancellation_requested ? '取消待核实退出' : receipt.data.stage} · 操作 ${operationId}` });
    await timeout(500);
    try { receipt = await getMcpInstallationOperation(0, workspace, target, operationId, signal); }
    catch (cause) { throw new Error(`安装状态读取中断，原操作可能仍在运行。请查询 ${operationId}`, { cause }); }
  }
  if (receipt.data?.status === 'cancelled') throw new CancellationError();
  if (receipt.data?.status !== 'succeeded' || !receipt.data.artifact) throw new Error(receipt.data?.error ?? '目标没有确认制品发布成功');
  const artifact = receipt.data.artifact;
  if (artifact.binding?.backend_id !== target.backend_id || artifact.manifest_digest !== inspected.data.manifest_digest
      || artifact.variant?.digest !== candidate.variant?.digest || artifact.entrypoint !== entrypoint) {
    throw new Error('安装回执不属于原目标、候选或显式入口');
  }
  signal.throwIfAborted();
  return { artifact, candidate };
}
