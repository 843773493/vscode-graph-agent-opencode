import { CancellationTokenSource, type CancellationToken } from '../../upstream/src/vs/base/common/cancellation.js';
import { parse, type ParseError } from '../../upstream/src/vs/base/common/json.js';
import { URI } from '../../upstream/src/vs/base/common/uri.js';
import type { IInputOptions, IQuickInputService } from '../../upstream/src/vs/platform/quickinput/common/quickInput.js';
import type { INotificationService } from '../../upstream/src/vs/platform/notification/common/notification.js';
import { gatewayWorkspaceRequestHeaders, requestJson, unwrapApiData } from '../../../web/src/api/http.js';
import { getConfigSourceDocument } from '../../../web/src/api/configSources.js';
import { inspectMcpInstallationCandidate, writeMcpPrivateEnv, writeMcpPrivateHeader,
  type McpInstallationRequestTarget } from '../../../web/src/api/mcpConnections.js';
import type { APIResponse } from '../../../web/src/types/backend.js';
import type { ConfigSourceDocumentDTO, McpCandidatePlanRequestDTO, McpPrivateEnvRefDTO, McpPrivateHeaderRefDTO,
  McpStdioLaunchDefinitionDTO, WorkspaceFileRootsDTO } from '../../../web/src/types/protocol_generated/boxteam/workspace/v2/public.js';
import { workspaceFileSource } from '../workspaceFiles.js';
import { mcpConfigurationErrorMessage, mcpConfigurationSourceKey, saveMcpWorkspaceConfiguration,
  selectMcpConfigurationWorkspace } from './configuration.js';

type ManualDefinition = (McpStdioLaunchDefinitionDTO & {
  transport: 'stdio'; env: Record<string, string | McpPrivateEnvRefDTO>;
}) | { transport: 'streamable_http'; url: string; headers: Record<string, string | McpPrivateHeaderRefDTO> };
interface InputContext {
  quickInput: IQuickInputService;
  token: CancellationToken;
  check: () => void;
  owner: string;
}

function hasServer(value: unknown, serverId: string): boolean {
  if (!value || typeof value !== 'object' || !('mcp' in value)) return false;
  const mcp = value.mcp;
  return !!mcp && typeof mcp === 'object' && 'servers' in mcp && !!mcp.servers && typeof mcp.servers === 'object'
    && Object.hasOwn(mcp.servers, serverId);
}

/** Add 不能变成 upsert；同时检查原来源、待生效合并值和 active，竞争仍由原 CAS 拒绝。 */
function assertNewServer(document: ConfigSourceDocumentDTO, serverId: string): void {
  const errors: ParseError[] = [];
  const source: unknown = parse(document.jsonc || '{}', errors);
  if (errors.length) throw new Error('原工作区来源正文无效，无法核对 Server ID');
  if ([source, document.source_effective, document.effective].some(value => hasServer(value, serverId))) {
    throw new Error(`工作区已存在 Server ID ${serverId}，Add 不会覆盖已有配置；请明确编辑原 Server`);
  }
}

async function input(context: InputContext, options: IInputOptions): Promise<string | undefined> {
  const value = await context.quickInput.input({ prompt: context.owner, ...options }, context.token);
  context.check();
  return value;
}

async function entries(kind: 'env' | 'header', context: InputContext): Promise<{
  plain: Record<string, string>; secrets: Map<string, string>;
} | undefined> {
  const plain: Record<string, string> = Object.create(null), secrets = new Map<string, string>(), names = new Set<string>();
  const env = kind === 'env', field = env ? '环境变量' : 'HTTP Header';
  for (;;) {
    const action = await context.quickInput.pick([
      { label: `添加普通${field}`, secret: false, done: false },
      { label: `添加私有${field}`, secret: true, done: false },
      { label: `完成${field}`, secret: false, done: true },
    ], { title: `配置 MCP ${field}`, prompt: context.owner,
      placeHolder: '私有值只交目标工作区凭据 owner；全部输入完成后才写入' }, context.token);
    context.check();
    if (!action) return;
    if (action.done) return { plain, secrets };
    const key = await input(context, { title: `${field}名称`, validateInput: async value => {
      if (env ? !/^[a-zA-Z_][a-zA-Z0-9_]*$/.test(value) : value.length > 256 || !/^[!#$%&'*+.^_`|~0-9A-Za-z-]+$/.test(value)) {
        return env ? '环境变量名须以字母或下划线开头，仅含字母、数字或下划线' : 'Header 名称须为不超过 256 字符的 HTTP token';
      }
      return names.has(env ? value : value.toLowerCase()) ? `${field}名称已存在，不能重复添加` : undefined;
    } });
    if (key === undefined) return;
    const value = await input(context, { title: `${action.secret ? '私有' : '普通'}${field}值 · ${key}`, password: action.secret,
      placeHolder: action.secret ? '输入私有值，不写入普通配置或浏览器存储' : '按原文输入，可明确留空',
      validateInput: async value => {
        if (value.includes('\0')) return '值不能包含 NUL 字符';
        if (action.secret && !value.length) return '私有值不能为空字符串';
        if (!env && action.secret && (value.length > 8192 || /[^\x20-\x7e]/.test(value) || value !== value.trim())) {
          return '私有 Header 必须为不超过 8192 字符、无控制字符及首尾空格的 ASCII 文本';
        }
        return undefined;
      } });
    if (value === undefined) return;
    names.add(env ? key : key.toLowerCase());
    if (action.secret) secrets.set(key, value);
    else plain[key] = value;
  }
}

async function stdioDefinition(context: InputContext): Promise<{ definition: ManualDefinition; secrets: Map<string, string> } | undefined> {
  const command = await input(context, { title: 'MCP stdio 可执行命令', placeHolder: '仅输入 command；参数下一步逐项收集，不经过 shell 拆词',
    validateInput: async value => value.length && !value.includes('\0') ? undefined : '请输入非空且不含 NUL 的 command' });
  if (command === undefined) return;
  const args: string[] = [];
  for (;;) {
    const action = await context.quickInput.pick([{ label: '添加一个参数', add: true }, { label: '完成参数', add: false }],
      { title: 'MCP stdio 参数', prompt: context.owner, placeHolder: `已添加 ${args.length} 项，保留加入顺序、重复值、空字符串和引号` }, context.token);
    context.check();
    if (!action) return;
    if (!action.add) break;
    const value = await input(context, { title: `MCP 参数 #${args.length + 1}`, placeHolder: '输入一个完整 argv；可以留空，引号作为字符保留',
      validateInput: async value => value.includes('\0') ? '参数不能包含 NUL 字符' : undefined });
    if (value === undefined) return;
    args.push(value);
  }
  const directory = await context.quickInput.pick([
    { label: '使用目标工作区默认目录', custom: false }, { label: '指定工作目录', custom: true },
  ], { title: 'MCP stdio 工作目录', prompt: context.owner }, context.token);
  context.check();
  if (!directory) return;
  const cwd = directory.custom ? await input(context, { title: 'MCP stdio 工作目录', placeHolder: '目标机器上的 cwd，或原有目标环境表达式',
    validateInput: async value => value.length && !value.includes('\0') ? undefined : '工作目录不能为空或包含 NUL' }) : undefined;
  if (directory.custom && cwd === undefined) return;
  const collected = await entries('env', context);
  if (!collected) return;
  return { definition: { transport: 'stdio', command, args, ...(cwd === undefined ? {} : { cwd }), env: collected.plain }, secrets: collected.secrets };
}

function requirePrivateEndpoint(endpoint: string): void {
  if (endpoint.includes('${')) throw new Error('私有 Header 需要精确的 literal endpoint，不能在浏览器解析目标环境表达式');
  if (!URL.canParse(endpoint)) throw new Error('私有 Header endpoint 必须为有效 HTTP(S) URL');
  const parsed = new URL(endpoint);
  if (!['http:', 'https:'].includes(parsed.protocol) || !parsed.hostname || endpoint !== endpoint.trim()
    || parsed.username || parsed.password || endpoint.includes('#')
    || parsed.protocol === 'http:' && !['localhost', '127.0.0.1', '[::1]'].includes(parsed.hostname)) {
    throw new Error('私有 Header endpoint 须为无凭据、fragment 或首尾空白的 HTTP(S) URL；仅 loopback 可使用 HTTP');
  }
}

async function httpDefinition(context: InputContext): Promise<{ definition: ManualDefinition; secrets: Map<string, string> } | undefined> {
  const url = await input(context, { title: 'MCP Streamable HTTP endpoint', placeHolder: '输入目标 HTTP(S) endpoint；环境表达式由目标 owner 解析',
    validateInput: async value => value.length && !value.includes('\0') ? undefined : 'endpoint 不能为空或包含 NUL' });
  if (url === undefined) return;
  const collected = await entries('header', context);
  if (!collected) return;
  if (collected.secrets.size) requirePrivateEndpoint(url);
  return { definition: { transport: 'streamable_http', url, headers: collected.plain }, secrets: collected.secrets };
}

/** 原 Add 命令的手动定义纵向片；不进入旧 mcp.json writer、saved inputs 或安装循环。 */
export async function addMcpConfiguration(installation: McpInstallationRequestTarget, label: string,
  quickInput: IQuickInputService, notifications: INotificationService, assertAccess: () => void, signal: AbortSignal,
  configUri?: URI | string): Promise<void> {
  const installationTarget = Object.freeze({ ...installation });
  const cancellation = new CancellationTokenSource();
  const abortInput = () => cancellation.cancel();
  const check = () => { assertAccess(); signal.throwIfAborted(); };
  signal.addEventListener('abort', abortInput, { once: true });
  try {
    check();
    let folder: ReturnType<typeof workspaceFileSource> | undefined;
    if (configUri !== undefined) {
      const resource = typeof configUri === 'string' ? URI.parse(configUri, true) : configUri;
      if (!URI.isUri(resource)) throw new Error('显式 MCP 配置目标必须是可核验的工作区根 URI');
      if (/(?:^|[/\\])(?:\.mcp\.json|\.vscode[/\\]mcp\.json)$/.test(resource.path)) {
        throw new Error('此 Add 入口尚不支持精确写入 .mcp.json／.vscode/mcp.json，不能换成另一配置来源');
      }
      folder = workspaceFileSource(resource);
    }
    if (folder && (folder.gatewayId !== installationTarget.gateway_id || folder.path !== folder.root)) {
      throw new Error('指定文件夹必须是原 Gateway／Backend 的精确工作区根，不能换成另一配置目标');
    }
    const workspace = await selectMcpConfigurationWorkspace(installationTarget, label, quickInput, cancellation.token, check, folder?.workspaceId);
    if (!workspace) return;
    const { route, owner, check: checkWorkspace } = workspace;
    if (folder) {
      const roots = unwrapApiData(await requestJson<APIResponse<WorkspaceFileRootsDTO>>(0, '/api/v1/workspace/files/roots', {
        headers: gatewayWorkspaceRequestHeaders(route), signal, cache: 'no-store',
      }));
      checkWorkspace();
      if (folder.root !== roots.workspace_uri) throw new Error('指定文件夹不是目标 owner 返回的工作区根，不能替换配置目标');
    }
    const context = { quickInput, token: cancellation.token, check: checkWorkspace, owner };
    const serverId = await input(context, { title: '添加 MCP Server', placeHolder: '输入新的精确 Server ID',
      validateInput: async value => /^[a-zA-Z][a-zA-Z0-9._-]{1,63}$/.test(value)
        ? undefined : 'Server ID 须以字母开头，长度 2–64，仅含字母、数字、点、下划线或连字符' });
    if (serverId === undefined) return;
    const document = await getConfigSourceDocument(0, route, mcpConfigurationSourceKey, { signal });
    checkWorkspace();
    if (!document.writable) throw new Error('原工作区配置来源不可写');
    assertNewServer(document, serverId);
    const transport = await quickInput.pick([{ label: 'stdio', transport: 'stdio' }, { label: 'Streamable HTTP', transport: 'streamable_http' }],
      { title: '选择 MCP 传输方式', prompt: owner }, cancellation.token);
    checkWorkspace();
    if (!transport) return;
    const collected = await (transport.transport === 'stdio' ? stdioDefinition(context) : httpDefinition(context));
    if (!collected) return;
    const enabled = await quickInput.pick([{ label: '暂不启用此 Server', enabled: false }, { label: '启用此 Server', enabled: true }],
      { title: `选择 ${serverId} 的启用状态`, placeHolder: owner }, cancellation.token);
    checkWorkspace();
    if (!enabled) return;
    const { definition, secrets } = collected;
    const hasPrivateInputs = secrets.size > 0;
    const target = { kind: 'workspace', gateway_id: route.gateway_id, workspace_id: route.workspace_id };
    try {
      for (const [key, value] of secrets) {
        try {
          if (definition.transport === 'stdio') {
            const { command, args, cwd } = definition;
            definition.env[key] = await writeMcpPrivateEnv(0, route, { target, server_id: serverId, input_key: key,
              launch: { command, args, ...(cwd === undefined ? {} : { cwd }) }, value }, signal);
          } else definition.headers[key] = await writeMcpPrivateHeader(0, route, {
            target, server_id: serverId, header_name: key, endpoint: definition.url, value,
          }, signal);
        } catch (cause) {
          throw new Error(`私有 ${definition.transport === 'stdio' ? 'env' : 'Header'} ${key} 写入未确认，不会自动重试；${owner} · Server ${serverId} · ${mcpConfigurationErrorMessage(cause)}`, { cause });
        }
        checkWorkspace();
      }
    } finally { secrets.clear(); }
    const source = { kind: 'manual', name: serverId, version: 'user-defined', definition };
    const backend = { kind: 'backend', gateway_id: installationTarget.gateway_id, backend_id: installationTarget.backend_id };
    const inspected = await inspectMcpInstallationCandidate(0, installationTarget, { target: backend, source }, signal);
    checkWorkspace();
    const variant = inspected.data?.variants;
    if (!inspected.data || variant?.length !== 1 || variant[0]!.ref?.kind !== 'manual' || variant[0]!.transport !== definition.transport
      || variant[0]!.configuration_inputs.length || variant[0]!.configuration_unavailable_reason != null) {
      throw new Error('目标没有返回唯一可用的手动候选，不能选择其它 variant 或省略输入');
    }
    const candidate: McpCandidatePlanRequestDTO = { target: backend, source, manifest_digest: inspected.data.manifest_digest,
      variant: structuredClone(variant[0]!.ref), server_id: serverId, inputs: {} };
    await saveMcpWorkspaceConfiguration({ ...workspace, quickInput, notifications, token: cancellation.token, signal }, {
      candidate, document, serverId, enabled: enabled.enabled,
      disposition: definition.transport === 'stdio' ? 'explicit_command' : 'configuration_only',
      summary: `${definition.transport} 手动定义${hasPrivateInputs ? ' · 私有版本已写入，取消保存不会删除该版本' : ''}`,
    });
  } finally { signal.removeEventListener('abort', abortInput); cancellation.dispose(); }
}
