import { describe, expect, test } from "bun:test";
import path from "node:path";

import { issueFederationToken } from "./federation-token.mjs";

const fixtureRoot = path.resolve(
  "out/tests/packages/launcher/src/federation-token/workspace/配对 命令",
);
const fixturePythonExecutable = path.join(fixtureRoot, "runtime", "python");
const fixtureApplicationRoot = path.join(fixtureRoot, "application");
const fixtureBoxteamHome = path.join(fixtureRoot, "boxteams");

describe("federation token", () => {
  test("使用内置 Python 签发令牌", () => {
    const calls = [];
    const output = issueFederationToken({
      runtime: {
        pythonExecutable: fixturePythonExecutable,
        applicationRoot: fixtureApplicationRoot,
      },
      environment: {
        BOXTEAM_HOME: fixtureBoxteamHome,
      },
      args: [
        "--connection-id",
        "rgw_local",
        "--peer-gateway-id",
        "gateway_remote",
        "--json",
      ],
      spawnSyncImpl(command, args, options) {
        calls.push({ command, args, options });
        return { status: 0, stdout: '{"token":"secret"}\n', stderr: "" };
      },
    });

    expect(calls[0].command).toBe(fixturePythonExecutable);
    expect(calls[0].args[0]).toBe("-m");
    expect(calls[0].args[1]).toBe("app.gateway.federation_pairing");
    expect(calls[0].options.env.BOXTEAM_HOME).toBe(fixtureBoxteamHome);
    expect(output).toBe('{"token":"secret"}');
  });

  test("签发失败不会静默继续", () => {
    expect(() =>
      issueFederationToken({
        runtime: {
          pythonExecutable: fixturePythonExecutable,
          applicationRoot: fixtureApplicationRoot,
        },
        environment: {},
        args: [],
        spawnSyncImpl() {
          return { status: 2, stdout: "", stderr: "missing arguments" };
        },
      }),
    ).toThrow("missing arguments");
  });

  for (const code of ["ENOENT", "EACCES", "ETIMEDOUT"]) {
    test(`内置 Python 启动失败保留${code}原始原因`, () => {
      const failure = Object.assign(new Error(`spawn ${code}`), {
        code, path: fixturePythonExecutable, syscall: "spawnSync",
      });
      let observed;
      try {
        issueFederationToken({
          runtime: {
            pythonExecutable: fixturePythonExecutable,
            applicationRoot: fixtureApplicationRoot,
          },
          environment: { BOXTEAM_HOME: fixtureBoxteamHome },
          args: [],
          spawnSyncImpl() {
            return { error: failure, status: null, stdout: "", stderr: "" };
          },
        });
      } catch (error) {
        observed = error;
      }
      expect(observed).toBeInstanceOf(Error);
      expect(observed.message).toBe(
        `签发 Gateway 配对令牌失败: ${fixturePythonExecutable}: ${failure.message}`,
      );
      expect(observed.cause).toBe(failure);
    });
  }
});
