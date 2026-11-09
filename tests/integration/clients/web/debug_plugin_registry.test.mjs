import { afterAll, expect, test } from 'bun:test';
import { createHash } from 'node:crypto';
import { readFile, writeFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import ts from '../../../../src/clients/workbench/node_modules/typescript/lib/typescript.js';
import { Disposable, toDisposable } from '../../../../src/clients/workbench/upstream/src/vs/base/common/lifecycle.js';
import { Emitter, Event } from '../../../../src/clients/workbench/upstream/src/vs/base/common/event.js';
import { RunOnceScheduler } from '../../../../src/clients/workbench/upstream/src/vs/base/common/async.js';
import { ContextKeyExpr } from '../../../../src/clients/workbench/upstream/src/vs/platform/contextkey/common/contextkey.js';
import { isObject } from '../../../../src/clients/workbench/upstream/src/vs/base/common/types.js';
import { filter } from '../../../../src/clients/workbench/upstream/src/vs/base/common/objects.js';
import { pinnedUpstreamCommit, sourceTransform } from '../../../../src/clients/workbench/scripts/sourceTransform.mjs';
import { TestRunContext } from '../../../harness/js/run-context.mjs';

const run = await TestRunContext.fromTestFile('tests/integration/clients/web/debug_plugin_registry.test.mjs').prepare();
const root = resolve('src/clients/workbench');
const transform = sourceTransform(root, { commit: pinnedUpstreamCommit }, []);
const fingerprints = [];

/** 执行完整实际转换类；仅参数装饰器交由下面具名服务边界装配。 */
async function declaration(relative, name, dependencies) {
  const path = join(root, 'upstream/src/vs/workbench/contrib/debug', relative);
  const original = await readFile(path, 'utf8'), converted = await transform(path, original);
  const parsed = ts.createSourceFile(path, converted, ts.ScriptTarget.Latest, true);
  if (parsed.parseDiagnostics.length) throw new Error(`转换源码不能解析：${relative}`);
  const item = parsed.statements.find(node => node.name?.text === name);
  if (!item) throw new Error(`实际源码缺少声明：${name}`);
  const stripped = ts.transform(item, [context => {
    const visit = node => ts.isDecorator(node) ? undefined : ts.visitEachChild(node, visit, context);
    return node => ts.visitNode(node, visit);
  }]);
  const source = ts.createPrinter().printNode(ts.EmitHint.Unspecified, stripped.transformed[0], parsed).replace(/^export /gm, '');
  stripped.dispose();
  const javascript = ts.transpile(source, { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.None, useDefineForClassFields: false });
  fingerprints.push({ path, original: createHash('sha256').update(original).digest('hex'), converted: createHash('sha256').update(converted).digest('hex') });
  await writeFile(join(run.artifactsDir, relative.replaceAll('/', '__')), converted);
  return new Function(...Object.keys(dependencies), `${javascript}\nreturn ${name};`)(...Object.values(dependencies));
}

const Debugger = await declaration('common/debugger.ts', 'Debugger', {
  ContextKeyExpr, isObject, filter, nls: { localize: (_key, text) => text },
  ConfigurationResolverUtils: { applyDeprecatedVariableMessage() {} },
});
const AdapterManager = await declaration('browser/debugAdapterManager.ts', 'AdapterManager', {
  Disposable, Emitter, Event, RunOnceScheduler, toDisposable, Debugger,
  CONTEXT_DEBUGGERS_AVAILABLE: { bindTo: service => service.createKey('available', false) },
  CONTEXT_DEBUG_EXTENSION_AVAILABLE: { bindTo: service => service.createKey('extensions', false) },
  debuggersExtPoint: { setHandler() {} }, breakpointsExtPoint: { setHandler() {} },
  LifecyclePhase: { Eventually: 4 },
});

function fixture() {
  const calls = [], keys = new Map();
  const context = {
    bufferChangeEvents: action => action(), onDidChangeContext: Event.None, contextMatchesRules: () => true,
    createKey(name, value) { keys.set(name, value); return { set(value) { keys.set(name, value); } }; },
  };
  const instantiation = { createInstance(Type, ...args) {
    expect(Type).toBe(Debugger);
    return new Type(...args, {}, {}, {}, {}, {}, context, {}, {});
  } };
  const manager = new AdapterManager({ onDidNewSession: Event.None }, {}, {}, {}, instantiation, {},
    { activateByEvent: async () => { throw new Error('自有调试器不得激活旧宿主'); } }, context, {}, {},
    { when: () => new Promise(() => {}) }, { onDidChangeTaskConfig: Event.None, onDidChangeTaskProviders: Event.None }, {});
  const factory = {
    createDebugAdapter(session) { calls.push(session); return { owner: session.configuration.pluginDebuggerRegistration }; },
    async substituteVariables(_folder, config) { return config; },
  };
  return { manager, factory, keys, calls };
}

const type = 'boxteam-plugin:example/debugging/node';
const config = registration => ({ type, request: 'launch', name: registration, pluginDebuggerRegistration: registration });
const contribution = (label, property) => ({ type, label, configurationAttributes: {
  launch: { properties: { [property]: { type: 'string' } }, required: [property] },
} });

test('双目标同类型保留独立标签与schema，释放A不撤B的dispatcher', async () => {
  const f = fixture(), handles = [];
  try {
    handles.push(f.manager.registerPluginDebugger('gateway-A:instance-v1', contribution('A v1', 'aOnly'), f.factory));
    handles.push(f.manager.registerPluginDebugger('gateway-B:instance-v2', contribution('B v2', 'bOnly'), f.factory));
    const a = f.manager.getDebugger(type, config('gateway-A:instance-v1'));
    const b = f.manager.getDebugger(type, config('gateway-B:instance-v2'));
    expect(f.manager.getDebuggerLabel(type, config('gateway-A:instance-v1'))).toBe('A v1');
    expect(f.manager.getDebuggerLabel(type, config('gateway-B:instance-v2'))).toBe('B v2');
    expect(a.getMainExtensionDescriptor()).toBeUndefined();
    const aSchema = a.getSchemaAttributes({ common: { properties: {} } })[0];
    const bSchema = b.getSchemaAttributes({ common: { properties: {} } })[0];
    expect(aSchema.properties.aOnly).toEqual({ type: 'string' });
    expect(aSchema.properties.bOnly).toBeUndefined();
    expect(bSchema.properties.bOnly).toEqual({ type: 'string' });
    expect(bSchema.properties.aOnly).toBeUndefined();
    expect(await b.createDebugAdapter({ configuration: config('gateway-B:instance-v2') })).toEqual({ owner: 'gateway-B:instance-v2' });
    handles[0].dispose();
    expect(f.manager.getDebugger(type, config('gateway-A:instance-v1'))).toBeUndefined();
    expect(f.manager.getEnabledDebugger(type, config('gateway-B:instance-v2'))).toBe(b);
    expect(await b.createDebugAdapter({ configuration: config('gateway-B:instance-v2') })).toEqual({ owner: 'gateway-B:instance-v2' });
    handles[1].dispose();
    expect(f.manager.hasEnabledDebuggers()).toBe(false);
    expect(f.manager.createDebugAdapter({ configuration: config('gateway-B:instance-v2') })).toBeUndefined();
  } finally { for (const handle of handles) handle.dispose(); f.manager.dispose(); }
});

test('同一登记的多个session共享声明，最后引用释放才撤销', () => {
  const f = fixture(), handles = [];
  try {
    const value = contribution('A v1', 'aOnly');
    handles.push(f.manager.registerPluginDebugger('A:v1', value, f.factory));
    const original = f.manager.getDebugger(type, config('A:v1'));
    handles.push(f.manager.registerPluginDebugger('A:v1', value, f.factory));
    expect(f.manager.getDebugger(type, config('A:v1'))).toBe(original);
    handles[0].dispose(); handles[0].dispose();
    expect(f.manager.getDebugger(type, config('A:v1'))).toBe(original);
    handles[1].dispose();
    expect(f.manager.getDebugger(type, config('A:v1'))).toBeUndefined();
    expect(f.keys.get('available')).toBe(false);
  } finally { for (const handle of handles) handle.dispose(); f.manager.dispose(); }
});

test('登记或类型不匹配不得选其它目标，第二dispatcher不能覆盖原者', () => {
  const f = fixture();
  const handle = f.manager.registerPluginDebugger('A:v1', contribution('A', 'aOnly'), f.factory);
  try {
    expect(f.manager.getDebugger(type, config('B:v1'))).toBeUndefined();
    expect(f.manager.getDebugger('other', config('A:v1'))).toBeUndefined();
    expect(f.manager.getDebugger(type)).toBeUndefined();
    expect(() => f.manager.registerPluginDebugger('B:v1', contribution('B', 'bOnly'), { ...f.factory })).toThrow('不同 dispatcher');
    expect(f.manager.getDebuggerLabel(type, config('A:v1'))).toBe('A');
    expect(f.manager.getDebugger(type, config('B:v1'))).toBeUndefined();
  } finally { handle.dispose(); f.manager.dispose(); }
});

afterAll(async () => {
  await writeFile(join(run.artifactsDir, 'registry-evidence.json'), JSON.stringify({
    boundary: '实际sourceTransform后的完整AdapterManager与Debugger类；原Disposable/Event/Scheduler/ContextKeyExpr。上下文UI、宿主发现及构造服务为具名边界，不验证浏览器、Gateway或真实双目标网络。',
    fingerprints,
  }, null, 2));
  await run.close();
});
