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
    })).toThrow("SSH 端口必须是 1-65535 的整数");
  });
});
