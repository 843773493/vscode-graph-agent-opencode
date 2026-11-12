import type { GatewayConfigSourceDocumentDTO, GatewayConfigSourceEditRequest, GatewayConfigSourcePreflightDTO, GatewayConfigSourcesDTO } from '../../../web/src/types/protocol_generated/boxteam/gateway/v2/public.js';
import type { ConfigSourceDocumentDTO, ConfigSourceEditRequest } from '../../../web/src/types/protocol_generated/boxteam/workspace/v2/public.js';
import type { GatewayWorkspaceRequestTarget } from '../../../web/src/api/workspaceIdentity.js';
import { getConfigSourceDocument, listConfigSources, preflightConfigSource, saveConfigSource } from '../../../web/src/api/configSources.js';
import { watchPluginConfigurationSchemas } from '../../../web/src/api/pluginConfigurationSchemas.js';
import { gateway } from '../agentApi.js';
import type { PluginConfigurationSchemasDTO } from '../../../web/src/types/protocol_generated/boxteam/gateway/v2/service_plugin.js';

export type SourceTarget = Readonly<{ gateway_id: string; gateway_connection_id?: string; workspace_id?: string }>;
export type SourceDocument = GatewayConfigSourceDocumentDTO | ConfigSourceDocumentDTO;
export type SourceIntent = GatewayConfigSourceEditRequest | ConfigSourceEditRequest;
export type SourceBaseline = Pick<SourceIntent, 'base_file_digest' | 'base_layer_revision' | 'base_layer_digest' | 'expected_active_revision' | 'expected_active_digest'>;

export function sourceBaseline(document: SourceDocument): SourceBaseline {
  return { base_file_digest: document.file_digest, base_layer_revision: document.layer_revision, base_layer_digest: document.layer_digest,
    expected_active_revision: document.active_revision, expected_active_digest: document.active_digest };
}

/** 表单与 JSONC 共用原配置 owner；这里只选择传输，不合并配置或判定生效。 */
export class ConfigurationSourceOwner {
  readonly target: SourceTarget;
  constructor(target: SourceTarget) {
    this.target = { gateway_id: target.gateway_id, gateway_connection_id: target.gateway_connection_id,
      workspace_id: target.workspace_id };
  }
  private get workspace(): GatewayWorkspaceRequestTarget {
    return { gateway_id: this.target.gateway_id, workspace_id: this.target.workspace_id!, gateway_connection_id: this.target.gateway_connection_id };
  }
  private path(sourceKey?: string, operation = '', idempotencyKey?: string): string {
    const query = new URLSearchParams();
    if (this.target.gateway_connection_id) query.set('gateway_connection_id', this.target.gateway_connection_id);
    if (idempotencyKey) query.set('idempotency_key', idempotencyKey);
    return '/api/gateway/config/sources' + (sourceKey === undefined ? '' : '/' + encodeURIComponent(sourceKey)) + operation + (query.size ? '?' + query : '');
  }
  private request<T>(path: string, options: RequestInit = {}): Promise<T> {
    return gateway.request<T>(path, { ...options, cache: 'no-store', headers: { 'X-BoxTeam-Target-Gateway-Id': this.target.gateway_id } });
  }
  list(signal?: AbortSignal) {
    return this.target.workspace_id ? listConfigSources(0, this.workspace, signal)
      : this.request<GatewayConfigSourcesDTO>(this.path(), { signal });
  }
  async configurationSchemas(signal?: AbortSignal): Promise<PluginConfigurationSchemasDTO> {
    const { gateway_id, gateway_connection_id, workspace_id } = this.target;
    const schemas = await this.request<PluginConfigurationSchemasDTO>('/api/gateway/service-plugins/configuration-schemas', {
      method: 'POST', body: JSON.stringify({ gateway_id, gateway_connection_id, workspace_id }), signal,
    });
    if (schemas.gateway_id !== this.target.gateway_id || (schemas.workspace_id ?? undefined) !== this.target.workspace_id) {
      throw new Error('配置 schema 回执不属于原目标');
    }
    return schemas;
  }
  watchConfigurationSchemas(signal: AbortSignal, onSnapshot: (snapshot: PluginConfigurationSchemasDTO) => void): Promise<void> {
    return watchPluginConfigurationSchemas(this.target, signal, onSnapshot);
  }
  private document(document: GatewayConfigSourceDocumentDTO, sourceKey: string, key?: string): GatewayConfigSourceDocumentDTO {
    if (document.gateway_id !== this.target.gateway_id || document.source_key !== sourceKey
      || key && document.edit && document.edit.idempotency_key !== key) throw new Error('配置回执不属于冻结目标、来源或原提交');
    return document;
  }
  async read(sourceKey: string, key?: string, signal?: AbortSignal): Promise<SourceDocument> {
    if (this.target.workspace_id) return getConfigSourceDocument(0, this.workspace, sourceKey, { idempotencyKey: key, signal });
    const document = await this.request<GatewayConfigSourceDocumentDTO>(this.path(sourceKey, '', key), { signal });
    return this.document(document, sourceKey, key);
  }
  intent(sourceKey: string, jsonc: string, baseline: SourceBaseline): SourceIntent {
    return { gateway_id: this.target.gateway_id, ...(this.target.workspace_id ? { workspace_id: this.target.workspace_id } : {}),
      source_key: sourceKey, jsonc, ...baseline, idempotency_key: crypto.randomUUID() };
  }
  async preflight(intent: SourceIntent, signal?: AbortSignal) {
    if (this.target.workspace_id) return preflightConfigSource(0, this.workspace, intent as ConfigSourceEditRequest, signal);
    const result = await this.request<GatewayConfigSourcePreflightDTO>(this.path(intent.source_key, '/preflight'), { method: 'POST', body: JSON.stringify(intent), signal });
    if (!result.document) throw new Error('配置预检没有返回原来源文档');
    this.document(result.document, intent.source_key);
    return result;
  }
  async save(intent: SourceIntent, signal?: AbortSignal): Promise<SourceDocument> {
    if (this.target.workspace_id) return saveConfigSource(0, this.workspace, intent as ConfigSourceEditRequest, signal);
    const document = await this.request<GatewayConfigSourceDocumentDTO>(this.path(intent.source_key), { method: 'PUT', body: JSON.stringify(intent), signal });
    this.document(document, intent.source_key, intent.idempotency_key);
    if (!document.edit) throw new Error('配置保存回执没有原提交记录，请查询原幂等键');
    return document;
  }
}

export function sourceStatus(document: SourceDocument): string {
  const status = document.status, edit = document.edit;
  return `来源 ${document.source_key} · ${edit ? `来源${edit.source_saved ? '已保存' : '未保存'} · 层${edit.layer_imported ? '已导入' : '未导入'} · 提交 ${edit.idempotency_key} / ${edit.state} · ` : ''}`
    + `active=${document.active_revision ?? '无'} · pending=${status?.pending_revision ?? '无'} · ${status?.restart_required ? '需要重启' : '无需重启'}`
    + ` · ${status?.reason ?? ''} ${status?.last_error ?? ''}`;
}
