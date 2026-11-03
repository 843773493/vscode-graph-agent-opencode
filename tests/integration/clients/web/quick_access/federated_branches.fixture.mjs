import { querySearchBranches } from '../../../../../src/clients/workbench/src/search/quickAccessBranches.js';
import { workspaceContext } from '../../../../../src/clients/workbench/src/workspaceContext.js';
import { DisposableStore } from '../../../../../src/clients/workbench/upstream/src/vs/base/common/lifecycle.js';
import { CancellationTokenSource } from '../../../../../src/clients/workbench/upstream/src/vs/base/common/cancellation.js';

/** 真实名称／正文分支与共享 SSE 消费器，只有 Gateway 网络响应为显式装具。 */
export function contentBranchContract(mode) { return federatedBranchContract(mode, '会话消息'); }
export function nameBranchContract({ mode, category }) { return federatedBranchContract(mode, category); }

async function federatedBranchContract(mode, category) {
  const originalFetch = globalThis.fetch, before = workspaceContext.getSnapshot();
  const names = { '会话消息': 'message', '会话': 'catalog', 'Thread': 'thread' };
  const content = category === '会话消息', operation = content ? 'content' : 'name';
  const owners = ['A', 'B'].map(workspaceId => ({ gatewayId: 'gateway-a', workspaceId }));
  const frozen = { version: 1, gateway_id: 'gateway-a', resolved_at: '2026-11-03T00:00:00Z', mode: 'selected',
    owners: owners.map(owner => ({ gateway_id: owner.gatewayId, workspace_id: owner.workspaceId })) };
  const lifetime = new DisposableStore(), token = new CancellationTokenSource(), connections = [], paths = [];
  workspaceContext.publish({ ...before, gatewayId: 'gateway-a', workspaces: owners.map(owner => ({
    workspace_id: `route-${owner.workspaceId}`, backend_workspace_id: owner.workspaceId, connection_kind: 'local', name: `名称 ${owner.workspaceId}`,
  })) });
  globalThis.fetch = async (url, init) => {
    const path = new URL(String(url), location.origin).pathname; paths.push(path);
    if (path === '/api/gateway/auth/local-credential') return Response.json({ data: { token: 'content-test' } });
    if (path !== '/api/gateway/search') throw new Error(`正文搜索出现额外请求：${path}`);
    if (new Headers(init.headers).get('Content-Type') !== 'application/json') throw new Error('搜索请求缺少 JSON Content-Type');
    const connection = { signal: init.signal, request: JSON.parse(init.body), sequence: 0, cancelled: false, id: `request-${connections.length}` };
    connections.push(connection);
    const response = new Response(new ReadableStream({ start(controller) { connection.controller = controller; }, cancel() { connection.cancelled = true; } }),
      { headers: { 'Content-Type': 'text/event-stream', 'X-Request-ID': connection.id } });
    if (mode === 'late') return new Promise(resolve => { connection.release = () => resolve(response); });
    return response;
  };
  const until = async predicate => {
    for (let count = 0; count < 200; count++) { if (predicate()) return; await new Promise(resolve => setTimeout(resolve, 5)); }
    throw new Error('正文分支条件未收敛');
  };
  const send = (connection, type, payload) => {
    const sequence = ++connection.sequence;
    connection.controller.enqueue(new TextEncoder().encode(`id: ${sequence}\nevent: search\ndata: ${JSON.stringify({ sequence, request_id: connection.id, type, ...payload })}\n\n`));
  };
  const scope = connection => send(connection, 'scope', { scope: { scope: frozen,
    branches: frozen.owners.map(owner => ({ owner, state: 'registered', target_id: owner.workspace_id })) } });
  const state = (index, status = 'complete') => ({ branch: { owner: frozen.owners[index], category: names[category], operation },
    state: status, retryable: status === 'failed', allocated_results: 50, returned_results: status === 'failed' ? 0 : 1,
    ...(status === 'limited' ? { code: 'result_limit', cursor: 'A-original-cursor' } : {}),
    ...(status === 'failed' ? { code: 'upstream_error', message: 'B 原始读取失败' } : {}) });
  const page = (connection, index, start, status = 'complete') => {
    const cursor = status === 'limited' ? 'A-original-cursor' : null;
    send(connection, 'page', { branch: state(index, status), page: { provider_id: 'canonical-history', workspace_id: owners[index].workspaceId,
      category: names[category], operation, contract_version: 1, state: status,
      ...(status === 'limited' ? { limited_reason: 'result_limit' } : {}),
      ...(content ? { messages: { catalog_revision: 1, cursor, complete: status === 'complete', limited_reason: cursor ? 'result_limit' : null,
        scanned_sessions: 1, scanned_threads: 1, scanned_items: 1, scanned_bytes: 5,
        items: [{ thread: { workspace_id: owners[index].workspaceId, session_id: 'session', thread_id: 'thread',
          thread_uri: 'boxteam://session/session', kind: 'child', title: '同名 Thread', session_title: '同名 Session', created_at: '2026-11-03T00:00:00Z' },
          match: { turn_id: 'turn', message_id: 'same-message', role: 'user', preview: '相同😀命中', preview_start_codepoint: 0,
            reference: { item_id: 'same-item', content_hash: 'same-hash', start_codepoint: start, end_codepoint: start + 2, category: 'message' } } }] } }
        : category === '会话' ? { catalog: { revision: 'r1', catalog_revision: 1, cursor, total: 2, items: [
          { node: { node_id: 'same-session', kind: 'session', name: `needle Session ${start}`, session_id: 'same-session',
            has_children: false, node_revision: 1, state: 'active' }, breadcrumb: [], relative_path: 'needle Session' },
          { node: { node_id: 'same-folder', kind: 'folder', name: `needle Folder ${start}`, folder_id: 'same-folder',
            has_children: true, node_revision: 1, state: 'active' }, breadcrumb: [], relative_path: 'needle Folder' },
        ] } } : { threads: { catalog_revision: 1, cursor, complete: status === 'complete', limited_reason: cursor ? 'result_limit' : null,
          scanned_sessions: 1, scanned_threads: 1, items: [{ workspace_id: owners[index].workspaceId,
            session_id: 'same-session', thread_id: `same-child-${start}`, thread_uri: 'boxteam://session/same-session',
            kind: 'child', title: `needle Child ${start}`, session_title: 'needle Session', created_at: '2026-11-03T00:00:00Z' }] } }),
    } });
    send(connection, 'branch', { branch: state(index, status) });
  };
  const end = (connection, branches) => {
    send(connection, 'end', { summary: { state: branches.every(branch => branch.state === 'complete') ? 'complete' : 'incomplete',
      order: 'category-scope-owner-v1', returned_results: branches.reduce((count, branch) => count + branch.returned_results, 0), branches } });
    connection.controller.close();
  };
  let busy = false, updates = 0, label;
  try {
    const branches = querySearchBranches(owners, [category], content ? '😀命中' : 'needle', lifetime, token.token, {}, {}, {},
      (item, owner) => ({ id: content ? `${owner.workspaceId}/${item.message.reference.start_codepoint}` : `${owner.workspaceId}/${item.kind}/${item.title}`, label: item.title, accept() {} }),
      () => updates++, value => { busy = value; }, value => { label = value; });
    await until(() => connections.length === 1); const first = connections[0]; scope(first);
    if (mode === 'cancel' || mode === 'late') {
      const count = updates; lifetime.dispose();
      if (mode === 'late') { page(first, 0, 2); page(first, 1, 2); end(first, [state(0), state(1)]); first.release(); }
      if (mode === 'late') await new Promise(resolve => setTimeout(resolve, 20));
      else await until(() => first.cancelled);
      return { cancelled: first.signal.aborted, unchanged: count === updates, calls: connections.length, paths };
    }
    if (mode === 'disconnect') {
      page(first, 0, 2); first.controller.close();
      await until(() => !busy);
      return { labels: branches.map(branch => branch.status.label), details: branches.map(branch => branch.status.detail),
        picks: branches.map(branch => branch.picks.map(pick => pick.id)) };
    }
    page(first, 0, 2, 'limited'); await until(() => !branches[0].pending);
    const originalPick = branches[0].picks[0];
    const continuing = branches[0].status.trigger();
    await until(() => connections.length === 2); const next = connections[1]; scope(next);
    send(first, 'branch', { branch: state(1, 'failed') }); end(first, [state(0, 'limited'), state(1, 'failed')]);
    await until(() => !branches[1].pending); await new Promise(resolve => setTimeout(resolve, 5));
    const pendingPreserved = branches[0].pending, partialFailure = branches[1].status.label;
    page(next, 0, 4); end(next, [state(0)]); await continuing;
    const retry = branches[1].status.trigger();
    await until(() => connections.length === 3); const last = connections[2]; scope(last); page(last, 1, 2); end(last, [state(1)]); await retry;
    return { requests: connections.map(connection => connection.request), paths, pendingPreserved, partialFailure,
      originalPreserved: originalPick === branches[0].picks[0], picks: branches.map(branch => branch.picks.map(pick => pick.id)), busy, label,
      status: branches.map(branch => branch.status.detail), highlights: branches.map(branch => branch.picks.map(pick => pick.highlights)) };
  } finally { lifetime.dispose(); token.dispose(true); globalThis.fetch = originalFetch; workspaceContext.publish(before); }
}
