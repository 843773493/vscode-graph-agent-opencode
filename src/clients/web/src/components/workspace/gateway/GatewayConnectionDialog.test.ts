import { describe, expect, test } from "bun:test";

import {
  buildManualSshConnectionRequest,
  buildSelectedSshConnectionRequest,
} from "./GatewayConnectionDialog";

describe("添加 SSH 连接请求", () => {
  test("从 ~/.ssh/config 选项构建请求", () => {
    expect(buildSelectedSshConnectionRequest({
      connection_id: "ssh-config:gpu",
      source: "ssh_config",
      label: "gpu",
      host: "100.64.0.60",
      port: 22,
      username: "developer",
      ssh_config_host: "gpu",
    })).toEqual({
      ssh_config_host: "gpu",
      remote_gateway_port: 8014,
    });
  });

  test("手动表单保留显式连接参数并校验端口", () => {
    expect(buildManualSshConnectionRequest({
      name: "GPU Gateway",
      host: " 100.64.0.60 ",
      port: "2222",
      username: " developer ",
      privateKeyPath: " ~/.ssh/gpu_ed25519 ",
      remoteGatewayPort: "9014",
    })).toEqual({
      name: "GPU Gateway",
      host: "100.64.0.60",
      port: 2222,
      username: "developer",
      private_key_path: "~/.ssh/gpu_ed25519",
      remote_gateway_port: 9014,
    });

    expect(() => buildManualSshConnectionRequest({
      name: "",
      host: "100.64.0.60",
      port: "70000",
      username: "developer",
      privateKeyPath: "~/.ssh/id_ed25519",
      remoteGatewayPort: "8014",
    })).toThrow("SSH 端口必须是 1–65535 之间的十进制整数");
  });

  test("十六进制与科学计数法不被静默收敛成别的端口", () => {
    // 旧实现用 Number() 解析，"0x50" 会被当成 80、"1e3" 当成 1000，SSH 会连到
    // 用户没填的端口上。端口字面量必须与端口转发面板共用同一份严格口径。
    for (const port of ["0x50", "1e3", "0b101", " 2222 ", "12.0", "65536", "0"]) {
      expect(() => buildManualSshConnectionRequest({
        name: "",
        host: "100.64.0.60",
        port,
        username: "developer",
        privateKeyPath: "~/.ssh/id_ed25519",
        remoteGatewayPort: "8014",
      })).toThrow("SSH 端口必须是 1–65535 之间的十进制整数");
    }
  });
});
