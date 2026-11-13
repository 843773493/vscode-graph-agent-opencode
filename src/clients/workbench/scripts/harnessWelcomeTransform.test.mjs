import { expect, test } from 'bun:test';
import { readFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { pinnedUpstreamCommit, sourceTransform } from './sourceTransform.mjs';
import { harnessWelcomeSourcePaths } from './harnessWelcomeTransform.mjs';

const root = resolve('src/clients/workbench');
const upstream = join(root, 'upstream');
const transform = sourceTransform(root, { commit: pinnedUpstreamCommit }, []);

test('Getting Started直接提供Provider、Agent和Gateway管理入口，不触碰固定上游', async () => {
  const path = join(upstream, harnessWelcomeSourcePaths[0]);
  const original = await readFile(path, 'utf8');
  const transformed = await transform(path, original);

  for (const command of ['boxteam.settings.modelsProviders', 'boxteam.settings.agents', 'boxteam.gateway.connections']) {
    expect(transformed).toContain(`command:${command}`);
  }
  expect(transformed).toContain("id: 'boxteam.manageProviders'");
  expect(transformed).toContain("id: 'boxteam.manageAgents'");
  expect(transformed).toContain("id: 'boxteam.manageGateway'");
  expect(await readFile(path, 'utf8')).toBe(original);
});
