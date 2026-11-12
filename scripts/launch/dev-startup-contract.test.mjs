import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import path from "node:path";
import vm from "node:vm";
import ts from "typescript";

const source = readFileSync(path.resolve("scripts/launch/dev.mjs"), "utf8");
const parsed = ts.createSourceFile("dev.mjs", source, ts.ScriptTarget.Latest, true, ts.ScriptKind.JS);
const entry = parsed.statements.find((node) => ts.isIfStatement(node) && node.expression.getText(parsed) === "onlyLaunch");
if (!entry) throw new Error("dev.mjs 缺少 onlyLaunch 入口");
const main = parsed.statements.find((node) => ts.isFunctionDeclaration(node) && node.name?.text === "main");
const pending = main?.body.statements.flatMap((node) => ts.isVariableStatement(node) ? [...node.declarationList.declarations] : [])
  .find((node) => node.name.getText(parsed) === "pendingGatewayRestart");
if (!pending?.initializer) throw new Error("dev.mjs 缺少 main pending 配置判定");
const mainEnvironment = main?.body.statements.flatMap((node) => ts.isVariableStatement(node) ? [...node.declarationList.declarations] : [])
  .find((node) => node.name.getText(parsed) === "environment");
if (!mainEnvironment?.initializer) throw new Error("dev.mjs 缺少 main 服务环境");

describe("开发服务日志捕获的归属", () => {
  for (const readyFile of [null, "development-ready.json"]) {
    for (const captured of [false, true]) {
      test(`ready=${String(readyFile)} captured=${String(captured)} 时仅继承真实外层捕获`, () => {
        const sink = JSON.stringify({ role: "launcher", owner_id: "fixture-owner" });
        const environment = captured
          ? { BOXTEAM_SERVICE_LOG_CAPTURED: "1", BOXTEAM_RUNTIME_LOG_SINK: sink }
          : {};
        // 执行真实环境组装表达式；ready文件只表示生命周期就绪，不证明stdio已被捕获。
        const forwarded = vm.runInNewContext(`(${mainEnvironment.initializer.getText(parsed)})`, {
          process: { env: environment, execPath: "fixture-bun" }, path,
          runtimeManifest: "fixture-manifest", boxteamHome: "fixture-home", projectRoot: process.cwd(), frontendRoot: process.cwd(),
          pythonBin: "fixture-python", nodeBin: "fixture-node", defaultWorkspaceRoot: "fixture-workspace",
          portOffset: 0, ports: {}, listenHost: "127.0.0.1", host: "127.0.0.1",
          detachedReadyFile: readyFile, SERVICE_LOG_CAPTURED_ENV: "BOXTEAM_SERVICE_LOG_CAPTURED",
        });
        expect(forwarded.BOXTEAM_SERVICE_LOG_CAPTURED).toBe(environment.BOXTEAM_SERVICE_LOG_CAPTURED);
        expect(forwarded.BOXTEAM_RUNTIME_LOG_SINK).toBe(environment.BOXTEAM_RUNTIME_LOG_SINK);
      });
    }
  }
});

describe("开发 launcher 的 pending 配置准入", () => {
  for (const candidate of [undefined, "", "  ", "candidate-v1"]) {
    test(`候选为 ${String(candidate)} 时只按显式 pending 发起 handoff`, async () => {
      const calls = [];
      const environment = candidate === undefined ? {} : { BOXTEAM_CONFIG_CANDIDATE_REF: candidate };
      // 执行真实入口，只替换外部进程端口；不启动开发服务或写用户状态。
      const execute = vm.runInNewContext(`(async () => ${entry.thenStatement.getText(parsed)})`, {
        restartDelayMs: 0,
        boxteamHome: "fixture-home",
        process: { env: environment },
        requestGatewayHandoff: async (request) => {
          calls.push({ kind: "handoff", request });
          return { handled: false };
        },
        launchDetachedManager: async () => { calls.push({ kind: "launch" }); },
      });
      await execute();
      expect(calls.map((call) => call.kind)).toEqual(candidate?.trim() ? ["handoff", "launch"] : ["launch"]);
      if (candidate?.trim()) expect(calls[0].request.environment).toBe(environment);
    });

    test(`manager 候选为 ${String(candidate)} 时不误入 pending 重启分支`, () => {
      const environment = candidate === undefined ? {} : { BOXTEAM_CONFIG_CANDIDATE_REF: candidate };
      expect(vm.runInNewContext(pending.initializer.getText(parsed), { environment })).toBe(Boolean(candidate?.trim()));
    });
  }
});
