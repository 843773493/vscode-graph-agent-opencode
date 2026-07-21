export const TARGET_COMMANDS = new Set([
  "provision",
  "sync",
  "activate",
  "bootstrap",
  "start",
  "stop",
  "restart",
  "status",
  "shell",
  "test",
  "collect",
]);

const VALUE_OPTIONS = new Set([
  "--config",
  "--profile",
  "--boxteam-home",
  "--workspace",
  "--output",
]);
const FLAG_OPTIONS = new Set(["--activate", "--no-env", "--rebuild", "--submodules"]);

export function parseTargetCliArgs(args) {
  if (args.length === 0 || args.includes("--help") || args.includes("-h")) {
    return { help: true };
  }
  const [command, targetId, ...rest] = args;
  if (!TARGET_COMMANDS.has(command)) throw new Error(`未知目标命令: ${command}`);
  if (!targetId || targetId.startsWith("-")) throw new Error(`${command} 必须提供 target-id`);

  const options = {
    profile: "development",
    activate: false,
    copyEnv: true,
    rebuild: false,
    submodules: false,
    passthrough: [],
  };
  for (let index = 0; index < rest.length; index += 1) {
    const argument = rest[index];
    if (argument === "--") {
      options.passthrough = rest.slice(index + 1);
      break;
    }
    if (VALUE_OPTIONS.has(argument)) {
      const value = rest[index + 1];
      if (!value || value.startsWith("--")) throw new Error(`${argument} 必须提供值`);
      index += 1;
      const key = {
        "--config": "configPath",
        "--profile": "profile",
        "--boxteam-home": "boxteamHome",
        "--workspace": "workspace",
        "--output": "output",
      }[argument];
      options[key] = value;
      continue;
    }
    if (FLAG_OPTIONS.has(argument)) {
      if (argument === "--activate") options.activate = true;
      if (argument === "--no-env") options.copyEnv = false;
      if (argument === "--rebuild") options.rebuild = true;
      if (argument === "--submodules") options.submodules = true;
      continue;
    }
    throw new Error(`未知参数: ${argument}`);
  }
  if (!new Set(["development", "installed"]).has(options.profile)) {
    throw new Error(`--profile 仅支持 development 或 installed: ${options.profile}`);
  }
  if (options.passthrough.length > 0 && !new Set(["test", "shell"]).has(command)) {
    throw new Error(`只有 test 和 shell 支持 -- 后的透传参数`);
  }
  return { help: false, command, targetId, options };
}

export function targetCliUsage() {
  return [
    "用法: bun run scripts/cross-platform-development-target.mjs <command> <target-id> [options]",
    "命令: provision | sync | activate | bootstrap | start | stop | restart | status | shell | test | collect",
    "选项: --config <path> --profile <development|installed> --activate --no-env",
    "      --boxteam-home <path> --workspace <path> --output <path> --rebuild --submodules",
  ].join("\n");
}
