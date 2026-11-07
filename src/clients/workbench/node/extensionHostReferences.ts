import { request } from 'node:http';
import type { IExtensionDescription } from '../upstream/src/vs/platform/extensions/common/extensions.js';

interface ReferenceTransport {
  service: { url: string; backend_id: string; runtime_instance_id: string };
  reference: { generation: string; capability: string };
  gateway_id: string;
  profile_id: string;
  timeout_seconds: number;
}

interface Publication {
  extension_key: string;
  extension_id: string;
  native_uri: string;
}

// 只缓存本进程已经由安装 owner 准入的不可变描述，避免每个 delta 重发旧版本。
const retained = new Map<string, Publication>();
let transport: ReferenceTransport | undefined;

/** 原 Node registry 在首次激活及候选增量发布前提供实际自有集合，绝不解析 RPC 字节。 */
export async function retainNodeExtensionVersions(extensions: readonly IExtensionDescription[]): Promise<void> {
  if (!transport) {
    const encoded = process.env.BOXTEAM_EXTENSION_HOST_REFERENCE;
    if (!encoded) throw new Error('目标 Node 宿主缺少原 Backend 持有的运行引用');
    transport = JSON.parse(encoded) as ReferenceTransport;
    // 启动时转入原运行模块的私有闭包；扩展及其后续子进程不继承引用能力。
    delete process.env.BOXTEAM_EXTENSION_HOST_REFERENCE;
  }
  const binding = transport;
  const mine = extensions.filter(extension => !extension.isBuiltin && !extension.isUnderDevelopment);
  for (const extension of mine) {
    const target = extension.installationTarget;
    if (target?.kind !== 'backend' || target.gatewayId !== binding.gateway_id || target.backendId !== binding.service.backend_id
      || extension.installationProfileId !== binding.profile_id || !extension.extensionKey) {
      throw new Error('Node 运行扩展不属于已认证安装域、profile 或精确发布版本：' + extension.identifier.value);
    }
  }
  const missing = mine.filter(extension => !retained.has(extension.extensionKey!));
  let additions = new Map<string, Publication>();
  if (missing.length) {
    const body = JSON.stringify({ method: 'host-publication', backend_id: binding.service.backend_id,
        runtime_instance_id: binding.service.runtime_instance_id, ...binding.reference,
        profile_id: binding.profile_id, extension_keys: missing.map(extension => extension.extensionKey) });
    // Node 原生请求不携浏览器 Origin／Sec-Fetch 头；仍使用同一具名本机引用入口。
    const response = await new Promise<import('node:http').IncomingMessage>((resolve, reject) => {
      const outgoing = request(binding.service.url, { method: 'POST',
        signal: AbortSignal.timeout(binding.timeout_seconds * 1000),
        headers: { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(body) } }, resolve);
      outgoing.on('error', reject);
      outgoing.end(body);
    });
    const chunks: Buffer[] = [];
    for await (const chunk of response) chunks.push(Buffer.from(chunk));
    if (response.statusCode !== 200) throw new Error('Node 运行版本准入失败：HTTP ' + response.statusCode);
    const reply = JSON.parse(Buffer.concat(chunks).toString('utf8')) as {
      error?: string;
      result?: { publications: Publication[] };
    };
    if (!reply.result) throw new Error(reply.error ?? 'Node 运行版本准入缺少安装 owner 回执');
    additions = new Map(reply.result.publications.map(publication => [publication.extension_key, publication]));
    if (additions.size !== missing.length || missing.some(extension => !additions.has(extension.extensionKey!))) {
      throw new Error('Node 运行版本准入回执与实际候选集合不符');
    }
  }
  for (const extension of mine) {
    const publication = additions.get(extension.extensionKey!) ?? retained.get(extension.extensionKey!)!;
    if (publication.extension_id.toLowerCase() !== extension.identifier.value.toLowerCase()
      || publication.native_uri !== extension.extensionLocation.toString()) {
      throw new Error('Node 扩展描述与原安装 owner 冻结的包位置不符：' + extension.identifier.value);
    }
  }
  for (const [key, publication] of additions) retained.set(key, publication);
}
