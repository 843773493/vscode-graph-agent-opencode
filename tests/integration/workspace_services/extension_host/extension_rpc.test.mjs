import { afterAll, expect, test } from 'bun:test';
import { writeFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { build } from '../../../../src/clients/workbench/node_modules/esbuild/lib/main.js';
import { buildSourcePlugin, pinnedUpstreamCommit, sourceTransform } from '../../../../src/clients/workbench/scripts/sourceTransform.mjs';
import { TestRunContext } from '../../../harness/js/run-context.mjs';

const run = await TestRunContext.fromTestFile('tests/integration/workspace_services/extension_host/extension_rpc.test.mjs').prepare();
const root = resolve('src/clients/workbench'), bundle = join(run.artifactsDir, 'rpc.fixture.mjs');
const upstream = './src/clients/workbench/upstream/src/vs/';
await build({ stdin: { resolveDir: resolve('.'), contents: `
export { RPCProtocol } from '${upstream}workbench/services/extensions/common/rpcProtocol.js';
export { createProxyIdentifier } from '${upstream}workbench/services/extensions/common/proxyIdentifier.js';
export { URI } from '${upstream}base/common/uri.js';
export { VSBuffer } from '${upstream}base/common/buffer.js';
export { Emitter } from '${upstream}base/common/event.js';
export { CancellationTokenSource } from '${upstream}base/common/cancellation.js';
` }, outfile: bundle, bundle: true, format: 'esm', platform: 'node', target: 'es2024',
  plugins: [buildSourcePlugin(root, sourceTransform(root, { commit: pinnedUpstreamCommit }, []))],
  tsconfig: join(root, 'tsconfig.jsonc'), logLevel: 'silent' });
const { RPCProtocol, createProxyIdentifier, URI, VSBuffer, Emitter, CancellationTokenSource } = await import(bundle);
const receipt = { boundary: '实际 sourceTransform 后的原 RPC、序列化、ack/error/cancel 与 URI；内存消息传输和归属转换拒绝为具名装具，不代表 Gateway 或真实宿主。', checks: [] };
afterAll(async () => {
  try { await run.close(); }
  finally { await writeFile(join(run.artifactsDir, 'rpc-checks.json'), JSON.stringify(receipt, null, 2) + '\n'); }
});

for (const mixed of [false, true]) for (const cancellable of [false, true]) {
  test(`URI 拒绝沿原 RPC 回复且后续调用与取消保持：mixed=${mixed}, cancel=${cancellable}`, async () => {
    const incoming = [new Emitter(), new Emitter()], sent = [[], []];
    const transport = side => ({ onMessage: incoming[side].event, send(message) {
      sent[side].push(message.buffer[0]);
      queueMicrotask(() => incoming[1 - side].fire(message));
    } });
    const transformer = { transformIncoming(value) {
      if (value.path === '/outside') throw new Error('文件不属于已绑定目标视图');
      return { ...value, path: '/bound' + value.path };
    }, transformOutgoing: value => value, transformOutgoingURI: value => value, transformOutgoingScheme: value => value };
    const client = new RPCProtocol(transport(0)), server = new RPCProtocol(transport(1), null, transformer);
    const cancellation = new CancellationTokenSource();
    const started = Promise.withResolvers();
    const calls = [], id = createProxyIdentifier('ConfigurationRejectionProbe');
    server.set(id, { async $update(uri, ...args) {
      calls.push({ uri, args });
      if (uri.path === '/bound/wait') {
        started.resolve();
        await new Promise(resolve => args.at(-1).onCancellationRequested(resolve));
      }
      return { path: uri.path, cancelled: cancellable ? args.at(-1).isCancellationRequested : false };
    } });
    const invoke = async path => client.getProxy(id).$update(URI.file(path),
      ...(mixed ? [VSBuffer.fromString('正文不应被 URI 拒绝丢失')] : []),
      ...(cancellable ? [cancellation.token] : []));
    try {
      await expect(invoke('/outside')).rejects.toThrow('文件不属于已绑定目标视图');
      expect(calls).toHaveLength(0);
      expect(await invoke('/valid')).toEqual({ path: '/bound/valid', cancelled: false });
      expect(calls).toHaveLength(1);
      if (mixed) expect(calls[0].args[0].toString()).toBe('正文不应被 URI 拒绝丢失');
      if (cancellable) {
        const waiting = invoke('/wait');
        await started.promise;
        cancellation.cancel();
        expect(await waiting).toEqual({ path: '/bound/wait', cancelled: true });
      }
      receipt.checks.push({ mixed, cancellable, rejectedBeforeHandler: true, subsequentCalls: calls.length, sent });
    } finally {
      cancellation.dispose(); client.dispose(); server.dispose(); incoming.forEach(emitter => emitter.dispose());
    }
  });
}
