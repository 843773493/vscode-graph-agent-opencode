import assert from 'node:assert/strict';

export const WORKBENCH_LIFECYCLE_MARKER = 'boxteam-test-native-lifecycle:';

/** 在真实 console 调用前记录 Error 对象身份，仍原样执行每一次错误输出。 */
export function observeNativeShutdown(marker) {
  if (window !== window.top) return;
  const emit = console.debug.bind(console);
  for (const kind of ['beforeunload', 'pagehide']) {
    window.addEventListener(kind, event => {
      emit(marker + JSON.stringify({ kind, trusted: event.isTrusted, topLevel: window === window.top,
        at: Date.now(), url: location.href }));
    }, { capture: true });
  }
  const identities = new WeakMap();
  let nextId = 0;
  const original = console.error;
  const captureStackTrace = Error.captureStackTrace;
  console.error = function observeConsoleError(...args) {
    const callSite = {};
    captureStackTrace(callSite, observeConsoleError);
    const proof = { kind: 'console-call', callStack: callSite.stack,
      topLevel: true, at: Date.now(), url: location.href };
    const error = args[2];
    if (args.length === 3 && args[0] === '%c  ERR' && args[1] === 'color: #f33'
        && error instanceof Error && error.name === 'Canceled' && error.message === 'Canceled'
        && typeof error.stack === 'string') {
      if (!identities.has(error)) identities.set(error, ++nextId);
      Object.assign(proof, { errorObjectId: identities.get(error),
        name: error.name, message: error.message, stack: error.stack });
    }
    emit(marker + JSON.stringify(proof));
    return Reflect.apply(original, this, args);
  };
}

/** 原始 console 位置指向观察器；以同步采集的第一个真实调用帧恢复来源，保留原件。 */
export function originalConsoleLocation(location, proof, identity) {
  if (proof?.kind !== 'console-call' || proof.topLevel !== true || proof.pageId !== identity.pageId
      || proof.documentId !== identity.documentId || proof.observationOrder + 1 !== identity.observationOrder) return location;
  const caller = typeof proof.callStack === 'string' && proof.callStack.split('\n')[1];
  const match = typeof caller === 'string'
    && /^    at (?:[^()]+ \()?((?:https?):\/\/[^\s()]+):(\d+):(\d+)\)?$/.exec(caller);
  if (!match) return { ...location, observerLocation: location, consoleCallObservationOrder: proof.observationOrder,
    callSiteError: 'console 真实调用帧不是 HTTP 资源，保留原始位置及调用栈供审计' };
  const line = Number(match[2]) - 1;
  const column = Number(match[3]) - 1;
  return { ...location, observerLocation: location, consoleCallObservationOrder: proof.observationOrder,
    url: match[1], line, column, lineNumber: line, columnNumber: column };
}

function workbenchBundleOrigin(url) {
  if (!URL.canParse(url)) return null;
  const resource = new URL(url);
  return !resource.search && !resource.hash
    && /^\/out\/(?:main|chunks\/chunk-[\w-]+)\.js$/.test(resource.pathname) ? resource.origin : null;
}

function isExtensionHostDisposeStack(stack, url) {
  const frames = stack.split('\n');
  // 正式 ESM 产物会压缩函数／类名并拆出公共 chunk；原版方法链及同源构建资源仍须逐帧匹配。
  const methods = [/^[$\w]+$/, /^$/, null, /^[$\w]+\.dispose$/,
    /^[$\w]+\.dispose$/, /^[$\w]+\.dispose$/,
    /^[$\w]+\.stopAllInReverse$/, /^async [$\w]+\._doStopExtensionHosts$/];
  const origin = workbenchBundleOrigin(url);
  if (!origin) return false;
  if (frames.length !== methods.length + 1 || frames[0] !== 'Canceled: Canceled') return false;
  return methods.every((method, index) => {
    const frame = frames[index + 1];
    if (method === null) return frame === '    at Array.forEach (<anonymous>)';
    const named = /^    at (.+) \((https?:\/\/[^\s()]+):(\d+):(\d+)\)$/.exec(frame);
    const anonymous = /^    at (https?:\/\/[^\s()]+):(\d+):(\d+)$/.exec(frame);
    return named ? method.test(named[1]) && workbenchBundleOrigin(named[2]) === origin
      : anonymous !== null && method.test('') && workbenchBundleOrigin(anonymous[1]) === origin;
  });
}

export function classifyNativeShutdownLogs(errors, events) {
  const handled = [];
  const consumed = new Set();
  // 固定上游的已知 joiner；关闭日志可在 pagehide 回调内产生，以真实导航或关页完成界定边界。
  const warning = /^%c  ERR color: #f33 \[lifecycle\] Long running operations during shutdown are unsupported in the web \(id: (join\.stopExtensionHosts|workbench\.contrib\.editorChatUsage|join\.chatSessionStore|join\.chatEditingSession)\)$/;
  for (const error of errors) {
    const match = warning.exec(error.text);
    if (!match || error.callSiteError || !Number.isFinite(error.observationOrder) || !URL.canParse(error.url)) continue;
    const key = JSON.stringify([error.pageId, error.documentId, match[1]]);
    if (consumed.has(key)) continue;
    const sameDocument = events.filter(event => event.pageId === error.pageId && event.documentId === error.documentId);
    const before = sameDocument.find(event => event.kind === "beforeunload" && event.trusted === true
      && event.topLevel === true && URL.canParse(event.url)
      && new URL(event.url).origin === new URL(error.url).origin
      && event.observationOrder < error.observationOrder);
    const after = before && sameDocument.find(event => event.kind === "pagehide" && event.trusted === true
      && event.topLevel === true && event.url === before.url && event.at >= before.at
      && event.observationOrder > before.observationOrder);
    const navigated = after && sameDocument.find(event => event.kind === "navigated" && event.url === after.url
      && event.observationOrder > after.observationOrder && event.observationOrder > error.observationOrder);
    // Context.close 不派发 beforeunload；须有真实顶层 pagehide 和关页回执。
    const hidden = sameDocument.find(event => event.kind === "pagehide" && event.trusted === true
      && event.topLevel === true && URL.canParse(event.url)
      && new URL(event.url).origin === new URL(error.url).origin
      && event.observationOrder < error.observationOrder);
    const closed = hidden && sameDocument.find(event => event.kind === "closed" && event.url === hidden.url
      && event.observationOrder > error.observationOrder);
    if (navigated || closed) {
      consumed.add(key);
      handled.push({ consoleIndex: error.index, pageId: error.pageId,
        documentId: error.documentId, joiner: match[1], before, after, navigated, hidden, closed });
    }
  }
  const shutdowns = handled.filter(fact => fact.joiner === 'join.stopExtensionHosts');
  const consumedObjects = new Set();
  for (const error of errors) {
    if (error.callSiteError || !workbenchBundleOrigin(error.url)) continue;
    const proof = events.find(event => event.kind === 'console-call'
      && event.pageId === error.pageId && event.documentId === error.documentId
      && event.observationOrder + 1 === error.observationOrder);
    if (!proof || error.consoleCallObservationOrder !== proof.observationOrder
        || proof.topLevel !== true || !Number.isInteger(proof.errorObjectId) || proof.errorObjectId < 1
        || proof.name !== 'Canceled' || proof.message !== 'Canceled' || typeof proof.stack !== 'string'
        || error.text !== `%c  ERR color: #f33 ${proof.stack}`
        || !isExtensionHostDisposeStack(proof.stack, error.url)) continue;
    const shutdown = shutdowns.find(fact => fact.pageId === error.pageId && fact.documentId === error.documentId
      && (fact.before ?? fact.hidden).url === proof.url
      && new URL(proof.url).origin === new URL(error.url).origin
      && errors.find(candidate => candidate.index === fact.consoleIndex)?.observationOrder < proof.observationOrder
      && (fact.navigated ?? fact.closed).observationOrder > error.observationOrder);
    if (!shutdown) continue;
    const key = JSON.stringify([error.pageId, error.documentId, proof.errorObjectId]);
    if (consumedObjects.has(key)) continue;
    consumedObjects.add(key);
    handled.push({ kind: 'extension-host-rpc-cancellation', consoleIndex: error.index,
      pageId: error.pageId, documentId: error.documentId, errorObjectId: proof.errorObjectId,
      objectObservationOrder: proof.observationOrder, shutdownConsoleIndex: shutdown.consoleIndex });
  }
  return handled;
}


export function runNativeShutdownClassifierSelfTest() {
  const url = "http://127.0.0.1:19000/out/main.js";
  const error = { index: 0, url, pageId: 7, documentId: 8, receivedAt: 120, observationOrder: 20,
    text: "%c  ERR color: #f33 [lifecycle] Long running operations during shutdown are unsupported in the web (id: join.stopExtensionHosts)" };
  const before = { url: "http://127.0.0.1:19000/", pageId: 7, documentId: 8, kind: "beforeunload",
    trusted: true, topLevel: true, at: 100, observationOrder: 10 };
  const after = { ...before, kind: "pagehide", at: 130, receivedAt: 140, observationOrder: 30 };
  const navigated = { ...after, kind: "navigated", observationOrder: 40 };
  const classify = (errors = [error], events = [before, after, navigated]) => classifyNativeShutdownLogs(errors, events);
  assert.equal(classify().length, 1);
  const duringPagehide = { ...error, receivedAt: 141, observationOrder: 35 };
  assert.equal(classify([duringPagehide]).length, 1, "pagehide 内产生的日志以同文档真实导航完成为界，不依赖跨通道毫秒到达顺序");
  assert.equal(classify([error, { ...error, index: 1 }]).length, 1, "同文档同 joiner 重复错误仍须审计");
  for (const events of [[], [before], [after], [before, after], [before, navigated],
    [{ ...before, trusted: false }, after, navigated], [before, { ...after, trusted: false }, navigated],
    [{ ...before, topLevel: false }, after, navigated], [before, { ...after, topLevel: false }, navigated],
    [{ ...before, pageId: 9 }, after, navigated], [before, { ...after, documentId: 9 }, navigated],
    [before, { ...after, url: "http://127.0.0.1:19001/" }, navigated],
    [{ ...before, observationOrder: 21 }, after, navigated], [before, { ...after, at: 99 }, navigated],
    [before, after, { ...navigated, documentId: 9 }], [before, after, { ...navigated, pageId: 9 }],
    [before, after, { ...navigated, observationOrder: 19 }],
    [before, after, { ...navigated, url: "http://127.0.0.1:19001/" }],
  ]) assert.deepEqual(classify([error], events), [], "缺少同文档真实关闭边界不能归为原版关闭日志");
  assert.deepEqual(classify([{ ...error, observationOrder: 41 }]), [], "导航完成后的错误仍须审计");
  assert.deepEqual(classify([{ ...error, observationOrder: undefined }]), []);
  assert.deepEqual(classify([{ ...error, callSiteError: '未知调用来源' }]), []);
  const hidden = { ...after, at: 110, receivedAt: 115, observationOrder: 15 };
  const closed = { pageId: 7, documentId: 8, kind: "closed", url: before.url, receivedAt: 140, observationOrder: 40 };
  assert.equal(classify([error], [hidden, closed]).length, 1, "直接关页必须同时取得顶层 pagehide 和实际关闭回执");
  for (const events of [[hidden], [closed],
    [{ ...hidden, topLevel: false }, closed], [{ ...hidden, trusted: false }, closed],
    [{ ...hidden, pageId: 9 }, closed], [hidden, { ...closed, documentId: 9 }],
    [hidden, { ...closed, url: "http://127.0.0.1:19001/" }],
    [{ ...hidden, observationOrder: 21 }, closed], [hidden, { ...closed, observationOrder: 19 }],
  ]) assert.deepEqual(classify([error], events), [], "缺少精确关页身份、真实事件或顺序时仍须审计");
  assert.equal(classify([error, { ...error, index: 1 }], [hidden, closed]).length, 1);
  for (const text of [error.text.replace("join.stopExtensionHosts", "unknown.joiner"),
    "FileOperationError: IndexedDB database 'vscode-web-db' is closed.", "未预期的业务错误"]) {
    assert.deepEqual(classify([{ ...error, text }]), [], "原版关闭事实不能豁免存储或业务异常");
    assert.deepEqual(classify([{ ...error, text }], [hidden, closed]), []);
  }
  const stack = [
    'Canceled: Canceled', `    at canceled (${url}:344:18)`, `    at ${url}:1335743:26`,
    '    at Array.forEach (<anonymous>)', `    at _RPCProtocol.dispose (${url}:1335740:42)`,
    `    at ExtensionHostManager.dispose (${url}:1336497:24)`,
    `    at ExtensionHostManagerData.dispose (${url}:1338484:24)`,
    `    at ExtensionHostCollection.stopAllInReverse (${url}:1338439:15)`,
    `    at async ExtensionService._doStopExtensionHosts (${url}:1337958:5)`,
  ].join('\n');
  const proof = { pageId: 7, documentId: 8, kind: 'console-call', errorObjectId: 1,
    name: 'Canceled', message: 'Canceled', stack, topLevel: true, url: before.url, observationOrder: 22,
    callStack: `Error\n    at logToConsole (${url}:230:30)` };
  const cancellation = { ...error, index: 1, observationOrder: 23,
    ...originalConsoleLocation({ url: '', line: 34, column: 19 }, proof,
      { pageId: 7, documentId: 8, observationOrder: 23 }), text: `%c  ERR color: #f33 ${stack}` };
  const rpc = (fault = cancellation, object = proof, errors = [error, fault], events = [before, object, after, navigated]) =>
    classifyNativeShutdownLogs(errors, events).filter(fact => fact.kind === 'extension-host-rpc-cancellation');
  assert.equal(rpc().length, 1);
  assert.equal(rpc(cancellation, proof, [error, cancellation], [hidden, proof, closed]).length, 1);
  const chunk = 'http://127.0.0.1:19000/out/chunks/chunk-6HHVFX35.js';
  const bundledStack = stack.replace(`canceled (${url}`, `WW (${chunk}`)
    .replace('_RPCProtocol.dispose', 's.dispose').replace('ExtensionHostManager.dispose', 'C1.dispose')
    .replace('ExtensionHostManagerData.dispose', '$eo.dispose').replace('ExtensionHostCollection.stopAllInReverse', 'Keo.stopAllInReverse')
    .replace('ExtensionService._doStopExtensionHosts', 'NSt._doStopExtensionHosts');
  const bundledProof = { ...proof, stack: bundledStack, callStack: `Error\n    at s.log (${chunk}:32:15781)` };
  const bundledCancellation = { ...cancellation, text: `%c  ERR color: #f33 ${bundledStack}`,
    ...originalConsoleLocation({}, bundledProof, { pageId: 7, documentId: 8, observationOrder: 23 }) };
  assert.equal(rpc(bundledCancellation, bundledProof).length, 1, '正式拆包压缩产物沿同一对象与关闭证据归因');
  for (const changed of [bundledStack.replace('Keo.stopAllInReverse', 'Keo.stop'),
    bundledStack.replace('s.dispose', 's.invoke'), bundledStack.replace(chunk, chunk.replace(':19000', ':19001')),
    bundledStack.replace(chunk, chunk.replace('/out/chunks/', '/business/')),
    bundledStack.replace(`at WW (${chunk}`, `at WW (${chunk}?wrong`),
  ]) {
    assert.deepEqual(rpc({ ...bundledCancellation, text: `%c  ERR color: #f33 ${changed}` },
      { ...bundledProof, stack: changed }), [], '方法链、构建资源与同源身份仍须完整匹配');
  }
  const second = { ...cancellation, index: 2, observationOrder: 25, consoleCallObservationOrder: 24 };
  const secondProof = { ...proof, errorObjectId: 2, observationOrder: 24 };
  assert.equal(rpc(cancellation, proof, [error, cancellation, second],
    [before, proof, secondProof, after, navigated]).length, 2, '不同 pending RPC 必须有不同的实际 Error 身份');
  assert.equal(rpc(cancellation, proof, [error, cancellation, second],
    [before, proof, { ...secondProof, errorObjectId: 1 }, after, navigated]).length, 1,
  '同一个 Error 重复输出仍须失败，不能按相同调用栈放行');
  for (const changed of [{ pageId: 9 }, { documentId: 9 }, { topLevel: false },
    { errorObjectId: 0 }, { errorObjectId: null }, { name: 'Error' }, { message: '业务失败' },
    { url: before.url + '?another-document' }, { observationOrder: 21 }, { stack: undefined },
  ]) assert.deepEqual(rpc(cancellation, { ...proof, ...changed }), [], '身份、邻接顺序和错误原件必须全部匹配');
  for (const stackText of [stack.replace('ExtensionHostCollection.stopAllInReverse', 'BusinessService.stop'),
    stack.replace('_RPCProtocol.dispose', '_RPCProtocol.invoke'), stack.replace(url, url + '?wrong'),
    stack + '\n    at anotherCaller (http://127.0.0.1:19000/out/main.js:1:1)', 'Canceled: Canceled',
    stack.replace('    at Array.forEach (<anonymous>)', '    at Array.map (<anonymous>)'),
    stack.replace(':344:18', ':invalid'),
  ]) assert.deepEqual(rpc({ ...cancellation, text: `%c  ERR color: #f33 ${stackText}` }, { ...proof, stack: stackText }), []);
  assert.deepEqual(rpc({ ...cancellation, url: 'http://127.0.0.1:19001/out/main.js' }), []);
  assert.deepEqual(rpc(cancellation, proof, [cancellation]), [], '没有实际原版关闭 joiner 不得归因');
  assert.deepEqual(rpc(cancellation, proof, [{ ...error, observationOrder: 24 }, cancellation]), []);
  for (const events of [[proof], [before, proof], [before, proof, after], [proof, after, navigated],
    [{ ...before, trusted: false }, proof, after, navigated],
    [before, proof, { ...after, documentId: 9 }, navigated],
    [before, proof, after, { ...navigated, observationOrder: 22 }],
  ]) assert.deepEqual(rpc(cancellation, proof, [error, cancellation], events), []);
  assert.deepEqual(rpc({ ...cancellation, observationOrder: 41 }, { ...proof, observationOrder: 40 }), [],
    '导航完成后即使有对象原件也不能归入关闭日志');
  const raw = { url: '', line: 34, column: 19 };
  const identity = { pageId: 7, documentId: 8, observationOrder: 23 };
  assert.equal(cancellation.url, url);
  assert.equal(cancellation.line, 229);
  assert.equal(cancellation.column, 29);
  assert.deepEqual(cancellation.observerLocation, raw);
  for (const changed of [{ kind: 'another-event' }, { topLevel: false }, { pageId: 9 },
    { documentId: 9 }, { observationOrder: 21 }]) {
    assert.deepEqual(originalConsoleLocation(raw, { ...proof, ...changed }, identity), raw);
  }
  assert.ok(originalConsoleLocation(raw, { ...proof, callStack: 'Error\n    at unknown' }, identity).callSiteError);
}
