import { readFileSync } from "node:fs";

import { initializeUserConfiguration } from "./config-bootstrap.mjs";
import { defaultBoxteamHome } from "./config-bootstrap.mjs";
import { buildDoctorReport, printDoctorReport } from "./doctor.mjs";
import { issueFederationToken } from "./federation-token.mjs";
import { superviseGateway } from "./gateway-supervisor.mjs";
import { acquireLauncherLock } from "./instance-lock.mjs";
import { discoverRuntime } from "./runtime-discovery.mjs";

const packageMetadata = JSON.parse(
  readFileSync(new URL("../package.json", import.meta.url), "utf8"),
);

export async function main(args) {
  if (args.includes("--version") || args.includes("-v")) {
    process.stdout.write(`${packageMetadata.version}\n`);
    return;
  }
  const command = args[0]?.startsWith("-") ? "start" : (args[0] ?? "start");
  if (!new Set(["start", "doctor", "config", "gateway"]).has(command)) {
    throw new Error(`未知命令: ${command}`);
  }
  const commandArgs = command === args[0] ? args.slice(1) : args;
  const manifestIndex = commandArgs.indexOf("--runtime-manifest");
  const environment = { ...process.env };
  if (manifestIndex !== -1) {
    const manifestPath = commandArgs[manifestIndex + 1];
    if (!manifestPath || manifestPath.startsWith("-")) {
      throw new Error("--runtime-manifest 必须提供路径");
    }
    environment.BOXTEAM_RUNTIME_MANIFEST = manifestPath;
  }
  const effectiveArgs =
    manifestIndex === -1
      ? commandArgs
      : commandArgs.filter(
          (_, index) => index !== manifestIndex && index !== manifestIndex + 1,
        );
  const runtime = discoverRuntime({ environment });
  const boxteamHome =
    environment.BOXTEAM_HOME?.trim() ||
    defaultBoxteamHome(runtime.distribution);
  if (command === "doctor") {
    const invalidDoctorArgs = effectiveArgs.filter((arg) => arg !== "--json");
    if (invalidDoctorArgs.length > 0) {
      throw new Error(`未知 doctor 参数: ${invalidDoctorArgs.join(", ")}`);
    }
    printDoctorReport(
      buildDoctorReport({ runtime, boxteamHome }),
      { json: effectiveArgs.includes("--json") },
    );
    return;
  }
  if (command === "gateway") {
    if (effectiveArgs[0] !== "issue-federation-token") {
      throw new Error(
        "未知 gateway 子命令；当前支持 issue-federation-token",
      );
    }
    const output = issueFederationToken({
      runtime,
      environment: {
        ...environment,
        BOXTEAM_HOME: boxteamHome,
      },
      args: effectiveArgs.slice(1),
    });
    if (output) {
      process.stdout.write(`${output}\n`);
    }
    return;
  }
  if (command === "config") {
    if (
      effectiveArgs[0] !== "init" ||
      effectiveArgs.slice(1).some((arg) => arg !== "--force")
    ) {
      throw new Error("未知 config 参数；当前支持 init [--force]");
    }
    const bootstrap = initializeUserConfiguration({
      runtime,
      environment: {
        ...environment,
        BOXTEAM_HOME: boxteamHome,
      },
      args: effectiveArgs.includes("--force") ? ["--force"] : [],
    });
    if (bootstrap.output) {
      process.stdout.write(`${bootstrap.output}\n`);
    }
    return;
  }
  const unknownArgs = effectiveArgs.filter((arg) => arg !== "--no-open");
  if (unknownArgs.length > 0) {
    throw new Error(`未知 start 参数: ${unknownArgs.join(", ")}`);
  }
  const lock = acquireLauncherLock({
    boxteamHome,
    runtime,
  });
  try {
    const bootstrap = initializeUserConfiguration({
      runtime,
      environment: {
        ...environment,
        BOXTEAM_HOME: boxteamHome,
      },
    });
    if (bootstrap.output) {
      process.stdout.write(`配置: ${bootstrap.output}\n`);
    }
    const exitCode = await superviseGateway({
      runtime,
      environment: bootstrap.environment,
      openBrowser: !effectiveArgs.includes("--no-open"),
    });
    process.exitCode = exitCode;
  } finally {
    lock.release();
  }
}
