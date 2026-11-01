import { resolve } from 'node:path';
import { parseArgs } from 'node:util';
import { createWorkbenchServer } from './server.mjs';

const { values } = parseArgs({ options: { port: { type: 'string', default: '8411' }, host: { type: 'string', default: '127.0.0.1' }, gateway: { type: 'string' }, root: { type: 'string' } } });
const server = createWorkbenchServer({ root: values.root ?? resolve(import.meta.dirname, '../dist'), gateway: values.gateway });
server.on('error', cause => { console.error('Workbench 服务启动失败', cause); process.exitCode = 1; });
server.listen(Number(values.port), values.host, () => console.log(`Code OSS Workbench: http://${values.host}:${server.address().port}`));
