import { Emitter } from '../../upstream/src/vs/base/common/event.js';
import { Disposable } from '../../upstream/src/vs/base/common/lifecycle.js';
import { deletePortForward, listPortForwardHosts, listPortForwards, savePortForward, setPortForwardRunning } from '../../../web/src/gatewayApi.js';
import type { CreateGatewayPortForwardRequest, GatewayPortForward } from '../../../web/src/types/backend.js';
import type { PortForwardHostsDTO } from '../../../web/src/types/gatewayProtocol.js';
import { errorMessage } from '../../../web/src/utils/errorMessage.js';
import { parsePortLiteral } from '../../../web/src/components/workspace/portLiteral.js';
import { t } from '../../../shared/core/i18n/index.js';

export const portApi = { list: listPortForwards, hosts: listPortForwardHosts, save: savePortForward, setRunning: setPortForwardRunning, remove: deletePortForward };
export type PortApi = typeof portApi;
export type Field = 'label' | 'listenerHost' | 'listenerAddress' | 'listenerPort' | 'destinationHost' | 'destinationAddress' | 'destinationPort' | 'protocol';
export const editableFields: readonly Field[] = ['label', 'listenerHost', 'listenerAddress', 'listenerPort', 'destinationHost', 'destinationAddress', 'destinationPort', 'protocol'];
export const NEW = 'new-forward';
export const ADD = 'add-forward';
export interface Row { id: string }
export type Draft = Record<Field, string>;
export function draftOf(item: GatewayPortForward): Draft {
  return { label: item.label ?? '', listenerHost: item.listener.host_id, listenerAddress: item.listener.address,
    listenerPort: String(item.listener.port), destinationHost: item.destination.host_id,
    destinationAddress: item.destination.address, destinationPort: String(item.destination.port), protocol: item.protocol };
}

/** 只保存后端镜像和本窗口的未提交编辑，不生成第二份持久业务状态。 */
export class PortModel extends Disposable {
  private readonly changed = this._register(new Emitter<void>());
  readonly onDidChange = this.changed.event;
  items: GatewayPortForward[] = [];
  hosts: PortForwardHostsDTO['items'] = [];
  draft?: Draft;
  readonly busy = new Set<string>();
  readonly errors = new Map<string, string>();
  loading = false;
  loadError = '';
  private disposed = false;
  private readSequence = 0;
  private writing: Promise<void> = Promise.resolve();

  constructor(private readonly api: PortApi = portApi) { super(); }
  get rows(): Row[] { return [...this.items.map(item => ({ id: item.forward_id })), ...(this.draft ? [{ id: NEW }] : []), { id: ADD }]; }
  item(id: string): GatewayPortForward | undefined { return this.items.find(item => item.forward_id === id); }
  values(id: string): Draft { return id === NEW ? this.draft! : draftOf(this.item(id)!); }
  error(id: string): string { return this.errors.get(id) ?? this.item(id)?.error ?? ''; }
  hostLabel(id: string): string { return this.hosts.find(host => host.host_id === id)?.label ?? id; }

  async refresh(): Promise<void> {
    if (this.busy.size) return;
    const sequence = ++this.readSequence;
    this.loading = true; this.changed.fire();
    const results = await Promise.allSettled([
      this.api.list(0).then(receipt => { if (!this.disposed && sequence === this.readSequence) { this.items = receipt.items; this.changed.fire(); } }),
      this.api.hosts(0).then(receipt => { if (!this.disposed && sequence === this.readSequence) this.hosts = receipt.items; }),
    ]);
    if (this.disposed || sequence !== this.readSequence) return;
    this.loadError = results.filter(result => result.status === 'rejected').map(result => errorMessage(result.reason)).join('\n');
    this.loading = false; this.changed.fire();
  }

  add(): void {
    this.draft ??= { label: '', listenerHost: 'local', listenerAddress: '127.0.0.1', listenerPort: '',
      destinationHost: 'local', destinationAddress: '127.0.0.1', destinationPort: '', protocol: 'tcp' };
    this.changed.fire();
  }
  cancelNew(): void { this.draft = undefined; this.errors.delete(NEW); this.changed.fire(); }

  async edit(id: string, field: Field, value: string): Promise<void> {
    if (id === NEW) { this.draft![field] = value; this.errors.delete(id); this.changed.fire(); return; }
    if (this.values(id)[field] === value) return;
    const values = { ...this.values(id), [field]: value };
    await this.run(id, () => this.api.save(0, this.payload(values, this.item(id)!.enabled), id));
  }
  async saveNew(start: boolean): Promise<void> {
    await this.run(NEW, async () => {
      const receipt = await this.api.save(0, this.payload(this.draft!, start));
      this.draft = undefined;
      return receipt;
    });
  }
  async toggle(id: string): Promise<void> {
    if (id === NEW) return this.saveNew(true);
    return this.run(id, () => this.api.setRunning(0, id, this.item(id)!.status !== 'active'));
  }
  async restart(id: string): Promise<void> { await this.run(id, () => this.api.setRunning(0, id, true)); }
  async remove(id: string): Promise<void> {
    if (id === NEW) { this.cancelNew(); return; }
    await this.run(id, () => this.api.remove(0, id));
  }

  private payload(values: Draft, enabled: boolean): CreateGatewayPortForwardRequest {
    if (!this.hosts.some(host => host.host_id === values.listenerHost) || !this.hosts.some(host => host.host_id === values.destinationHost)) throw new Error(t('hostPorts.unavailable'));
    return { label: values.label.trim() || null, protocol: values.protocol as CreateGatewayPortForwardRequest['protocol'], enabled,
      listener: { host_id: values.listenerHost, address: values.listenerAddress.trim(), port: parsePortLiteral(values.listenerPort, t('hostPorts.listenPort')) },
      destination: { host_id: values.destinationHost, address: values.destinationAddress.trim(), port: parsePortLiteral(values.destinationPort, t('hostPorts.servicePort')) } };
  }

  private async run(id: string, operation: () => ReturnType<PortApi['list']>): Promise<void> {
    if (this.busy.has(id)) return;
    this.busy.add(id); this.errors.delete(id); ++this.readSequence; this.loading = false; this.changed.fire();
    const previous = this.writing;
    let release!: () => void;
    this.writing = new Promise<void>(resolve => { release = resolve; });
    await previous;
    try {
      // 按提交顺序消费完整回执，普通成功操作不追加读请求。
      const receipt = await operation();
      if (!this.disposed) this.items = receipt.items;
    } catch (cause) {
      let detail = errorMessage(cause);
      try { const receipt = await this.api.list(0); if (!this.disposed) this.items = receipt.items; }
      catch (readError) { detail += `\n${errorMessage(readError)}`; }
      if (!this.disposed) this.errors.set(id, detail);
    } finally { release(); this.busy.delete(id); if (!this.disposed) this.changed.fire(); }
  }
  override dispose(): void { this.disposed = true; ++this.readSequence; super.dispose(); }
}
