import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { resolve,join } from 'node:path';
import { writeFile } from 'node:fs/promises';
import { chromium } from 'playwright';
import { build } from '../../../../../src/clients/workbench/node_modules/esbuild/lib/main.js';
import { buildSourcePlugin,pinnedUpstreamCommit,sourceTransform } from '../../../../../src/clients/workbench/scripts/sourceTransform.mjs';
import { test } from 'bun:test';
import { TestRunContext } from '../../../../harness/js/run-context.mjs';

test('原生文件流提前交付、仅 EOF 发布 CAS，取消及 Range 不污染基线', async () => {
  const run=await TestRunContext.fromTestFile('tests/integration/clients/web/workspace_files/workspace_file_stream.test.mjs').prepare();
  const artifacts=run.artifactsDir;
  const root=resolve('src/clients/workbench');
  const fixture=`
export { FileService } from './src/clients/workbench/upstream/src/vs/platform/files/common/fileService.js';
export { NullLogService } from './src/clients/workbench/upstream/src/vs/platform/log/common/log.js';
export { UriIdentityService } from './src/clients/workbench/upstream/src/vs/platform/uriIdentity/common/uriIdentityService.js';
export { CancellationTokenSource } from './src/clients/workbench/upstream/src/vs/base/common/cancellation.js';
export { installBackendFiles,workspaceFileUri } from './src/clients/workbench/src/workspaceFiles.js';
export { workspaceContext } from './src/clients/workbench/src/workspaceContext.js';
export { invalidateGatewayToken } from './src/clients/web/src/api/http.js';`;
  await build({stdin:{contents:fixture,loader:'ts',resolveDir:process.cwd()},outfile:join(artifacts,'provider.fixture.mjs'),bundle:true,format:'esm',platform:'browser',target:'es2024',
   plugins:[buildSourcePlugin(root,sourceTransform(root,{commit:pinnedUpstreamCommit},[]))],tsconfig:join(root,'tsconfig.jsonc'),logLevel:'silent'});
  const first=new TextEncoder().encode('中文🙂\r\n'),last=new TextEncoder().encode('最后一块');
  const controls=new Map(),requests=[];
  const server=Bun.serve({hostname:'127.0.0.1',port:0,fetch(req){
   const url=new URL(req.url);
   if(url.pathname==='/fixture.mjs')return new Response(Bun.file(join(artifacts,'provider.fixture.mjs')),{headers:{'Content-Type':'text/javascript'}});
   if(url.pathname==='/api/gateway/auth/local-credential')return Response.json({data:{token:'provider-test'},request_id:'provider-credential'});
   if(url.pathname==='/api/v1/workspace/files/raw'){
    const path=url.searchParams.get('path');requests.push({path,range:req.headers.get('Range'),gateway:req.headers.get('X-BoxTeam-Target-Gateway-Id'),workspace:req.headers.get('X-BoxTeam-Workspace-Id')});
    if(req.headers.get('Range'))return new Response(new Uint8Array([111]),{status:206});
    return new Response(new ReadableStream({start(c){controls.set(path,c);c.enqueue(first);}}),{headers:{'Content-Type':'application/octet-stream'}});
   }
   if(url.pathname==='/release') {const c=controls.get(url.searchParams.get('path'));c.enqueue(last);c.close();return new Response('released');}
   return new Response('<!doctype html><title>原生文件 Provider 流式验证</title>');
  }});
  run.addCleanup('受控文件 HTTP 服务', () => server.stop(true));
  try{
   const browser=await chromium.launch({executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH ?? chromium.executablePath(),headless:true,args:['--no-sandbox']});
   run.addCleanup('文件流 Chromium', () => browser.close());
   const page=await browser.newPage();await page.goto(`http://127.0.0.1:${server.port}/`);
   const observations=await page.evaluate(async()=>{
    globalThis._VSCODE_FILE_ROOT=location.origin+'/';const m=await import('/fixture.mjs');
    const files=new m.FileService(new m.NullLogService()),identity=new m.UriIdentityService(files);m.installBackendFiles(files,identity);
    const provider=files.getProvider('boxteam-file'),owner={gatewayId:'gateway-stream',workspaceId:'00000000-0000-4000-8000-000000000099'};
    const root=`boxteam://workspace/${owner.workspaceId}/resources/files/root`;
    m.workspaceContext.publish({gatewayId:owner.gatewayId,workspaces:[],activeId:null,settings:null,loading:false,error:null});
    const resource=name=>m.workspaceFileUri(owner,root+'/'+name,root);
    function read(name,options={}){
     const token=new m.CancellationTokenSource(),uri=resource(name),stream=provider.readFileStream(uri,options,token.token),chunks=[];
     let first;const seen=new Promise(r=>{first=r});
     const done=new Promise(resolve=>{let error;stream.on('error',e=>{error=e.message});stream.on('end',()=>resolve({error,chunks}));stream.on('data',chunk=>{chunks.push([...chunk]);first()});});
     return {token,uri,seen,done};
    }
    const full=read('full.txt');await full.seen;
    const before=provider.revisionForBackup(full.uri);if(before!==undefined)throw new Error('EOF 前不得发布 CAS');
    await fetch('/release?path='+encodeURIComponent(root+'/full.txt'));const complete=await full.done;
    const revision=provider.revisionForBackup(full.uri);
    const range=read('full.txt',{position:2,length:1});const partial=await range.done;
    if(provider.revisionForBackup(full.uri)!==revision)throw new Error('Range 改写了完整文件保存基线');
    const cancelled=read('cancel.txt');await cancelled.seen;cancelled.token.cancel();const cancel=await cancelled.done;
    if(provider.revisionForBackup(cancelled.uri)!==undefined)throw new Error('取消读取发布了不完整 CAS');
    const revoked=read('revoke.txt');await revoked.seen;m.invalidateGatewayToken(0);const revoke=await revoked.done;
    const switched=read('switch.txt');await switched.seen;m.workspaceContext.publish({...m.workspaceContext.getSnapshot(),gatewayId:'gateway-other'});const change=await switched.done;
    const result={before:before??null,complete,revision,partial,cancel,revoke,change};
    identity.dispose();files.dispose();return result;
   });
   const expected=createHash('sha256').update(first).update(last).digest('hex');
   assert.equal(observations.revision,expected);assert.equal(observations.complete.error,undefined);assert.equal(observations.complete.chunks.length,2);
   assert.deepEqual(observations.partial.chunks,[[111]]);assert.ok(observations.cancel.error);assert.ok(observations.revoke.error);assert.ok(observations.change.error);
   assert.ok(requests.every(r=>r.gateway==='gateway-stream'&&r.workspace==='00000000-0000-4000-8000-000000000099'));
   const result={boundary:'真实 Chromium、原生 Provider 与 FileService；HTTP 文件正文为受控分块边界，不代表真实 Gateway/磁盘 E2E。',observations,requests,expected};
   await writeFile(join(artifacts,'provider-receipt.json'),JSON.stringify(result,null,2));console.log('已验证首块、完整 CAS、Range、取消、凭据撤销和目标切换');
  }finally{await run.close();}
}, 30_000);
