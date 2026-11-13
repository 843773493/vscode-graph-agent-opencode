import { afterEach, expect, test } from 'bun:test';
import { listAgents, setWorkspaceDefaultAgent, setWorkspaceDefaultProvider } from '../workspace';
import { gatewayWorkspaceKey } from '../workspaceIdentity';
import { installSessionApiFetchMock, unwrapSessionApiFetch } from '../session/sessionApiFetchMock';

afterEach(unwrapSessionApiFetch);

test.each([true, false])('默认值操作保留原目标及完整 Agent 回执（明确连接=%s）', async explicitRoute => {
  const target = { gateway_id: 'gateway-defaults', workspace_id: 'workspace-defaults', gateway_connection_id: 'original-route' };
  const selection = explicitRoute ? target : gatewayWorkspaceKey(target);
  const calls: { path: string; method: string; body: unknown }[] = [];
  const agents = [{ agent_id: 'coder', name: 'Coder', model: 'primary', tools: [], capabilities: [], providers: [], workspace_default: true },
    { agent_id: 'reviewer', name: 'Reviewer', model: 'backup', tools: [], capabilities: [], providers: [], workspace_default: false }];
  installSessionApiFetchMock(({ path, method, init }) => {
    const headers = new Headers(init?.headers);
    expect(headers.get('X-BoxTeam-Target-Gateway-Id')).toBe(target.gateway_id);
    expect(headers.get('X-BoxTeam-Workspace-Id')).toBe(target.workspace_id);
    expect(headers.get('X-BoxTeam-Gateway-Connection-Id')).toBe(explicitRoute ? target.gateway_connection_id : null);
    calls.push({ path, method, body: init?.body ? JSON.parse(String(init.body)) : undefined });
    return Response.json({ code: 0, message: 'ok', request_id: 'defaults-request', data: agents });
  });
  expect(await listAgents(0, selection)).toEqual(agents);
  expect(await setWorkspaceDefaultAgent(0, 'coder', selection)).toEqual(agents);
  expect(await setWorkspaceDefaultProvider(0, 'coder', 'primary', selection)).toEqual(agents);
  expect(calls).toEqual([
    { path: '/api/v1/agents', method: 'GET', body: undefined },
    { path: '/api/v1/agents/workspace-default', method: 'PUT', body: { agent_id: 'coder' } },
    { path: '/api/v1/agents/coder/workspace-default-provider', method: 'PUT', body: { provider_id: 'primary' } },
  ]);
});
