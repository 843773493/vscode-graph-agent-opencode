import { expect, test } from "bun:test";
import { mkdirSync, readFileSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { TestRunContext } from "../../../harness/js/run-context.mjs";

const context = TestRunContext.fromTestFile(fileURLToPath(import.meta.url));

test("日志状态核实际描述符和路径替换，同时拒绝篡改设备及文件索引", () => {
  const fixture = path.join(context.workspaceRoot, randomUUID());
  mkdirSync(fixture, { recursive: true });
  const logPath = path.join(fixture, "actual.log");
  const result = spawnSync(process.env.BOXTEAM_NODE_BIN ?? "node", ["--input-type=module", "--eval", `
    import {closeSync,openSync,renameSync,writeFileSync} from 'node:fs';
    import {registerRuntimeLogSink,runtimeLoggingState,loadRuntimeLogContract} from '@boxteam/plugin-sdk/logging';
    const contract=loadRuntimeLogContract(), logPath=${JSON.stringify(logPath)};
    const descriptor=openSync(logPath,'a+'), other=openSync(logPath+'.other','a+');
    const sink={path:logPath,owner_id:'actual-owner',role:'terminal',source:'test',...contract.fileIdentity(descriptor)};
    const refusal=operation=>{try{operation();return null;}catch(error){return error.message;}};
    try {
      const device=refusal(()=>registerRuntimeLogSink({sink:{...sink,device:sink.device+'1'},descriptors:[descriptor]}));
      const inode=refusal(()=>registerRuntimeLogSink({sink:{...sink,inode:sink.inode+'1'},descriptors:[descriptor]}));
      const redirected=refusal(()=>registerRuntimeLogSink({sink,descriptors:[other]}));
      registerRuntimeLogSink({sink,descriptors:[descriptor]});
      const before=runtimeLoggingState('terminal');
      renameSync(logPath,logPath+'.moved');writeFileSync(logPath,'replacement');
      const replaced=refusal(()=>runtimeLoggingState('terminal'));
      process.stdout.write(JSON.stringify({before,sink,device,inode,redirected,replaced}));
    } finally {closeSync(other);closeSync(descriptor);}
  `], { encoding: "utf8", timeout: 10000 });
  expect(result.status, result.stderr).toBe(0);
  const observed = JSON.parse(result.stdout);
  expect(observed.before.log_sink).toEqual(observed.sink);
  for (const rejection of [observed.device, observed.inode, observed.redirected, observed.replaced]) {
    expect(rejection).toBe("受管进程实际日志描述符与冻结去向不一致");
  }
  expect(readFileSync(logPath, "utf8")).toBe("replacement");
});

test("受管 Node 在首条日志前读取启动 revision，后续读取不重放启动设置", () => {
  const result = spawnSync(process.env.BOXTEAM_NODE_BIN ?? "node", ["--input-type=module", "--eval", `
    import { createRuntimeLogger, runtimeLoggingState, applyRuntimeLogging } from '@boxteam/plugin-sdk/logging';
    const lines = [];
    const logger = createRuntimeLogger("terminal", "startup", {write(value) { lines.push(value); }});
    logger.info("应过滤");
    const before = runtimeLoggingState("terminal");
    applyRuntimeLogging({runtime_instance_id: before.runtime_instance_id, expected_revision: before.applied_revision,
      revision: "hot-r2", level: "error", pretty: true}, "terminal");
    process.stdout.write(JSON.stringify({before, after: runtimeLoggingState("terminal"), lines}));
  `], {
    env: { ...process.env, BOXTEAM_RUNTIME_LOGGING: JSON.stringify({revision: "startup-r1", level: "warning", pretty: false}) },
    encoding: "utf8", timeout: 5000,
  });
  expect(result.status).toBe(0);
  const { before, after, lines } = JSON.parse(result.stdout);
  expect(before.applied_revision).toBe("startup-r1");
  expect(before.level).toBe("warning");
  expect(before.pretty).toBe(false);
  expect(after).toEqual({...before, applied_revision: "hot-r2", level: "error", pretty: true});
  expect(lines).toEqual([]);
});
