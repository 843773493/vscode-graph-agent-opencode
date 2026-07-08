import { describe, expect, test } from "bun:test";
import { parsePort, parseTargetsConfigText } from "../src/server/config.js";

describe("config", () => {
  const localTargetId = "7c9e2a4f1b6d";
  const remoteTargetId = "3f8b1d6a9c02";

  test("解析端口", () => {
    expect(parsePort("7910", "测试端口")).toBe(7910);
    expect(() => parsePort("0", "测试端口")).toThrow();
    expect(() => parsePort("abc", "测试端口")).toThrow();
  });

  test("解析 SSH attach 目标", () => {
    const config = parseTargetsConfigText(
      JSON.stringify({
        defaultTargetId: localTargetId,
        targets: [
          { id: localTargetId, name: "青岚节点", kind: "local", backendOrigin: "http://127.0.0.1:7912" },
          {
            id: remoteTargetId,
            name: "星桥节点",
            kind: "ssh",
            ssh: {
              host: "127.0.0.1",
              port: 2222,
              user: "demo",
              privateKeyPath: "ssh/id_ed25519",
              remoteBackendHost: "127.0.0.1",
              remoteBackendPort: 7912,
            },
          },
        ],
      }),
      "/demo/root",
    );

    expect(config.defaultTargetId).toBe(localTargetId);
    expect(config.targets).toHaveLength(2);
    expect(config.targets[0].backendOrigin).toBe("http://127.0.0.1:7912");
    expect(config.targets[1].ssh.privateKeyPath).toBe("/demo/root/ssh/id_ed25519");
  });

  test("拒绝非 12 位 target id", () => {
    expect(() => parseTargetsConfigText(
      JSON.stringify({
        defaultTargetId: "local",
        targets: [
          { id: "local", name: "本地", kind: "local", backendOrigin: "http://127.0.0.1:7912" },
        ],
      }),
      "/demo/root",
    )).toThrow();
  });
});
