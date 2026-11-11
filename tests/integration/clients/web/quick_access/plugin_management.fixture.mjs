import { Emitter, Event } from '../../../../../src/clients/workbench/upstream/src/vs/base/common/event.js';
import { CancellationTokenSource } from '../../../../../src/clients/workbench/upstream/src/vs/base/common/cancellation.js';
import { CommandsRegistry } from '../../../../../src/clients/workbench/upstream/src/vs/platform/commands/common/commands.js';
import { IQuickInputService } from '../../../../../src/clients/workbench/upstream/src/vs/platform/quickinput/common/quickInput.js';
import { ManagePluginQuickAccessProvider, InstallPluginQuickAccessProvider } from '../../../../../src/clients/workbench/src/extensions/quickAccess.js';
import { selectExtensionManagementTargetCommand } from '../../../../../src/clients/workbench/src/managementTarget.js';
import { workspaceContext } from '../../../../../src/clients/workbench/src/workspaceContext.js';
import { invalidateGatewayToken } from '../../../../../src/clients/web/src/api/http.js';
import { installSessionApiFetchMock, unwrapSessionApiFetch } from '../../../../../src/clients/web/src/api/session/sessionApiFetchMock.js';

/** 真实 Provider 和目标选择，只有库存 HTTP 与原管理视图接缝使用显式替身。 */
export async function pluginManagementContract(mode) {
  const previous = workspaceContext.getSnapshot(), changes = new Emitter(), accepts = new Emitter(), cancellation = new CancellationTokenSource();
  const initial = { ...previous, gatewayId: 'entry-gateway', loading: false, error: null };
  workspaceContext.publish(initial);
  const requests = [], opened = [], commands = [], errors = [];
  let releaseRead, aborted = false;
  const ok = data => Response.json({ code: 0, request_id: 'plugin-quick-contract', data }, { headers: { 'X-Request-ID': 'plugin-quick-contract' } });
  installSessionApiFetchMock(({ path, method, init }) => {
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    requests.push({ path, method, body });
    if (path === '/api/gateway/management/gateways') return ok({ local_gateway_id: initial.gatewayId,
      items: [{ gateway_id: 'target-gateway', name: '远端目标', connection_kind: 'remote_gateway', gateway_connection_id: 'connection-target' }] });
    if (path !== '/api/gateway/service-plugins/inventory') throw new Error(`非预期插件请求：${method} ${path}`);
    const response = () => mode === 'failed' ? Response.json({ detail: '原安装域不可达' }, { status: 503 })
      : ok({ packages: ['1.0.0', '2.0.0'].map(version => ({ manifest: {
        package_id: 'proof.same-package', name: '同名包', version,
        plugins: [{ id: 'same-plugin', name: 'Needle 同名插件', instance_scope: version === '1.0.0' ? 'gateway' : 'workspace' }],
      } })), sources: { entries: [], message: '插件源尚未配置' } });
    if (mode.startsWith('pending-')) {
      init.signal.addEventListener('abort', () => { aborted = true; });
      return new Promise(resolve => { releaseRead = () => resolve(response()); });
    }
    return response();
  });
  const select = async () => CommandsRegistry.getCommand(selectExtensionManagementTargetCommand).handler({ get(id) {
    if (id !== IQuickInputService) throw new Error(`目标选择意外访问服务：${id}`);
    return { async pick(items) { return items[0]; } };
  } });
  await select();
  const installing = mode.startsWith('install');
  const provider = new (installing ? InstallPluginQuickAccessProvider : ManagePluginQuickAccessProvider)(
    { executeCommand: async (...args) => { commands.push(args); } },
    { error: error => errors.push(String(error)) },
    { async openView(id) { return { async openPlugin(...args) { opened.push({ id, args }); } }; } },
  );
  const picker = { value: installing ? 'ext install ' : 'ext Needle', items: [], activeItems: [], selectedItems: [], buttons: [],
    keyMods: { alt: false, ctrlCmd: false }, onDidChangeValue: changes.event, onDidAccept: accepts.event,
    onDidTriggerItemButton: Event.None, onDidTriggerSeparatorButton: Event.None, hide() {} };
  const lifetime = provider.provide(picker, cancellation.token);
  const until = async predicate => {
    for (let i = 0; i < 300; i++) { if (predicate()) return; await new Promise(resolve => setTimeout(resolve, 5)); }
    throw new Error(`插件快速访问未收敛：${JSON.stringify({ requests, errors, items: picker.items.map(item => item.label) })}`);
  };
  try {
    if (mode.startsWith('pending-')) {
      await until(() => releaseRead);
      if (mode === 'pending-close') lifetime.dispose(); else invalidateGatewayToken(0);
      releaseRead(); await new Promise(resolve => setTimeout(resolve, 20));
    } else {
      await until(() => !picker.busy && picker.items.length);
      const candidate = picker.items.at(-1);
      if (mode === 'filter') { picker.value = 'ext 2.0.0'; changes.fire(picker.value); await until(() => !picker.busy && picker.items.length === 1); }
      if (mode === 'open' || mode === 'identity' || installing) {
        if (mode === 'identity' || mode === 'install-identity') invalidateGatewayToken(0);
        try { candidate.accept(picker.keyMods, { inBackground: false }); }
        catch (error) { errors.push(String(error)); }
        await until(() => opened.length || commands.length || errors.length);
      }
    }
    return { requests, opened, commands, errors, aborted, placeholder: picker.placeholder,
      items: picker.items.map(item => ({ id: item.id, label: item.label, detail: item.detail, actionable: !!item.accept })) };
  } finally {
    lifetime.dispose(); cancellation.dispose(true); provider.dispose(); changes.dispose(); accepts.dispose();
    unwrapSessionApiFetch(); workspaceContext.publish(previous);
  }
}
