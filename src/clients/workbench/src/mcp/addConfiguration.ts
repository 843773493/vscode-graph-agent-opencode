import { CancellationTokenSource, type CancellationToken } from '../../upstream/src/vs/base/common/cancellation.js';
import { parse, type ParseError } from '../../upstream/src/vs/base/common/json.js';
import { applyEdits, setProperty } from '../../upstream/src/vs/base/common/jsonEdit.js';
import { URI } from '../../upstream/src/vs/base/common/uri.js';
import type { IInputOptions, IQuickInputService } from '../../upstream/src/vs/platform/quickinput/common/quickInput.js';
import type { INotificationService } from '../../upstream/src/vs/platform/notification/common/notification.js';
import { gatewayWorkspaceRequestHeaders, requestJson, unwrapApiData } from '../../../web/src/api/http.js';
import { getConfigSourceDocument } from '../../../web/src/api/configSources.js';
import { writeMcpPrivateHeader } from '../../../web/src/api/mcpConnections.js';
import type { APIResponse } from '../../../web/src/types/backend.js';
import type { ConfigSourceDocumentDTO, McpPrivateHeaderRefDTO, WorkspaceFileRootsDTO } from '../../../web/src/types/protocol_generated/boxteam/workspace/v2/public.js';
import type { PluginInventoryResultDTO, PluginRuntimeStatusDTO, PluginTarget } from '../../../web/src/types/protocol_generated/boxteam/gateway/v2/service_plugin.js';
import { gateway } from '../agentApi.js';
import type { ManagementTarget } from '../managementTarget.js';
import { workspaceContext } from '../workspaceContext.js';
import { workspaceFileSource } from '../workspaceFiles.js';
import { mcpConfigurationErrorMessage, mcpConfigurationSourceKey, saveMcpWorkspaceConfiguration } from './configuration.js';

interface InputContext {
  quickInput: IQuickInputService;
  token: CancellationToken;
  check: () => void;
  owner: string;
}
interface WorkspaceChoice {
  gateway_id: string;
  workspace_id: string;
  backend_id?: string;
  gateway_connection_id?: string;
  name: string;
  status: string;
}
type ManualDefinition = { transport: 'plugin'; service: Record<string, string> } |
  { transport: 'streamable_http'; url: string; headers: Record<string, string | McpPrivateHeaderRefDTO>; oauth?: { redirect_uri: string } };

function hasServer(value: unknown, serverId: string): boolean {
  if (!value || typeof value !== 'object' || !('mcp' in value)) return false;
  const mcp = value.mcp;
  return !!mcp && typeof mcp === 'object' && 'servers' in mcp && !!mcp.servers && typeof mcp.servers === 'object'
    && Object.hasOwn(mcp.servers, serverId);
}

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

async function entries(context: InputContext): Promise<{ plain: Record<string, string>; secrets: Map<string, string> } | undefined> {
  const plain: Record<string, string> = Object.create(null), secrets = new Map<string, string>(), names = new Set<string>();
  for (;;) {
    const action = await context.quickInput.pick([
      { label: '添加普通 HTTP Header', secret: false, done: false },
      { label: '添加私有 HTTP Header', secret: true, done: false },
      { label: '完成 HTTP Header', secret: false, done: true },
    ], { title: '配置 MCP HTTP Header', prompt: context.owner, placeHolder: '私有值只交目标工作区凭据 owner；全部输入完成后才写入' }, context.token);
    context.check();
    if (!action) return;
    if (action.done) return { plain, secrets };
    const key = await input(context, { title: 'HTTP Header 名称', validateInput: async value => {
      if (value.length > 256 || !/^[!#$%&'*+.^_`|~0-9A-Za-z-]+$/.test(value)) return 'Header 名称须为不超过 256 字符的 HTTP token';
      return names.has(value.toLowerCase()) ? 'Header 名称已存在，不能重复添加' : undefined;
    } });
    if (key === undefined) return;
    const value = await input(context, { title: `${action.secret ? '私有' : '普通'} HTTP Header 值 · ${key}`, password: action.secret,
      placeHolder: action.secret ? '输入私有值，不写入普通配置或浏览器存储' : '按原文输入，可明确留空',
      validateInput: async value => {
        if (value.includes('\0')) return '值不能包含 NUL 字符';
        if (action.secret && !value.length) return '私有值不能为空字符串';
        if (action.secret && (value.length > 8192 || /[^\x20-\x7e]/.test(value) || value !== value.trim())) {
          return '私有 Header 必须为不超过 8192 字符、无控制字符及首尾空格的 ASCII 文本';
        }
        return undefined;
      } });
    if (value === undefined) return;
    names.add(key.toLowerCase());
    if (action.secret) secrets.set(key, value); else plain[key] = value;
  }
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

async function selectWorkspace(target: ManagementTarget, label: string, quickInput: IQuickInputService,
  token: CancellationToken, check: () => void, workspaceId?: string): Promise<WorkspaceChoice | undefined> {
  await workspaceContext.refresh();
  check();
  const snapshot = workspaceContext.getSnapshot();
  if (snapshot.error) throw new Error(snapshot.error);
  const choices = snapshot.workspaces.filter(workspace => workspace.gateway_id === target.gateway_id && workspace.status === 'ready');
  if (!choices.length) throw new Error('所选 Gateway 没有已就绪的工作区');
  const workspace = workspaceId === undefined ? (await quickInput.pick(choices.map(item => ({
    label: item.name, description: item.status, detail: item.workspace_id, item,
  })), { title: '选择 MCP 配置工作区', placeHolder: `${label} · 只列出所选 Gateway 的已就绪工作区` }, token))?.item
    : choices.find(item => item.workspace_id === workspaceId);
  check();
  if (!workspace) {
    if (workspaceId !== undefined) throw new Error('指定文件夹不属于所选 Gateway 的已就绪工作区');
    return;
  }
  const route = Object.freeze({ gateway_id: workspace.gateway_id, workspace_id: workspace.workspace_id,
    gateway_connection_id: target.gateway_connection_id });
  const assertWorkspace = () => {
    check();
    const current = workspaceContext.getSnapshot().workspaces.find(item => item.gateway_id === route.gateway_id
      && item.workspace_id === route.workspace_id && item.backend_id === workspace.backend_id);
    if (current?.status !== 'ready') throw new Error('原目标工作区已不就绪或不属于原 Gateway，请重新选择配置目标');
  };
  assertWorkspace();
  return { ...workspace, gateway_connection_id: route.gateway_connection_id };
}

async function pluginService(target: ManagementTarget, workspace: WorkspaceChoice, context: InputContext): Promise<ManualDefinition | undefined> {
  const requestTarget: PluginTarget = { gateway_id: target.gateway_id, gateway_connection_id: target.gateway_connection_id };
  const [inventory, runtime] = await Promise.all([
    gateway.request<PluginInventoryResultDTO>('/api/gateway/service-plugins/inventory', { method: 'POST', body: JSON.stringify(requestTarget) }),
    gateway.request<PluginRuntimeStatusDTO>('/api/gateway/service-plugins/status', { method: 'POST', body: JSON.stringify(requestTarget) }),
  ]);
  context.check();
  const ready = new Set(runtime.plugins.flatMap(slot => {
    const binding = slot.binding;
    return slot.state === 'ready' && binding?.workspace_id === workspace.workspace_id
      ? [JSON.stringify([binding.package_id, binding.package_version, binding.plugin_id])]
      : [];
  }));
  const choices = inventory.packages.flatMap(pkg => {
    const manifest = pkg.manifest;
    if (!manifest) return [];
    return manifest.plugins.flatMap(plugin => {
      if (plugin.instance_scope !== 'workspace' || !ready.has(JSON.stringify([manifest.package_id, manifest.version, plugin.id]))) return [];
      return (plugin.contributes?.services ?? []).filter(service => service.service_type === 'mcp' && service.protocol === 'streamable-http')
        .map(service => ({ packageId: manifest.package_id, version: manifest.version,
          pluginId: plugin.id, pluginName: plugin.name, serviceId: service.service_id }));
    });
  });
  if (!choices.length) return;
  const selected = await context.quickInput.pick(choices.map(service => ({
    label: `${service.pluginName} · ${service.serviceId}`,
    description: `${service.packageId}@${service.version} · ${service.pluginId}`,
    service,
  })), { title: '选择已就绪的 MCP 插件服务', placeHolder: `${workspace.name} · ${target.gateway_id}` }, context.token);
  context.check();
  if (!selected) return;
  return { transport: 'plugin', service: {
    gateway_id: target.gateway_id, workspace_id: workspace.workspace_id, package_id: selected.service.packageId,
    package_version: selected.service.version, plugin_id: selected.service.pluginId, service_id: selected.service.serviceId,
  } };
}

function httpDefinition(context: InputContext): Promise<{ definition: Extract<ManualDefinition, { transport: 'streamable_http' }>;
  secrets: Map<string, string> } | undefined> {
  return (async () => {
    const url = await input(context, { title: 'MCP Streamable HTTP endpoint', placeHolder: '输入目标 HTTP(S) endpoint；环境表达式由目标 owner 解析',
      validateInput: async value => value.length && !value.includes('\0') ? undefined : 'endpoint 不能为空或包含 NUL' });
    if (url === undefined) return;
    const collected = await entries(context);
    if (!collected) return;
    if (collected.secrets.size) requirePrivateEndpoint(url);
    const oauth = await context.quickInput.pick([
      { label: '不使用 OAuth', enabled: false }, { label: '配置 OAuth redirect URI', enabled: true },
    ], { title: 'MCP OAuth 配置', prompt: context.owner }, context.token);
    context.check();
    if (!oauth) return;
    let redirectUri: string | undefined;
    if (oauth.enabled) {
      redirectUri = await input(context, { title: 'OAuth redirect URI', placeHolder: '输入目标工作区可访问的 HTTP(S) redirect URI',
        validateInput: async value => URL.canParse(value) && ['http:', 'https:'].includes(new URL(value).protocol)
          && !new URL(value).username && !new URL(value).password && !new URL(value).hash ? undefined : '请输入无凭据或 fragment 的 HTTP(S) URI' });
      if (redirectUri === undefined) return;
    }
    return { definition: { transport: 'streamable_http', url, headers: collected.plain,
      ...(redirectUri === undefined ? {} : { oauth: { redirect_uri: redirectUri } }) }, secrets: collected.secrets };
  })();
}

export async function addMcpConfiguration(target: ManagementTarget, label: string, quickInput: IQuickInputService,
  notifications: INotificationService, assertAccess: () => void, signal: AbortSignal, configUri?: URI | string): Promise<void> {
  const frozenTarget = Object.freeze({ ...target });
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
    if (folder && (folder.gatewayId !== frozenTarget.gateway_id || folder.path !== folder.root)) {
      throw new Error('指定文件夹必须是原 Gateway 的精确工作区根，不能换成另一配置目标');
    }
    const workspace = await selectWorkspace(frozenTarget, label, quickInput, cancellation.token, check, folder?.workspaceId);
    if (!workspace) return;
    const route = { gateway_id: workspace.gateway_id, workspace_id: workspace.workspace_id,
      gateway_connection_id: workspace.gateway_connection_id };
    const checkWorkspace = () => {
      check();
      const current = workspaceContext.getSnapshot().workspaces.find(item => item.gateway_id === route.gateway_id
        && item.workspace_id === route.workspace_id && item.backend_id === workspace.backend_id);
      if (current?.status !== 'ready') throw new Error('原工作区已不就绪或不属于所选 Gateway');
    };
    if (folder) {
      const roots = unwrapApiData(await requestJson<APIResponse<WorkspaceFileRootsDTO>>(0, '/api/v1/workspace/files/roots', {
        headers: gatewayWorkspaceRequestHeaders(route), signal, cache: 'no-store',
      }));
      checkWorkspace();
      if (folder.root !== roots.workspace_uri) throw new Error('指定文件夹不是目标 owner 返回的工作区根，不能替换配置目标');
    }
    const context = { quickInput, token: cancellation.token, check: checkWorkspace,
      owner: `${workspace.name} · ${route.gateway_id} · 工作区 ${route.workspace_id}` };
    const serverId = await input(context, { title: '添加 MCP Server', placeHolder: '输入新的精确 Server ID',
      validateInput: async value => /^[a-zA-Z][a-zA-Z0-9._-]{1,63}$/.test(value)
        ? undefined : 'Server ID 须以字母开头，长度 2–64，仅含字母、数字、点、下划线或连字符' });
    if (serverId === undefined) return;
    const document = await getConfigSourceDocument(0, route, mcpConfigurationSourceKey, { signal });
    checkWorkspace();
    if (!document.writable) throw new Error('原工作区配置来源不可写');
    assertNewServer(document, serverId);
    const transport = await quickInput.pick([
      { label: '已就绪的 MCP 插件服务', transport: 'plugin' }, { label: '外部 Streamable HTTP', transport: 'streamable_http' },
    ], { title: '选择 MCP 服务来源', prompt: context.owner }, cancellation.token);
    checkWorkspace();
    if (!transport) return;
    let definition: ManualDefinition;
    let secrets = new Map<string, string>();
    if (transport.transport === 'plugin') {
      const selected = await pluginService(frozenTarget, workspace, context);
      checkWorkspace();
      if (!selected) throw new Error('所选 Gateway／工作区没有已就绪且声明 MCP 的插件服务');
      definition = selected;
    } else {
      const selected = await httpDefinition(context);
      if (!selected) return;
      definition = selected.definition;
      secrets = selected.secrets;
    }
    try {
    const enabled = await quickInput.pick([
      { label: '暂不启用此 Server', enabled: false }, { label: '启用此 Server', enabled: true },
    ], { title: `选择 ${serverId} 的启用状态`, placeHolder: context.owner }, cancellation.token);
    checkWorkspace();
    if (!enabled) return;
    try {
      for (const [key, value] of secrets) {
        if (definition.transport !== 'streamable_http') throw new Error('私有 HTTP Header 只能绑定外部 Streamable HTTP 服务');
        const reference = await writeMcpPrivateHeader(0, route, {
          target: { kind: 'workspace', gateway_id: route.gateway_id, workspace_id: route.workspace_id },
          server_id: serverId, header_name: key, endpoint: definition.url, value,
        }, signal);
        checkWorkspace();
        definition.headers[key] = reference;
      }
    } catch (cause) {
      throw new Error(`私有 HTTP Header 写入未确认，不会自动重试；${context.owner} · Server ${serverId} · ${mcpConfigurationErrorMessage(cause)}`, { cause });
    }
    const errors: ParseError[] = [];
    const source: unknown = parse(document.jsonc || '{}', errors);
    if (errors.length || !source || typeof source !== 'object' || Array.isArray(source)) throw new Error('原工作区配置来源不是有效 JSONC 对象');
    const server = { enabled: enabled.enabled, ...definition };
    const jsonc = applyEdits(document.jsonc || '{}', setProperty(document.jsonc || '{}', ['mcp', 'servers', serverId], server,
      { insertSpaces: true, tabSize: 2, eol: document.jsonc.includes('\r\n') ? '\r\n' : '\n' }));
    await saveMcpWorkspaceConfiguration(route, document, jsonc, quickInput, notifications, signal, checkWorkspace,
      `${serverId} · ${definition.transport === 'plugin' ? '插件 MCP 服务' : '外部 Streamable HTTP'}`);
    } finally { secrets.clear(); }
  } finally { signal.removeEventListener('abort', abortInput); cancellation.dispose(); }
}
