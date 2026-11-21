import { expect, test } from "bun:test";
import { spawnSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";

const projectRoot = process.cwd();
const run = randomUUID();
const base = path.resolve("out/tests/scripts/launch/development/tests/manager");

for (const platform of ["linux", "win32"]) for (const scenario of ["existing", "startup-failure", "cleanup-failure"]) {
  test(`${platform} 启动的实例归属：${scenario}`, () => {
    const root = path.join(base, "workspace", run, platform, scenario);
    const artifacts = path.join(base, "artifacts", run, platform, scenario);
    const home = path.join(root, "home");
    const ready = platform === "linux" ? path.join(home, "state/development-ready.json")
      : path.join(root, "out/development-runtime/detached-ready.json");
    const observation = path.join(artifacts, "observation.json");
    mkdirSync(path.dirname(ready), { recursive: true });
    mkdirSync(artifacts, { recursive: true });
    const record = { pid: 987654321, ready_at: "fixture", project_root: root, boxteam_home: home };
    if (scenario === "existing") writeFileSync(ready, JSON.stringify(record));
    const preload = path.join(artifacts, "preload.mjs");
    // 加载实际 manager 后才模拟平台，避免原生依赖按虚假平台选包；不触及真实实例。
    writeFileSync(preload, `
      import { existsSync, rmSync, writeFileSync } from 'node:fs';
      const { main } = await import(${JSON.stringify(path.join(projectRoot, "scripts/launch/development/manager.mjs"))});
      const platform = ${JSON.stringify(platform)}, scenario = ${JSON.stringify(scenario)};
      const ready = ${JSON.stringify(ready)}, observation = ${JSON.stringify(observation)};
      const record = ${JSON.stringify(record)};
      Object.defineProperty(process, 'platform', { value: platform });
      let launched = false, alive = true;
      const calls = [];
      const result = (stdout = '', exitCode = 0) => ({ stdout: Buffer.from(stdout), stderr: Buffer.from(''), exitCode });
      Bun.spawnSync = command => {
        calls.push(command);
        if (platform === 'linux') {
          if (command[0] === 'systemctl' && command[2] === 'show') {
            const state = scenario === 'existing' ? 'active' : launched ? 'failed' : 'inactive';
            const load = !launched && scenario !== 'existing' ? 'not-found' : 'loaded';
            return result('LoadState=' + load + '\\nActiveState=' + state + '\\nSubState=dead\\nMainPID=' + (scenario === 'existing' ? record.pid : 0) + '\\n', load === 'not-found' ? 1 : 0);
          }
          if (command[0] === 'systemd-run') { launched = true; return result(); }
          if (command[0] === 'systemctl' && command[2] === 'stop') {
            if (scenario === 'cleanup-failure') throw new Error('cleanup-original');
            alive = false; return result();
          }
        } else {
          if (command[1]?.replaceAll('\\\\', '/')?.endsWith('/scripts/launch/development/services.mjs')) {
            launched = true; writeFileSync(ready, JSON.stringify(record)); return result();
          }
          if (command[0] === 'taskkill') {
            if (scenario === 'cleanup-failure') throw new Error('cleanup-original');
            alive = false; return result();
          }
        }
        throw new Error('不得执行真实命令: ' + JSON.stringify(command));
      };
      process.kill = (pid, signal) => {
        if (pid !== record.pid || signal !== 0) throw new Error('不得操作真实 PID');
        if (!alive) throw Object.assign(new Error('missing'), { code: 'ESRCH' });
        return true;
      };
      globalThis.fetch = async () => {
        if (scenario !== 'existing') {
          if (platform !== 'win32' || !launched) throw new Error('不得请求真实服务');
          rmSync(ready);
        }
        return { ok: true, status: 200, body: { async cancel() {} } };
      };
      let failure;
      process.on('exit', () => writeFileSync(observation, JSON.stringify({ calls, alive, launched, ready: existsSync(ready), failure })));
      try { await main(); } catch (error) {
        failure = { message: error.message, errors: error.errors?.map(item => item.message), cause: error.cause?.message };
        throw error;
      }
    `);
    const command = [process.execPath, preload, "start"];
    const result = spawnSync(command[0], command.slice(1), { cwd: projectRoot, encoding: "utf8", timeout: 8000,
      env: { ...process.env, BOXTEAM_PROJECT_ROOT: root, BOXTEAM_FRONTEND_ROOT: projectRoot,
        BOXTEAM_HOME: home, BOXTEAM_DEFAULT_USER_WORKSPACE_ROOT: path.join(home, "boxteam_workspace"), BOXTEAM_DEV_PORT_OFFSET: "0" } });
    writeFileSync(path.join(artifacts, "process.json"), JSON.stringify({ command, ...result }));
    expect(result.error).toBeUndefined();
    expect(result.status, result.stdout + result.stderr).toBe(scenario === "existing" ? 0 : 1);
    const observed = JSON.parse(readFileSync(observation, "utf8"));
    if (scenario === "existing") {
      expect(result.stdout).toContain(platform === "linux" ? "已在运行" : "already running");
      expect(observed.launched).toBe(false);
      expect(observed.alive).toBe(true);
      expect(observed.ready).toBe(true);
      expect(observed.calls.every(command => command[0] === "systemctl" && command[2] === "show")).toBe(true);
    } else {
      expect(observed.launched).toBe(true);
      expect(result.stderr).toContain(platform === "linux" ? "就绪前已退出" : "已退出或更换");
      expect(observed.alive).toBe(scenario === "cleanup-failure");
      if (scenario === "cleanup-failure") {
        expect(result.stderr).toContain("cleanup-original");
        expect(observed.failure.message).toBe("开发实例启动与清理均失败");
        expect(observed.failure.errors).toEqual([observed.failure.cause, "cleanup-original"]);
      }
      const cleanup = observed.calls.at(-1);
      if (platform === "linux") expect(cleanup.slice(0, 3)).toEqual(["systemctl", "--user", "stop"]);
      else expect(cleanup).toEqual(["taskkill", "/T", "/PID", String(record.pid)]);
    }
    expect(existsSync(path.join(root, "out/development-runtime/manager-operation.lock"))).toBe(false);
  });
}
