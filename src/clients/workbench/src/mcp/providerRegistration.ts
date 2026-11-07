import type * as vscode from 'vscode';
import { raceCancellationError } from '../../upstream/src/vs/base/common/async.js';
import { CancellationTokenSource, type CancellationToken } from '../../upstream/src/vs/base/common/cancellation.js';
import { CancellationError, isCancellationError } from '../../upstream/src/vs/base/common/errors.js';
import { Disposable } from '../../upstream/src/vs/base/common/lifecycle.js';
import { generateUuid } from '../../upstream/src/vs/base/common/uuid.js';
import type { McpServerLaunch } from '../../upstream/src/vs/workbench/contrib/mcp/common/mcpTypes.js';
import type { McpProviderSnapshotDTO } from '../../../web/src/types/protocol_generated/boxteam/gateway/v2/public.js';

type ProviderIdentity = Pick<McpProviderSnapshotDTO, 'extension_id' | 'extension_key' | 'collection_id'>;
interface Definition { readonly original: vscode.McpServerDefinition; readonly version: string | undefined }

/** 原 Provider 持有原对象；每次注册／快照均使用 opaque 引用，label 和 version 只作 metadata。 */
export class McpProviderRegistration extends Disposable {
  readonly providerRef = generateUuid();
  readonly ready: Promise<void>;
  private readonly definitions = new Map<string, Definition>();
  private readonly resolving = new Set<CancellationTokenSource>();
  private providing: CancellationTokenSource | undefined;
  private revision = 0;
  private closed = false;

  constructor(private readonly provider: vscode.McpServerDefinitionProvider, private readonly identity: ProviderIdentity,
    private readonly publish: (value: McpProviderSnapshotDTO) => void,
    private readonly invalidate: (providerRef: string) => void,
    private readonly validate: (definition: vscode.McpServerDefinition) => void,
    private readonly convert: (definition: vscode.McpServerDefinition) => McpServerLaunch.Serialized,
    private readonly reportProvideFailure: (error: unknown) => void) {
    super();
    if (provider.onDidChangeMcpServerDefinitions) this._register(provider.onDidChangeMcpServerDefinitions(() => { void this.update(); }));
    this.ready = Promise.resolve().then(() => this.update());
  }

  private revoke(): void {
    this.revision++;
    this.invalidate(this.providerRef);
    this.definitions.clear();
    this.providing?.cancel();
    this.providing?.dispose();
    for (const source of this.resolving) source.cancel();
  }

  private async update(): Promise<void> {
    if (this.closed) return;
    this.revoke();
    const revision = this.revision, source = this.providing = new CancellationTokenSource();
    try {
      const list = await raceCancellationError(Promise.resolve(this.provider.provideMcpServerDefinitions(source.token)), source.token);
      if (this.closed || this.revision !== revision || source.token.isCancellationRequested) return;
      const definitions = (list ?? []).map(original => {
        this.validate(original);
        const definition_ref = generateUuid();
        this.definitions.set(definition_ref, { original, version: original.version });
        return { definition_ref, label: original.label, version: original.version,
          transport: 'uri' in original ? 'streamable_http' : 'stdio' };
      });
      this.publish({ ...this.identity, provider_ref: this.providerRef, kind: 'snapshot', definitions });
    } catch (error) {
      if (this.closed || this.revision !== revision || source.token.isCancellationRequested || isCancellationError(error)) return;
      this.definitions.clear();
      this.reportProvideFailure(error);
      this.publish({ ...this.identity, provider_ref: this.providerRef, kind: 'snapshot', definitions: [], error_code: 'provide_failed' });
    } finally {
      source.dispose();
      if (this.providing === source) this.providing = undefined;
    }
  }

  async resolve(definitionRef: string, token: CancellationToken): Promise<McpServerLaunch.Serialized | undefined> {
    const definition = this.definitions.get(definitionRef), revision = this.revision;
    if (this.closed || !definition || token.isCancellationRequested) throw new CancellationError();
    const source = new CancellationTokenSource(token);
    this.resolving.add(source);
    const check = () => {
      if (this.closed || source.token.isCancellationRequested || this.revision !== revision
        || this.definitions.get(definitionRef) !== definition || definition.original.version !== definition.version) throw new CancellationError();
    };
    try {
      check();
      const resolved = this.provider.resolveMcpServerDefinition
        ? await raceCancellationError(Promise.resolve(this.provider.resolveMcpServerDefinition(definition.original, source.token)), source.token)
        : definition.original;
      check();
      if (!resolved) return undefined;
      this.validate(resolved);
      return this.convert(resolved);
    } finally { this.resolving.delete(source); source.dispose(); }
  }

  override dispose(): void {
    if (this.closed) return;
    this.closed = true;
    this.revoke();
    super.dispose();
  }
}
