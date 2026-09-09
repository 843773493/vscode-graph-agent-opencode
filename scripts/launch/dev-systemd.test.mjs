import { describe, expect, test } from "bun:test";

import {
  developmentSystemdUnitName,
  resolveDevelopmentLayout,
} from "./dev-environment.mjs";
import { buildTransientDevelopmentUnit } from "./dev-systemd.mjs";

describe("源码开发运行目录", () => {
  test("默认把 BOXTEAM_HOME 放入当前 worktree 的 out", () => {
    const layout = resolveDevelopmentLayout({
      cwd: "/worktrees/feature-a",
      environment: {},
    });

    expect(layout.boxteamHome).toBe(
      "/worktrees/feature-a/out/development-runtime/boxteam-home",
    );
    expect(layout.defaultWorkspaceRoot).toBe(
      "/worktrees/feature-a/out/development-runtime/boxteam-home/boxteam_workspace",
    );
    expect(layout.ports.frontend).toBe(8011);
  });

  test("显式 BOXTEAM_HOME 和端口偏移保持优先", () => {
    const layout = resolveDevelopmentLayout({
      cwd: "/worktrees/feature-a",
      environment: {
        BOXTEAM_HOME: "/tmp/explicit-boxteam-home",
        BOXTEAM_DEV_PORT_OFFSET: "16",
      },
    });

    expect(layout.boxteamHome).toBe("/tmp/explicit-boxteam-home");
    expect(layout.ports.frontend).toBe(8027);
    expect(layout.ports.gateway).toBe(8030);
  });

  test("不同 worktree 使用不同 transient unit", () => {
    const first = resolveDevelopmentLayout({ cwd: "/worktrees/a", environment: {} });
    const second = resolveDevelopmentLayout({ cwd: "/worktrees/b", environment: {} });

    expect(developmentSystemdUnitName(first)).not.toBe(
      developmentSystemdUnitName(second),
    );
  });
});

describe("transient systemd 启动", () => {
  test("使用 collect、Restart=no 和仓库内 BOXTEAM_HOME", () => {
    const unit = buildTransientDevelopmentUnit({
      cwd: "/worktrees/feature-a",
      bunExecutable: "/usr/local/bin/bun",
      environment: {
        HOME: "/home/developer",
        PATH: "/usr/local/bin:/usr/bin",
        SECRET_SHOULD_NOT_LEAK: "secret",
      },
    });

    expect(unit.systemdRunArguments).toContain("--user");
    expect(unit.systemdRunArguments).toContain("--collect");
    expect(unit.systemdRunArguments).toContain("--property=Restart=no");
    expect(unit.systemdRunArguments).toContain(
      "--setenv=BOXTEAM_HOME=/worktrees/feature-a/out/development-runtime/boxteam-home",
    );
    expect(unit.systemdRunArguments).toContain(
      "--setenv=BOXTEAM_DEV_LOCAL_ONLY=1",
    );
    expect(unit.readyFile).toBe(
      "/worktrees/feature-a/out/development-runtime/boxteam-home/state/development-ready.json",
    );
    expect(unit.systemdRunArguments).toContain(
      `--setenv=BOXTEAM_DEV_READY_FILE=${unit.readyFile}`,
    );
    expect(unit.systemdRunArguments.join("\n")).not.toContain(
      "SECRET_SHOULD_NOT_LEAK",
    );
  });
});
