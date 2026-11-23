import { t } from '../../../shared/core/i18n/index.js';
import { SecretsClient } from '../../../web/src/api/secrets.js';
import { HttpRequestError } from '../../../web/src/api/http.js';
import type { GatewayRequestTarget } from '../../../web/src/api/workspaceIdentity.js';
import type { SecretMetadataDTO } from '../../../web/src/types/protocol_generated/boxteam/gateway/v2/secrets.js';
import type { IQuickInputService } from '../../upstream/src/vs/platform/quickinput/common/quickInput.js';
import type { IDialogService } from '../../upstream/src/vs/platform/dialogs/common/dialogs.js';
import { IStorageService, StorageScope, StorageTarget } from '../../upstream/src/vs/platform/storage/common/storage.js';
import { generateUuid } from '../../upstream/src/vs/base/common/uuid.js';
import { createManagementTargetLifecycle, listManagementTargets, managementTargetDisplayName } from '../managementTarget.js';

type Lifetime = ReturnType<typeof createManagementTargetLifecycle>;
const reference = (item: SecretMetadataDTO) => 'secret:' + item.id;
type SecretCreation = { kind: 'create'; id: string };

function creationKey(target: GatewayRequestTarget): string {
  return 'boxteam.secrets.creations.' + JSON.stringify([target.gateway_id, target.gateway_connection_id ?? null]);
}
function pendingCreations(storage: IStorageService, target: GatewayRequestTarget): SecretCreation[] {
  return storage.getObject<SecretCreation[]>(creationKey(target), StorageScope.WORKSPACE, []);
}
function writeCreations(storage: IStorageService, target: GatewayRequestTarget, items: SecretCreation[]): void {
  if (items.length) storage.store(creationKey(target), items, StorageScope.WORKSPACE, StorageTarget.MACHINE);
  else storage.remove(creationKey(target), StorageScope.WORKSPACE);
}
function forgetCreation(storage: IStorageService, target: GatewayRequestTarget, id: string): void {
  writeCreations(storage, target, pendingCreations(storage, target).filter(item => item.id !== id));
}
function isWriteRejected(error: unknown): boolean {
  return error instanceof HttpRequestError && [400, 401, 403, 404, 409, 422].includes(error.status);
}

async function recoverCreation(client: SecretsClient, target: GatewayRequestTarget, id: string,
  quick: IQuickInputService, lifetime: Lifetime, storage: IStorageService): Promise<string | undefined> {
  // 只查询原目标元数据；同 ID 存在证明资源已创建，不重发原写入。
  const { items } = await client.list(); lifetime.check();
  const item = items.find(item => item.id === id);
  if (!item) throw new Error(t('settings.secrets.creationNotFound', id));
  const accepted = await quick.pick([{ label: item.name, description: reference(item),
    detail: t('settings.secrets.revision', item.revision, item.updated_at) }],
  { title: t('settings.secrets.creationRecovered'), placeHolder: t('settings.secrets.confirmRecovered') }, lifetime.token);
  lifetime.check(); if (!accepted) return;
  forgetCreation(storage, target, id);
  return reference(item);
}

async function targetName(target: GatewayRequestTarget, lifetime: Lifetime): Promise<string> {
  const targets = await listManagementTargets(lifetime.signal);
  lifetime.check();
  const selected = targets.find(item => item.gateway_id === target.gateway_id
    && (!target.gateway_connection_id || item.gateway_connection_id === target.gateway_connection_id));
  if (!selected) throw new Error(t('workbench.settings.targetOffline'));
  return managementTargetDisplayName(selected);
}

async function createSecret(client: SecretsClient, target: GatewayRequestTarget, nameOfTarget: string,
  quick: IQuickInputService, lifetime: Lifetime, storage: IStorageService): Promise<string | undefined> {
  const name = await quick.input({ title: t('settings.secrets.create'), prompt: t('settings.secrets.name'),
    validateInput: async value => value.trim() && value.length <= 128 ? undefined : t('configuration.entities.required') }, lifetime.token);
  lifetime.check(); if (name === undefined) return;
  let password = await quick.input({ title: t('settings.secrets.create'), password: true, prompt: t('settings.secrets.target', nameOfTarget),
    validateInput: async value => value.length ? undefined : t('configuration.entities.required') }, lifetime.token);
  try {
    lifetime.check(); if (password === undefined) return;
    const id = 'sec_' + generateUuid().replaceAll('-', '');
    // 展示存储只留原路由和 ID；密码、摘要及请求正文不进入恢复记录。
    writeCreations(storage, target, [...pendingCreations(storage, target), { kind: 'create', id }]);
    await storage.flush();
    lifetime.check();
    let item: SecretMetadataDTO;
    try { item = await client.create({ id, name: name.trim(), value: password }); }
    catch (error) {
      if (isWriteRejected(error)) {
        forgetCreation(storage, target, id);
        throw error;
      }
      throw new Error(`${t('settings.secrets.creationUnknown', id)}\n${error instanceof Error ? error.message : String(error)}`, { cause: error });
    }
    forgetCreation(storage, target, id);
    lifetime.check(); return reference(item);
  } finally { password = undefined; }
}

/** Quick Pick 沿原设置的冻结目标管理；切换范围不会悄悄改写操作归属。 */
export async function chooseProviderCredential(target: GatewayRequestTarget, quick: IQuickInputService, lifetime: Lifetime,
  storage: IStorageService): Promise<string | undefined> {
  const client = new SecretsClient(target, lifetime.signal);
  const name = await targetName(target, lifetime);
  const { items } = await client.list(); lifetime.check();
  const selected = await quick.pick([
    ...pendingCreations(storage, target).map(item => ({ label: t('settings.secrets.queryCreation'),
      description: item.id, action: 'recover:' + item.id })),
    ...items.map(item => ({ label: `$(key) ${item.name}`, description: t('settings.secrets.storedCredential'), action: reference(item) })),
    { label: t('settings.secrets.create'), action: 'create' },
    { label: t('settings.secrets.environment'), action: 'environment' },
    { label: t('configuration.entities.noAuthentication'), action: 'none' },
  ], { title: `${t('configuration.entities.setSecret')} · ${name}`, placeHolder: t('settings.secrets.choose'),
    matchOnDescription: true }, lifetime.token);
  lifetime.check(); if (!selected) return;
  if (selected.action === 'create') return createSecret(client, target, name, quick, lifetime, storage);
  if (selected.action.startsWith('recover:')) return recoverCreation(client, target, selected.action.slice(8), quick, lifetime, storage);
  if (selected.action === 'none') return '';
  if (selected.action !== 'environment') {
    forgetCreation(storage, target, selected.action.slice(7));
    return selected.action;
  }
  const value = await quick.input({ title: t('settings.secrets.environment'), prompt: t('configuration.entities.environmentReference'),
    validateInput: async value => /^(?:[A-Za-z_][A-Za-z0-9_]*|env:[A-Za-z_][A-Za-z0-9_]*|\$\{[A-Za-z_][A-Za-z0-9_]*\})$/.test(value.trim()) ? undefined : t('configuration.entities.environmentRequired') }, lifetime.token);
  lifetime.check();
  if (value === undefined) return;
  const variableName = value.trim().replace(/^env:/, '').replace(/^\$\{(.+)\}$/, '$1');
  return '${' + variableName + '}';
}

export async function manageSecrets(target: GatewayRequestTarget, quick: IQuickInputService, dialogs: IDialogService,
  storage: IStorageService): Promise<void> {
  const lifetime = createManagementTargetLifecycle(t('workbench.settings.reopenSource'));
  const client = new SecretsClient(target, lifetime.signal);
  try {
    const nameOfTarget = await targetName(target, lifetime);
    let snapshot = await client.list();
    while (true) {
      lifetime.check();
      const selected = await quick.pick([
        { label: t('settings.secrets.create'), item: undefined },
        ...pendingCreations(storage, target).map(operation => ({ label: t('settings.secrets.queryCreation'),
          description: operation.id, item: undefined, recoveryId: operation.id })),
        ...snapshot.items.map(item => ({ label: `$(key) ${item.name}`, description: reference(item),
          detail: t('settings.secrets.revision', item.revision, item.updated_at), item })),
      ], { title: t('settings.secrets.manage'), placeHolder: t('settings.secrets.target', nameOfTarget), matchOnDescription: true }, lifetime.token);
      lifetime.check(); if (!selected) return;
      if ('recoveryId' in selected && selected.recoveryId) {
        await recoverCreation(client, target, selected.recoveryId, quick, lifetime, storage);
        snapshot = await client.list();
        continue;
      }
      if (!selected.item) {
        if (await createSecret(client, target, nameOfTarget, quick, lifetime, storage)) snapshot = await client.list();
        continue;
      }
      const item = selected.item;
      const action = await quick.pick([
        { label: t('settings.secrets.replace'), action: 'replace' },
        { label: t('settings.secrets.rename'), action: 'rename' },
        { label: t('settings.secrets.delete'), action: 'delete' },
      ], { title: item.name, placeHolder: reference(item) }, lifetime.token);
      lifetime.check(); if (!action) continue;
      try {
        if (action.action === 'delete') {
          const accepted = await dialogs.confirm({ type: 'warning', message: t('settings.secrets.confirmDelete', item.name),
            detail: t('settings.secrets.deleteDetail'), primaryButton: t('settings.secrets.delete') });
          lifetime.check(); if (!accepted.confirmed) continue;
          snapshot = await client.delete(item.id, item.revision);
        } else if (action.action === 'rename') {
          const name = await quick.input({ title: t('settings.secrets.rename'), value: item.name,
            validateInput: async value => value.trim() && value.length <= 128 ? undefined : t('configuration.entities.required') }, lifetime.token);
          lifetime.check(); if (name === undefined) continue;
          snapshot = await client.update(item.id, { expected_revision: item.revision, name: name.trim() });
        } else {
          let value = await quick.input({ title: t('settings.secrets.replace'), password: true, prompt: t('settings.secrets.target', nameOfTarget),
            validateInput: async value => value.length ? undefined : t('configuration.entities.required') }, lifetime.token);
          try {
            lifetime.check(); if (value === undefined) continue;
            snapshot = await client.update(item.id, { expected_revision: item.revision, name: item.name, value });
          } finally { value = undefined; }
        }
      } catch (error) {
        // 当前状态不证明原更新／删除已成功；禁止自动重放。
        const current = await client.list().catch(readError => { throw new AggregateError([error, readError],
          [error, readError].map(cause => cause instanceof Error ? cause.message : String(cause)).join('\n')); });
        if (isWriteRejected(error)) throw error;
        const latest = current.items.find(entry => entry.id === item.id);
        throw new Error(`${t('settings.secrets.writeUnknown', item.id, latest
          ? t('settings.secrets.revision', latest.revision, latest.updated_at) : t('settings.secrets.currentMissing'))}\n${error instanceof Error ? error.message : String(error)}`, { cause: error });
      }
    }
  } finally { lifetime.dispose(); }
}
