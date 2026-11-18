import { requestJson, unwrapApiData } from './http';
import type { APIResponse } from '../types/backend';
import type { CreateSecretRequest, GatewaySecretDTO, GatewaySecretsDTO, SecretMetadataDTO, UpdateSecretRequest } from '../types/protocol_generated/boxteam/gateway/v2/secrets';
import type { GatewayRequestTarget } from './workspaceIdentity';

type MetadataReceipt = GatewaySecretsDTO | GatewaySecretDTO;
const metadataObservers = new Set<{ gatewayId: string; receive: (snapshot: MetadataReceipt) => void }>();

function publishMetadata(snapshot: MetadataReceipt): void {
  for (const observer of metadataObservers) if (observer.gatewayId === snapshot.gateway_id) observer.receive(snapshot);
}

/** 只转发本窗口已取得的公开元数据回执；不缓存、轮询或读取密钥正文。 */
export function observeSecretMetadata(gatewayId: string, receive: (snapshot: MetadataReceipt) => void): () => void {
  const observer = { gatewayId, receive };
  metadataObservers.add(observer);
  return () => { metadataObservers.delete(observer); };
}

/** 密钥值只作为瞬时写入参数；此客户端没有读取明文的接口。 */
export class SecretsClient {
  private readonly target: GatewayRequestTarget;
  constructor(target: GatewayRequestTarget, private readonly signal?: AbortSignal) {
    this.target = { gateway_id: target.gateway_id, gateway_connection_id: target.gateway_connection_id };
  }
  private async request<T extends GatewaySecretsDTO | GatewaySecretDTO>(method: string, suffix = '', body?: unknown): Promise<T> {
    const query = new URLSearchParams(this.target.gateway_connection_id ? { gateway_connection_id: this.target.gateway_connection_id } : {});
    const [path, revision] = suffix.split('?');
    if (revision) for (const [key, value] of new URLSearchParams(revision)) query.set(key, value);
    const result = unwrapApiData(await requestJson<APIResponse<T>>(0,
      '/api/gateway/secrets' + path + (query.size ? '?' + query : ''), {
        method, headers: { 'X-BoxTeam-Target-Gateway-Id': this.target.gateway_id },
        body: body === undefined ? undefined : JSON.stringify(body), cache: 'no-store', signal: this.signal,
      }));
    if (result.gateway_id !== this.target.gateway_id) throw new Error('密钥管理回执不属于原目标');
    if ('items' in result) publishMetadata(result);
    return result;
  }
  list(): Promise<GatewaySecretsDTO> { return this.request('GET'); }
  async create(payload: CreateSecretRequest): Promise<SecretMetadataDTO> {
    const result = await this.request<GatewaySecretDTO>('POST', '', payload);
    if (!result.item || result.item.id !== payload.id) throw new Error('命名密钥创建回执与提交前确定的 ID 不匹配');
    publishMetadata(result);
    return result.item;
  }
  update(id: string, payload: UpdateSecretRequest): Promise<GatewaySecretsDTO> { return this.request('PUT', '/' + encodeURIComponent(id), payload); }
  delete(id: string, revision: number): Promise<GatewaySecretsDTO> {
    return this.request('DELETE', '/' + encodeURIComponent(id) + '?expected_revision=' + revision);
  }
}
