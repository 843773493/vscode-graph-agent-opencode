import { Emitter, Event } from '../../../../../../src/clients/workbench/upstream/src/vs/base/common/event.js';
import { CancellationTokenSource } from '../../../../../../src/clients/workbench/upstream/src/vs/base/common/cancellation.js';
import { ResourceQuickAccess } from '../../../../../../src/clients/workbench/src/resources/quickAccess.js';
import { ResourceOpener } from '../../../../../../src/clients/workbench/src/resourceOpener.js';
import { SourceDocuments, sourceDocumentSource, sourceDocumentUri } from '../../../../../../src/clients/workbench/src/sourceDocuments.js';
import { resourceReference } from '../../../../../../src/clients/workbench/src/resourceActions.js';
import { workspaceContext } from '../../../../../../src/clients/workbench/src/workspaceContext.js';
import { gatewayWorkspaceKey } from '../../../../../../src/clients/web/src/api/workspaceIdentity.js';
import { invalidateGatewayUserSession } from '../../../../../../src/clients/web/src/api/http.js';
import { installSessionApiFetchMock, unwrapSessionApiFetch } from '../../../../../../src/clients/web/src/api/session/sessionApiFetchMock.js';

// 固定 HTTP 回执只证明客户端合同；公开 URI 的真实生成和语法由后端及页面另核。
const owners = ['a', 'b', 'c', 'd'].map(letter => ({ gatewayId: `gateway_${letter.repeat(32)}`, workspaceId: '019a0000-0000-7000-8000-000000000001' }));
const agentsUri = `boxteam://workspace/${owners[0].workspaceId}/resources/agent-spec/root/AGENTS.md`;
const skillUri = id => `boxteam://workspace/${owners[0].workspaceId}/resources/skills/http_${id}/SKILL.md`;

export async function skillSourcesContract(mode) {
  const previous = workspaceContext.getSnapshot(), changes = new Emitter(), accepts = new Emitter(), buttons = new Emitter();
  const cancellation = new CancellationTokenSource(), requests = [], opened = [], references = [], errors = [], shows = [], snapshots = [], documents = [];
  const selected = mode === 'parallel' ? owners : owners.slice(0, 2);
  const targets = selected.map((owner, i) => ({ gateway_id: owner.gatewayId, workspace_id: owner.workspaceId, name: `工作区 ${i}`, status: 'ready' }));
  workspaceContext.publish({ ...previous, gatewayId: owners[0].gatewayId, loading: false, error: null,
    workspaces: targets, activeId: gatewayWorkspaceKey(owners[0]) });
  let provider, lifetime, picker, round = 0, active = 0, maximum = 0, aborted = 0, released = 0, releaseRead, releaseParse, parallelReleased = false;
  const parallel = [], ok = data => Response.json({ code: 0, request_id: 'skill-source-contract', data }, { headers: { 'X-Request-ID': 'skill-source-contract' } });
  installSessionApiFetchMock(({ path, method, init }) => {
    const headers = new Headers(init?.headers), gateway = headers.get('X-BoxTeam-Target-Gateway-Id');
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    requests.push({ path, method, gateway, body, round });
    if (path === '/api/gateway/workspaces') { round++; return ok({ items: targets, active_workspace: targets[0] }); }
    if (path === '/api/gateway/resource-addresses/parse') {
      const owner = mode === 'gateway-skill-owner-mismatch' ? owners[0] : owners.find(item => item.gatewayId === body.resource_gateway_id) ?? owners[1];
      const gatewayScope = body.uri.startsWith('boxteam://gateway/');
      const result = () => ok({ gateway_id: owner.gatewayId, scope: gatewayScope ? 'gateway' : 'workspace', scope_id: gatewayScope ? owner.gatewayId : owner.workspaceId,
        kind: mode === 'gateway-skill-copy-wrong-kind' ? 'config' : 'skills', uri: body.uri, reference_uri: body.uri.replace('boxteam://', `boxteam://${owner.gatewayId}/`) });
      if (mode === 'gateway-skill-copy-wrong-owner') return ok({ gateway_id: owners[0].gatewayId, scope: 'gateway', scope_id: owners[0].gatewayId, kind: 'skills' });
      if (mode === 'freeze') return new Promise(resolve => { releaseParse = () => resolve(result()); });
      return result();
    }
    if (path === '/api/v1/resources/navigation' && mode === 'gateway-skill-relay-open') {
      return ok({ node: { uri: body.uri, resource_shape: 'file' }, content: 'Relay plugin body', content_encoding: 'utf-8', media_type: 'text', truncated: false });
    }
    if (path === '/api/v1/resources') return ok({ workspace_id: owners[0].workspaceId, items: [] });
    if (!['/api/v1/resources/agent-spec', '/api/v1/resources/skill-sources', '/api/v1/resources/local-skills', '/api/v1/resources/gateway-skills',
      '/api/gateway/config/sources', '/api/v1/config/sources'].includes(path)) throw new Error(`非预期 Skill 请求: ${method} ${path}`);
    const configuration = path.endsWith('/config/sources');
    const gatewaySkill = path.endsWith('/gateway-skills');
    const first = gateway === owners[0].gatewayId, skill = path.endsWith('skill-sources'), local = path.includes('local-skills'), currentRound = round;
    const result = () => {
      if (configuration) return ok({ sources: [] });
      if (first && (mode === 'fail-skill' && skill || mode === 'fail-agent' && !skill && !local && !gatewaySkill || mode === 'fail-local' && local)) return Response.json({ detail: '具名来源边界失败', request_id: 'source-failure-id' },
        { status: 503, headers: { 'X-Request-ID': 'source-failure-id' } });
      if (gatewaySkill) {
        if (mode === 'fail-gateway-skill' && first) return Response.json({ detail: '具名来源边界失败', request_id: 'source-failure-id' },
          { status: 403, headers: { 'X-Request-ID': 'source-failure-id' } });
        return ok({ gateway_id: mode === 'gateway-skill-mismatch' && first ? owners[1].gatewayId : gateway,
          workspace_id: owners[0].workspaceId, revision: `revision-${currentRound}`, available: !(mode === 'gateway-skill-publication-unavailable' && first),
          error_code: mode === 'gateway-skill-publication-unavailable' && first ? 'source-unavailable' : null,
          entries: mode.startsWith('gateway-skill') ? [false, true].map(plugin => ({
            name: `Gateway 同名 ${currentRound}`, display_uri: `boxteam://gateway/${gateway}/resources/skills/${plugin ? 'plugin' : 'gateway'}/SKILL.md`,
            available: !(mode === 'gateway-skill-unavailable' && first && plugin),
            error_code: mode === 'gateway-skill-unavailable' && first && plugin ? 'source-unavailable' : null,
            priority: plugin ? 3 : 0, layer: 'gateway',
            plugin: plugin ? { package_id: 'proof.skill-package', plugin_id: 'instructions', package_version: `${currentRound}.0.0` } : undefined,
          })) : [] });
      }
      if (local) return ok({ workspace_id: first && mode === 'local-mismatch' ? '019a0000-0000-7000-8000-000000000099' : owners[0].workspaceId,
        entries: mode.startsWith('local-') ? [{ name: `同名 Skill ${currentRound}`, node: { uri: skillUri('unused').replace('http_unused', 'local-package'),
          resource_shape: mode === 'local-unsupported' ? 'directory' : 'file', operations: mode === 'local-unreadable' ? ['describe'] : ['describe', 'read'] } }] : [] });
      if (!skill) return ok({ node: { uri: agentsUri, resource_shape: 'file', operations: ['describe', 'read'] } });
      return ok({ workspace_id: first && mode === 'mismatch' ? '019a0000-0000-7000-8000-000000000099' : owners[0].workspaceId,
        entries: ['first', 'higher'].map((id, i) => ({ name: `同名 Skill ${currentRound}`, display_uri: skillUri(id),
          priority: i * 7, available: !(mode === 'unavailable' && first && i === 1), error_code: mode === 'unavailable' && first && i === 1 ? 'source-unavailable' : null })) });
    };
    active++; maximum = Math.max(maximum, active);
    init.signal.addEventListener('abort', () => { aborted++; });
    let response;
    if (mode === 'parallel' && !parallelReleased) response = new Promise(resolve => { parallel.push(() => resolve(result())); });
    else if ((skill && mode !== 'local-close' || local && mode === 'local-close') && first && !releaseRead && ['close', 'identity', 'gateway', 'filter-pending', 'scope-pending', 'local-close'].includes(mode)) {
      response = new Promise(resolve => { releaseRead = () => resolve(result()); });
    } else response = Promise.resolve(result());
    return response.finally(() => { active--; });
  });
  const quick = { quickAccess: { show(value, options) {
    shows.push({ value, options }); picker.value = value; lifetime = provider.provide(picker, cancellation.token, options.providerOptions);
  } }, async pick(items) { return mode === 'scope-pending' || mode === 'scope-change' ? [items.find(item => item.owner?.gatewayId === owners[1].gatewayId)] : undefined; } };
  const groups = { activeGroup: { id: 7 }, getGroup: id => ({ id }), findGroup: () => undefined, addGroup: () => ({ id: 8 }) };
  const configuration = { getValue: key => key === 'workbench.editor.openSideBySideDirection' ? 'right' : true };
  const opener = new ResourceOpener({ registerOpener: () => ({ dispose() {} }) },
    { openEditor: async (input, group) => opened.push({ source: sourceDocumentSource(input.resource), options: input.options, group }) },
    groups, configuration, { async createModelReference(resource) {
      references.push(sourceDocumentSource(resource));
      if (mode === 'gateway-skill-relay-open') {
        let provider;
        const source = new SourceDocuments({ registerTextModelContentProvider(_scheme, value) { provider = value; return { dispose() {} }; } },
          { getModel() { return null; }, createModel(content, _language, uri) { documents.push({ content, source: sourceDocumentSource(uri) }); return { uri }; } },
          { createByFilepathOrFirstLine() { return {}; } });
        try { await provider.provideTextContent(resource); } finally { source.dispose(); }
      }
      return { object: {}, dispose() { released++; } };
    } });
  picker = { value: 'resource ', items: [], activeItems: [], selectedItems: [], buttons: [], keyMods: { alt: mode === 'side', ctrlCmd: mode === 'pinned' },
    onDidChangeValue: changes.event, onDidAccept: accepts.event, onDidTriggerButton: buttons.event,
    onDidTriggerItemButton: Event.None, onDidTriggerSeparatorButton: Event.None, hide() { lifetime.dispose(); } };
  provider = new ResourceQuickAccess(quick, opener, groups, configuration, { error: cause => errors.push(String(cause)) }, {}, {});
  lifetime = provider.provide(picker, cancellation.token, { searchOwners: selected });
  const rows = () => picker.items.map(item => ({ id: item.id, label: item.label, description: item.description, detail: item.detail, highlights: item.highlights, selectable: !!item.accept }));
  const until = async predicate => {
    for (let i = 0; i < 300; i++) { if (predicate()) return; await new Promise(resolve => setTimeout(resolve, 5)); }
    throw new Error('Skill 来源合同未收敛');
  };
  const filter = async value => { picker.value = `resource ${value}`; changes.fire(picker.value); await until(() => !picker.busy); snapshots.push(rows()); };
  let barrierRequests;
  try {
    if (mode === 'parallel') {
      await until(() => parallel.length === 3); barrierRequests = requests.filter(item => item.path !== '/api/gateway/workspaces').length;
      parallelReleased = true; parallel.forEach(release => release());
    }
    if (['close', 'identity', 'gateway', 'filter-pending', 'scope-pending', 'local-close'].includes(mode)) {
      await until(() => releaseRead);
      if (mode === 'close' || mode === 'local-close') lifetime.dispose();
      else if (mode === 'identity') invalidateGatewayUserSession(0);
      else if (mode === 'gateway') workspaceContext.publish({ ...workspaceContext.getSnapshot(), gatewayId: owners[1].gatewayId });
      else if (mode === 'scope-pending') { buttons.fire(picker.buttons[0]); await until(() => shows.length); }
      else { picker.value = 'resource 同名'; changes.fire(picker.value); }
      releaseRead();
      if (['close', 'identity', 'gateway', 'local-close'].includes(mode)) await new Promise(resolve => setTimeout(resolve, 20));
      else await until(() => !picker.busy && picker.items.length > 0);
    } else await until(() => !picker.busy && picker.items.length > 0);
    if (mode === 'reuse' || mode === 'local-reuse' || mode === 'gateway-skill-reuse') for (const value of ['同', '同名', 'NO_MATCH', 'ag', '']) await filter(value);
    if (mode === 'reopen' || mode === 'gateway-skill-reopen') {
      snapshots.push(rows()); lifetime.dispose(); lifetime = provider.provide(picker, cancellation.token, { searchOwners: selected });
      await until(() => !picker.busy && round === 2); snapshots.push(rows());
    }
    if (['scope-change', 'scope-cancel'].includes(mode)) {
      buttons.fire(picker.buttons[0]); await until(() => shows.length && !picker.busy); snapshots.push(rows());
    }
    if (mode === 'gateway-skill-no-owner' || mode === 'gateway-skill-owner-mismatch') {
      const uri = `boxteam://gateway/${owners[1].gatewayId}/resources/skills/plugin/SKILL.md`;
      try { await opener.open(uri, mode.endsWith('no-owner') ? {} : { boxteamResourceOwner: owners[1] }); }
    if (mode.startsWith('gateway-skill-copy')) {
      const uri = `boxteam://gateway/${owners[1].gatewayId}/resources/skills/plugin/SKILL.md`;
      try { documents.push({ reference: await resourceReference(sourceDocumentUri(owners[1], uri)) }); }
      catch (cause) { errors.push(String(cause)); }
    }
      catch (cause) { errors.push(String(cause)); }
    }
    if (['open', 'side', 'pinned', 'background', 'freeze', 'local-open', 'gateway-skill-open', 'gateway-skill-relay-open'].includes(mode)) {
      picker.activeItems = picker.selectedItems = [picker.items.find(item => item.id.startsWith(gatewayWorkspaceKey(owners[1]))
        && (mode === 'local-open' ? item.description.startsWith('本地 Skill')
          : mode.startsWith('gateway-skill-') ? item.description.startsWith('插件 Skill') : item.id.endsWith(skillUri('first'))))];
      accepts.fire({ inBackground: mode === 'background' });
      if (mode === 'freeze') {
        await until(() => releaseParse); groups.activeGroup = { id: 9 }; lifetime.dispose(); releaseParse();
      }
      await until(() => opened.length || errors.length);
    }
    return { rows: rows(), requests, maximum, barrierRequests, aborted, opened, references, errors, shows, snapshots, released, documents };
  } finally {
    lifetime.dispose(); cancellation.dispose(true); provider.dispose(); opener.dispose(); changes.dispose(); accepts.dispose(); buttons.dispose();
    unwrapSessionApiFetch(); workspaceContext.publish(previous);
  }
}
