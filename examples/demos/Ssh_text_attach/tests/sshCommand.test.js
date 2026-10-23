import { describe, expect, test } from "bun:test";
import { buildSshArguments } from "../src/server/sshCommand.js";

const sshConfig = {
  host: "gateway.example.test",
  port: 2222,
  user: "demo",
  privateKeyPath: "C:\\demo\\ssh\\id_ed25519",
  remoteBackendHost: "127.0.0.1",
  remoteBackendPort: 7912,
};

describe("SSH 参数", () => {
  test("Windows 使用 NUL 作为 known-hosts 空设备", () => {
    const args = buildSshArguments(sshConfig, 43123, "win32");

    expect(args).toContain("UserKnownHostsFile=NUL");
    expect(args).not.toContain("UserKnownHostsFile=/dev/null");
    expect(args).toEqual([
      "-N",
      "-L",
      "127.0.0.1:43123:127.0.0.1:7912",
      "-i",
      "C:\\demo\\ssh\\id_ed25519",
      "-p",
      "2222",
      "-o",
      "ExitOnForwardFailure=yes",
      "-o",
      "IdentitiesOnly=yes",
      "-o",
      "StrictHostKeyChecking=no",
      "-o",
      "UserKnownHostsFile=NUL",
      "-o",
      "ServerAliveInterval=15",
      "-o",
      "ServerAliveCountMax=2",
      "demo@gateway.example.test",
    ]);
  });

  test.each(["linux", "darwin"])("%s 使用 POSIX known-hosts 空设备", (platform) => {
    expect(buildSshArguments(sshConfig, 43123, platform)).toContain(
      "UserKnownHostsFile=/dev/null",
    );
  });
});
