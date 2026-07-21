import { describe, expect, test } from "bun:test";

import { issueFederationToken } from "./federation-token.mjs";

describe("federation token", () => {
  test("使用内置 Python 签发令牌", () => {
    const calls = [];
    const output = issueFederationToken({
      runtime: {
        pythonExecutable: "/runtime/python/bin/python3.12",
        applicationRoot: "/runtime/application",
      },
      environment: {
        BOXTEAM_HOME: "/tmp/boxteams",
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

    expect(calls[0].command).toBe("/runtime/python/bin/python3.12");
    expect(calls[0].args[0]).toBe("-m");
    expect(calls[0].args[1]).toBe("app.gateway.federation_pairing");
    expect(calls[0].options.env.BOXTEAM_HOME).toBe("/tmp/boxteams");
    expect(output).toBe('{"token":"secret"}');
  });

  test("签发失败不会静默继续", () => {
    expect(() =>
      issueFederationToken({
        runtime: {
          pythonExecutable: "/runtime/python",
          applicationRoot: "/runtime/application",
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
