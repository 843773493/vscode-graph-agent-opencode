import { describe, expect, test } from "bun:test";
import { tmpdir } from "node:os";
import path from "node:path";

import { issueFederationToken } from "./federation-token.mjs";

const runtimeRoot = path.join(tmpdir(), "boxteam-federation-token-test");
const pythonExecutable = path.join(runtimeRoot, "python");
const applicationRoot = path.join(runtimeRoot, "application");
const boxteamHome = path.join(tmpdir(), "boxteams");

describe("federation token", () => {
  test("使用内置 Python 签发令牌", () => {
    const calls = [];
    const output = issueFederationToken({
      runtime: {
        pythonExecutable,
        applicationRoot,
      },
      environment: {
        BOXTEAM_HOME: boxteamHome,
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

    expect(calls[0].command).toBe(pythonExecutable);
    expect(calls[0].args[0]).toBe("-m");
    expect(calls[0].args[1]).toBe("app.gateway.federation_pairing");
    expect(calls[0].options.env.BOXTEAM_HOME).toBe(boxteamHome);
    expect(output).toBe('{"token":"secret"}');
  });

  test("签发失败不会静默继续", () => {
    expect(() =>
      issueFederationToken({
        runtime: {
          pythonExecutable,
          applicationRoot,
        },
        environment: {},
        args: [],
        spawnSyncImpl() {
          return { status: 2, stdout: "", stderr: "missing arguments" };
        },
      }),
    ).toThrow("missing arguments");
  });
});
