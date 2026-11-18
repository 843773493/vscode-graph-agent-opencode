import { t } from '../../../shared/core/i18n/index.js';
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
  if (errors.length) throw new Error(t('workbench.mcp.sourceInvalid'));
  if ([source, document.source_effective, document.effective].some(value => hasServer(value, serverId))) {
    throw new Error(t('workbench.mcp.duplicateServer', serverId));
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
      { label: t('workbench.mcp.addHeader'), secret: false, done: false },
      { label: t('workbench.mcp.addPrivateHeader'), secret: true, done: false },
      { label: t('workbench.mcp.finishHeaders'), secret: false, done: true },
    ], { title: t('workbench.mcp.configureHeaders'), prompt: context.owner, placeHolder: t('workbench.mcp.privateHeadersHint') }, context.token);
    context.check();
    if (!action) return;
    if (action.done) return { plain, secrets };
    const key = await input(context, { title: t('workbench.mcp.headerName'), validateInput: async value => {
      if (value.length > 256 || !/^[!#$%&'*+.^_`|~0-9A-Za-z-]+$/.test(value)) return t('workbench.mcp.headerNameInvalid');
      return names.has(value.toLowerCase()) ? t('workbench.mcp.headerNameDuplicate') : undefined;
    } });
    if (key === undefined) return;
    const value = await input(context, { title: action.secret ? t('workbench.mcp.privateHeaderValue', key) : t('workbench.mcp.plainHeaderValue', key), password: action.secret,
      placeHolder: action.secret ? t('workbench.mcp.privateValueHint') : t('workbench.mcp.plainValueHint'),
      validateInput: async value => {
        if (value.includes('\0')) return t('workbench.mcp.valueNul');
        if (action.secret && !value.length) return t('workbench.mcp.privateValueEmpty');
        if (action.secret && (value.length > 8192 || /[^\x20-\x7e]/.test(value) || value !== value.trim())) {
          return t('workbench.mcp.privateValueInvalid');
        }
        return undefined;
      } });
    if (value === undefined) return;
    names.add(key.toLowerCase());
    if (action.secret) secrets.set(key, value); else plain[key] = value;
  }
}

function requirePrivateEndpoint(endpoint: string): void {
  if (endpoint.includes('${')) throw new Error(t('workbench.mcp.literalEndpointRequired'));
  if (!URL.canParse(endpoint)) throw new Error(t('workbench.mcp.endpointInvalid'));
  const parsed = new URL(endpoint);
  if (!['http:', 'https:'].includes(parsed.protocol) || !parsed.hostname || endpoint !== endpoint.trim()
    || parsed.username || parsed.password || endpoint.includes('#')
    || parsed.protocol === 'http:' && !['localhost', '127.0.0.1', '[::1]'].includes(parsed.hostname)) {
    throw new Error(t('workbench.mcp.privateEndpointInvalid'));
  }
}

async function selectWorkspace(target: ManagementTarget, label: string, quickInput: IQuickInputService,
  token: CancellationToken, check: () => void, workspaceId?: string): Promise<WorkspaceChoice | undefined> {
  await workspaceContext.refresh();
  check();
  const snapshot = workspaceContext.getSnapshot();
  if (snapshot.error) throw new Error(snapshot.error);
  const choices = snapshot.workspaces.filter(workspace => workspace.gateway_id === target.gateway_id && workspace.status === 'ready');
  if (!choices.length) throw new Error(t('workbench.mcp.noReadyWorkspace'));
  const workspace = workspaceId === undefined ? (await quickInput.pick(choices.map(item => ({
    label: item.name, description: item.status, detail: item.workspace_id, item,
  })), { title: t('workbench.mcp.chooseWorkspace'), placeHolder: t('workbench.mcp.workspaceChoicesHint', label) }, token))?.item
    : choices.find(item => item.workspace_id === workspaceId);
  check();
  if (!workspace) {
    if (workspaceId !== undefined) throw new Error(t('workbench.mcp.folderNotReady'));
    return;
  }
  const route = Object.freeze({ gateway_id: workspace.gateway_id, workspace_id: workspace.workspace_id,
    gateway_connection_id: target.gateway_connection_id });
  const assertWorkspace = () => {
    check();
    const current = workspaceContext.getSnapshot().workspaces.find(item => item.gateway_id === route.gateway_id
      && item.workspace_id === route.workspace_id && item.backend_id === workspace.backend_id);
    if (current?.status !== 'ready') throw new Error(t('workbench.mcp.targetChanged'));
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
    return slot.state === 'ready' && binding && (!binding.workspace_id || binding.workspace_id === workspace.workspace_id)
      ? [JSON.stringify([binding.package_id, binding.package_version, binding.plugin_id])]
      : [];
  }));
  const choices = inventory.packages.flatMap(pkg => {
    const manifest = pkg.manifest;
    if (!manifest) return [];
    return manifest.plugins.flatMap(plugin => {
      if (!ready.has(JSON.stringify([manifest.package_id, manifest.version, plugin.id]))) return [];
      return (plugin.contributes?.services ?? []).filter(service => service.service_type === 'mcp' && service.protocol === 'streamable-http')
        .map(service => ({ packageId: manifest.package_id, version: manifest.version,
          pluginId: plugin.id, pluginName: plugin.name, serviceId: service.service_id, scope: plugin.instance_scope }));
    });
  });
  if (!choices.length) return;
  const selected = await context.quickInput.pick(choices.map(service => ({
    label: `${service.pluginName} · ${service.serviceId}`,
    description: `${service.packageId}@${service.version} · ${service.pluginId} · ${service.scope}`,
    service,
  })), { title: t('workbench.mcp.choosePluginService'), placeHolder: `${workspace.name} · ${target.gateway_id}` }, context.token);
  context.check();
  if (!selected) return;
  return { transport: 'plugin', service: {
    gateway_id: target.gateway_id, ...(selected.service.scope === 'workspace' ? { workspace_id: workspace.workspace_id } : {}),
    package_id: selected.service.packageId,
    package_version: selected.service.version, plugin_id: selected.service.pluginId, service_id: selected.service.serviceId,
  } };
}

function httpDefinition(context: InputContext): Promise<{ definition: Extract<ManualDefinition, { transport: 'streamable_http' }>;
  secrets: Map<string, string> } | undefined> {
  return (async () => {
    const url = await input(context, { title: t('workbench.mcp.httpEndpoint'), placeHolder: t('workbench.mcp.httpEndpointHint'),
      validateInput: async value => value.length && !value.includes('\0') ? undefined : t('workbench.mcp.httpEndpointInvalid') });
    if (url === undefined) return;
    const collected = await entries(context);
    if (!collected) return;
    if (collected.secrets.size) requirePrivateEndpoint(url);
    const oauth = await context.quickInput.pick([
      { label: t('workbench.mcp.noOAuth'), enabled: false }, { label: t('workbench.mcp.configureRedirect'), enabled: true },
    ], { title: t('workbench.mcp.configureOAuth'), prompt: context.owner }, context.token);
    context.check();
    if (!oauth) return;
    let redirectUri: string | undefined;
    if (oauth.enabled) {
      redirectUri = await input(context, { title: t('workbench.mcp.redirectTitle'), placeHolder: t('workbench.mcp.redirectHint'),
        validateInput: async value => URL.canParse(value) && ['http:', 'https:'].includes(new URL(value).protocol)
          && !new URL(value).username && !new URL(value).password && !new URL(value).hash ? undefined : t('workbench.mcp.redirectInvalid') });
      if (redirectUri === undefined) return;
    }
    return { definition: { transport: 'streamable_http', url, headers: collected.plain,
      ...(redirectUri === undefined ? {} : { oauth: { redirect_uri: redirectUri } }) }, secrets: collected.secrets };
  })();
}

export async function addMcpConfiguration(target: ManagementTarget, label: string, quickInput: IQuickInputService,
  notifications: INotificationService, assertAccess: () => void, signal: AbortSignal, configUri?: URI | string, workspaceId?: string): Promise<void> {
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
      if (!URI.isUri(resource)) throw new Error(t('workbench.mcp.rootUriRequired'));
      if (/(?:^|[/\\])(?:\.mcp\.json|\.vscode[/\\]mcp\.json)$/.test(resource.path)) {
        throw new Error(t('workbench.mcp.unsupportedConfigSource'));
      }
      folder = workspaceFileSource(resource);
    }
    if (folder && (folder.gatewayId !== frozenTarget.gateway_id || folder.path !== folder.root)) {
      throw new Error(t('workbench.mcp.exactRootRequired'));
    }
    const workspace = await selectWorkspace(frozenTarget, label, quickInput, cancellation.token, check, folder?.workspaceId ?? workspaceId);
    if (!workspace) return;
    const route = { gateway_id: workspace.gateway_id, workspace_id: workspace.workspace_id,
      gateway_connection_id: workspace.gateway_connection_id };
    const checkWorkspace = () => {
      check();
      const current = workspaceContext.getSnapshot().workspaces.find(item => item.gateway_id === route.gateway_id
        && item.workspace_id === route.workspace_id && item.backend_id === workspace.backend_id);
      if (current?.status !== 'ready') throw new Error(t('workbench.mcp.workspaceChanged'));
    };
    if (folder) {
      const roots = unwrapApiData(await requestJson<APIResponse<WorkspaceFileRootsDTO>>(0, '/api/v1/workspace/files/roots', {
        headers: gatewayWorkspaceRequestHeaders(route), signal, cache: 'no-store',
      }));
      checkWorkspace();
      if (folder.root !== roots.workspace_uri) throw new Error(t('workbench.mcp.rootMismatch'));
    }
    const context = { quickInput, token: cancellation.token, check: checkWorkspace,
      owner: t('workbench.mcp.workspaceOwner', workspace.name, route.gateway_id, route.workspace_id) };
    const serverId = await input(context, { title: t('workbench.mcp.addServer'), placeHolder: t('workbench.mcp.serverIdHint'),
      validateInput: async value => /^[a-zA-Z][a-zA-Z0-9._-]{1,63}$/.test(value)
        ? undefined : t('workbench.mcp.serverIdInvalid') });
    if (serverId === undefined) return;
    const document = await getConfigSourceDocument(0, route, mcpConfigurationSourceKey, { signal });
    checkWorkspace();
    if (!document.writable) throw new Error(t('workbench.mcp.sourceReadonly'));
    assertNewServer(document, serverId);
    const transport = await quickInput.pick([
      { label: t('workbench.mcp.readyPluginService'), transport: 'plugin' }, { label: t('workbench.mcp.externalHttp'), transport: 'streamable_http' },
    ], { title: t('workbench.mcp.chooseSource'), prompt: context.owner }, cancellation.token);
    checkWorkspace();
    if (!transport) return;
    let definition: ManualDefinition;
    let secrets = new Map<string, string>();
    if (transport.transport === 'plugin') {
      const selected = await pluginService(frozenTarget, workspace, context);
      checkWorkspace();
      if (!selected) throw new Error(t('workbench.mcp.noPluginService'));
      definition = selected;
    } else {
      const selected = await httpDefinition(context);
      if (!selected) return;
      definition = selected.definition;
      secrets = selected.secrets;
    }
    try {
    const enabled = await quickInput.pick([
      { label: t('workbench.mcp.disableServer'), enabled: false }, { label: t('workbench.mcp.enableServer'), enabled: true },
    ], { title: t('workbench.mcp.chooseEnablement', serverId), placeHolder: context.owner }, cancellation.token);
    checkWorkspace();
    if (!enabled) return;
    try {
      for (const [key, value] of secrets) {
        if (definition.transport !== 'streamable_http') throw new Error(t('workbench.mcp.privateHeaderTransport'));
        const reference = await writeMcpPrivateHeader(0, route, {
          target: { kind: 'workspace', gateway_id: route.gateway_id, workspace_id: route.workspace_id },
          server_id: serverId, header_name: key, endpoint: definition.url, value,
        }, signal);
        checkWorkspace();
        definition.headers[key] = reference;
      }
    } catch (cause) {
      throw new Error(t('workbench.mcp.privateHeaderUnconfirmed', context.owner, serverId, mcpConfigurationErrorMessage(cause)), { cause });
    }
    const errors: ParseError[] = [];
    const source: unknown = parse(document.jsonc || '{}', errors);
    if (errors.length || !source || typeof source !== 'object' || Array.isArray(source)) throw new Error(t('workbench.mcp.sourceObjectRequired'));
    const server = { enabled: enabled.enabled, ...definition };
    const jsonc = applyEdits(document.jsonc || '{}', setProperty(document.jsonc || '{}', ['mcp', 'servers', serverId], server,
      { insertSpaces: true, tabSize: 2, eol: document.jsonc.includes('\r\n') ? '\r\n' : '\n' }));
    await saveMcpWorkspaceConfiguration(route, document, jsonc, quickInput, notifications, signal, checkWorkspace,
      `${serverId} · ${definition.transport === 'plugin' ? t('workbench.mcp.pluginService') : t('workbench.mcp.externalHttp')}`);
    } finally { secrets.clear(); }
  } finally { signal.removeEventListener('abort', abortInput); cancellation.dispose(); }
}
