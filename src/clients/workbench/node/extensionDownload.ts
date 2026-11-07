import { AsyncLocalStorage } from 'node:async_hooks';
import { CancellationToken } from '../upstream/src/vs/base/common/cancellation.js';
import { getErrorMessage } from '../upstream/src/vs/base/common/errors.js';
import { type IHeaders, type IRequestContext } from '../upstream/src/vs/base/parts/request/common/request.js';
import { IConfigurationService } from '../upstream/src/vs/platform/configuration/common/configuration.js';
import { INativeEnvironmentService } from '../upstream/src/vs/platform/environment/common/environment.js';
import { ILogService } from '../upstream/src/vs/platform/log/common/log.js';
import { asText } from '../upstream/src/vs/platform/request/common/request.js';
import { nodeRequest, type NodeRequestOptions, RequestService } from '../upstream/src/vs/platform/request/node/requestService.js';
import { getResolvedShellEnv } from '../upstream/src/vs/platform/shell/node/shellEnv.js';

interface ExtensionDownloadTransport {
  readonly url: string;
  readonly request_id: string;
}

const downloadTransport = new AsyncLocalStorage<ExtensionDownloadTransport | undefined>();
const downloadCallSites = new Set([
  'extensionGalleryService.download', 'extensionGalleryService.download.fallback',
  'extensionGalleryService.signature', 'extensionGalleryService.signature.fallback',
  'extensionManagement.downloadVsix',
]);
const transportErrorHeader = 'x-boxteam-extension-download-error';

/** 传输能力只属于这次排队安装，不能落盘或被后续安装继承。 */
export function withExtensionDownloadTransport<T>(value: unknown, backendId: string, operation: () => T): T {
  if (value === undefined) return downloadTransport.run(undefined, operation);
  if (typeof value !== 'object' || value === null || !('url' in value) || typeof value.url !== 'string'
    || !('request_id' in value) || typeof value.request_id !== 'string' || !value.request_id
    || /[\r\n\x00]/.test(value.request_id)) {
    throw new TypeError('扩展下载传输必须包含回调 URL 和原 request_id');
  }
  const url = new URL(value.url);
  const authority = value.url.slice('http://'.length).split('/')[0];
  const prefix = `/api/gateway/internal/extension-management/${encodeURIComponent(backendId)}/downloads/`;
  if (!value.url.startsWith('http://') || !/^(?:127\.0\.0\.1|\[::1\])(?::[0-9]+)?$/.test(authority)
    || url.username || url.password || url.search || url.hash || value.url.includes('?') || value.url.includes('#')
    || !url.pathname.startsWith(prefix) || !/^[a-f0-9]{32}$/i.test(url.pathname.slice(prefix.length))
    || value.url.slice(value.url.indexOf('/', 'http://'.length)) !== url.pathname) {
    throw new TypeError('扩展下载回调必须指向当前安装域的具名本机 HTTP 入口');
  }
  return downloadTransport.run({ url: value.url, request_id: value.request_id }, operation);
}

/** 保留原查询、认证和代理 API；只有制品字节改由目标 Python 传输。 */
export class ExtensionDownloadRequestService extends RequestService {
  private shellEnvironmentErrorLogged = false;

  constructor(
    @IConfigurationService private readonly downloadConfiguration: IConfigurationService,
    @INativeEnvironmentService private readonly downloadEnvironment: INativeEnvironmentService,
    @ILogService logService: ILogService,
  ) {
    super('remote', downloadConfiguration, downloadEnvironment, logService);
  }

  override async request(options: NodeRequestOptions, token: CancellationToken): Promise<IRequestContext> {
    if (!downloadCallSites.has(options.callSite)) return super.request(options, token);
    const transport = downloadTransport.getStore();
    if (!transport) throw new Error(`扩展下载缺少本次安装的 Python 传输: ${options.callSite}`);
    if ((options.type ?? 'GET') !== 'GET' || !options.url || !/^https?:$/.test(new URL(options.url).protocol)) {
      throw new TypeError('扩展制品传输只支持 HTTP(S) GET');
    }
    if (options.agent || options.getRawRequest || options.user || options.password || options.proxyAuthorization) {
      throw new Error('扩展制品传输不支持自定义 Node agent 或独立请求认证');
    }
    const localToken = process.env.BOXTEAM_RUNTIME_CONTROL_TOKEN;
    if (!localToken) throw new Error('扩展下载缺少本机管理通道凭据');
    let shellEnv: typeof process.env | undefined;
    try {
      shellEnv = await getResolvedShellEnv(this.downloadConfiguration, this.logService, this.downloadEnvironment.args, process.env);
    } catch (error) {
      // 与原 RequestService 一致：只报告一次 shell 解析失败，并沿原进程环境继续解析代理。
      if (!this.shellEnvironmentErrorLogged) {
        this.shellEnvironmentErrorLogged = true;
        this.logService.error('resolving shell environment failed', getErrorMessage(error));
      }
    }
    const env = { ...process.env, ...shellEnv };
    const proxyUrl = this.downloadConfiguration.getValue<string>('http.proxy')
      || (new URL(options.url).protocol === 'http:' ? env.HTTP_PROXY || env.http_proxy
        : env.HTTPS_PROXY || env.https_proxy || env.HTTP_PROXY || env.http_proxy) || null;
    if (proxyUrl && !/^https?:$/.test(new URL(proxyUrl).protocol)) {
      throw new Error('扩展制品传输只支持 HTTP(S) 代理');
    }
    const strictSSL = !!this.downloadConfiguration.getValue<boolean>('http.proxyStrictSSL');
    const proxyAuthorization = this.downloadConfiguration.getValue<string>('http.proxyAuthorization');
    const headers: IHeaders = {};
    for (const [name, value] of Object.entries(options.headers ?? {})) {
      const lowerName = name.toLowerCase();
      if (value !== undefined && !['x-local-token', 'x-request-id', transportErrorHeader].includes(lowerName)
        && !(proxyAuthorization && lowerName === 'proxy-authorization')) {
        headers[name] = value;
      }
    }
    if (proxyAuthorization) headers['Proxy-Authorization'] = proxyAuthorization;
    if (!proxyUrl && Object.keys(headers).some(name => name.toLowerCase() === 'proxy-authorization')) {
      throw new Error('扩展制品传输配置了代理认证，但没有有效代理');
    }
    return this.logAndRequest({ ...options, logUrl: false }, async () => {
      const context = await nodeRequest({
        type: 'POST', url: transport.url, callSite: options.callSite, followRedirects: 0,
        headers: { 'Content-Type': 'application/json', 'X-Local-Token': localToken, 'X-Request-ID': transport.request_id },
        timeout: options.timeout,
        data: JSON.stringify({ call_site: options.callSite, url: options.url, headers,
          timeout_ms: options.timeout, follow_redirects: options.followRedirects, proxy_url: proxyUrl, strict_ssl: strictSSL }),
      }, token);
      if (context.res.headers[transportErrorHeader] !== undefined) {
        throw new Error(`Python 扩展下载传输失败: ${await asText(context) ?? '缺少错误正文'}`);
      }
      return context;
    });
  }
}
