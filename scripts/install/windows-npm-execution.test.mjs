import { execFileSync } from "node:child_process";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { randomUUID } from "node:crypto";

import { expect, test } from "bun:test";

import {
  runReleaseInstall,
  verifyInstalledRelease,
} from "./install-release.mjs";

const fixtureRunRoot = path.resolve(
  "out/tests/scripts/install/windows-npm-execution/workspace",
  randomUUID(),
);
const packageVersion = "1.2.3";

function createInstalledReleaseFixture(caseName) {
  const fixtureRoot = path.join(fixtureRunRoot, caseName, "发行安装 工作区");
  const globalRoot = path.join(fixtureRoot, "npm 全局目录");
  const launcherRoot = path.join(globalRoot, "boxteam");
  const runtimeRoot = path.join(globalRoot, "@boxteam", "runtime-windows-x64");
  const requiredFiles = [
    "python.exe",
    "application",
    "web-assets",
    "chromium.exe",
  ];

  mkdirSync(path.join(launcherRoot, "bin"), { recursive: true });
  mkdirSync(runtimeRoot, { recursive: true });
  writeFileSync(
    path.join(launcherRoot, "package.json"),
    JSON.stringify({ name: "boxteam", version: packageVersion }),
  );
  writeFileSync(path.join(launcherRoot, "bin", "boxteam.mjs"), "");
  writeFileSync(
    path.join(runtimeRoot, "package.json"),
    JSON.stringify({ name: "@boxteam/runtime-windows-x64", version: packageVersion }),
  );
  writeFileSync(
    path.join(runtimeRoot, "runtime-manifest.json"),
    JSON.stringify({
      python_executable: requiredFiles[0],
      application_root: requiredFiles[1],
      web_assets: requiredFiles[2],
      chromium_executable: requiredFiles[3],
    }),
  );
  for (const relativePath of requiredFiles) {
    writeFileSync(path.join(runtimeRoot, relativePath), "");
  }
  return { fixtureRoot, globalRoot };
}

function simulateWindowsNpmInvocation({ fixtureRoot, globalRoot }) {
  const launcherTarball = path.join(fixtureRoot, "boxteam release.tgz");
  const runtimeTarball = path.join(fixtureRoot, "runtime & assets.tgz");
  writeFileSync(launcherTarball, "launcher");
  writeFileSync(runtimeTarball, "runtime");
  const source = `
    import childProcess from "node:child_process";
    import { syncBuiltinESMExports } from "node:module";
    const [
      moduleUrl,
      installRoot,
      workingDirectory,
      installPrefix,
      launcherTarball,
      runtimeTarball,
    ] = JSON.parse(process.argv[1]);
    Object.defineProperty(process, "platform", { value: "win32" });
    const calls = [];
    childProcess.spawnSync = (command, args, options) => {
      calls.push({
        command,
        args,
        windowsVerbatimArguments: options.windowsVerbatimArguments,
      });
      return {
        status: 0,
        stdout: args.some((argument) => argument.includes("root"))
          ? installRoot
          : "",
        stderr: "",
      };
    };
    syncBuiltinESMExports();
    const { runReleaseInstall, verifyInstalledRelease } = await import(moduleUrl);
    runReleaseInstall({
      command: "npm.cmd",
      args: [
        "install",
        "--global",
        "--prefix",
        installPrefix,
        launcherTarball,
        runtimeTarball,
      ],
      cwd: workingDirectory,
    });
    verifyInstalledRelease({
      command: {
        command: "npm.cmd",
        prefix: installPrefix,
        targetPlatform: "windows-x64",
        version: "${packageVersion}",
      },
      cwd: workingDirectory,
      environment: process.env,
    });
    console.log(JSON.stringify(calls));
  `;
  const output = execFileSync(
    process.execPath,
    [
      "--input-type=module",
      "--eval",
      source,
      JSON.stringify([
        new URL("./install-release.mjs", import.meta.url).href,
        globalRoot,
        fixtureRoot,
        path.join(fixtureRoot, "prefix with spaces & symbols"),
        launcherTarball,
        runtimeTarball,
      ]),
    ],
    { encoding: "utf8", timeout: 5000 },
  );
  return {
    calls: JSON.parse(output),
    installPrefix: path.join(fixtureRoot, "prefix with spaces & symbols"),
    launcherTarball,
    runtimeTarball,
  };
}

test("Windows install 和 root 核验都经 cross-spawn 转义 cmd 参数", () => {
  const fixture = createInstalledReleaseFixture("simulated-windows");
  const { calls, installPrefix, launcherTarball, runtimeTarball } =
    simulateWindowsNpmInvocation(fixture);
  const escapeWindowsArgument = (value) =>
    value.replaceAll("&", "^&").replaceAll(" ", "^ ");

  expect(calls).toHaveLength(2);
  for (const call of calls) {
    expect(call.command.toLowerCase()).toMatch(/(?:^|\\)cmd\.exe$/);
    expect(call.args.slice(0, 3)).toEqual(["/d", "/s", "/c"]);
    expect(call.windowsVerbatimArguments).toBe(true);
  }
  expect(calls[0].args[3]).toContain('npm.cmd ^"install^"');
  expect(calls[0].args[3]).toContain(escapeWindowsArgument(installPrefix));
  expect(calls[0].args[3]).toContain(escapeWindowsArgument(launcherTarball));
  expect(calls[0].args[3]).toContain(escapeWindowsArgument(runtimeTarball));
  expect(calls[1].args[3]).toContain('npm.cmd ^"root^" ^"--global^"');
  expect(calls[1].args[3]).toContain(escapeWindowsArgument(installPrefix));
});

if (process.platform === "win32") {
  test("Windows 原生 npm.cmd shim 可完成安装与安装后目录核验", () => {
    const { fixtureRoot, globalRoot } = createInstalledReleaseFixture(
      "native-windows",
    );
    const installPrefix = path.join(fixtureRoot, "prefix with spaces & symbols");
    const markerPath = path.join(fixtureRoot, "npm-calls.txt");
    const commandPath = path.join(fixtureRoot, "npm.cmd");
    const launcherTarball = path.join(fixtureRoot, "boxteam release.tgz");
    const runtimeTarball = path.join(fixtureRoot, "runtime & assets.tgz");
    writeFileSync(launcherTarball, "launcher");
    writeFileSync(runtimeTarball, "runtime");
    const environment = {
      ...process.env,
      BOXTEAM_NPM_TEST_ROOT: globalRoot,
      BOXTEAM_NPM_TEST_CALLS: markerPath,
    };
    writeFileSync(
      commandPath,
      [
        "@echo off",
        'if "%~1"=="root" (',
        '  >>"%BOXTEAM_NPM_TEST_CALLS%" echo root:"%~4"',
        "  echo %BOXTEAM_NPM_TEST_ROOT%",
        "  exit /b 0",
        ")",
        'if "%~1"=="install" (',
        '  >>"%BOXTEAM_NPM_TEST_CALLS%" echo install:"%~4"',
        '  >>"%BOXTEAM_NPM_TEST_CALLS%" echo launcher:"%~5"',
        '  >>"%BOXTEAM_NPM_TEST_CALLS%" echo runtime:"%~6"',
        "  exit /b 0",
        ")",
        "exit /b 17",
        "",
      ].join("\r\n"),
    );

    runReleaseInstall({
      command: commandPath,
      args: [
        "install",
        "--global",
        "--prefix",
        installPrefix,
        launcherTarball,
        runtimeTarball,
      ],
      cwd: fixtureRoot,
      environment,
    });
    verifyInstalledRelease({
      command: {
        command: commandPath,
        prefix: installPrefix,
        targetPlatform: "windows-x64",
        version: packageVersion,
      },
      cwd: fixtureRoot,
      environment,
    });

    expect(readFileSync(markerPath, "utf8")).toBe(
      `install:"${installPrefix}"\r\n` +
        `launcher:"${launcherTarball}"\r\n` +
        `runtime:"${runtimeTarball}"\r\n` +
        `root:"${installPrefix}"\r\n`,
    );
  });
}
