function readArg(name) {
  const index = process.argv.indexOf(name);
  if (index === -1) {
    return null;
  }
  const value = process.argv[index + 1];
  if (!value) {
    throw new Error(`缺少命令行参数 ${name} 的值`);
  }
  return value;
}

function commandLine() {
  const args = process.argv.slice(2).filter((arg, index, all) => {
    const previous = all[index - 1];
    return previous !== "--host" && previous !== "--port" && arg !== "--host" && arg !== "--port";
  });
  const line = args.join(" ").trim();
  if (!line) {
    throw new Error("命令不能为空，例如: bun run command -- goto example.com");
  }
  return line;
}

async function main() {
  const host = process.env.REMOTE_SCREEN_COMMAND_HOST || readArg("--host") || "127.0.0.1";
  const port = process.env.REMOTE_SCREEN_PORT || readArg("--port") || "8121";
  const response = await fetch(`http://${host}:${port}/api/command`, {
    method: "POST",
    headers: {
      "content-type": "application/json; charset=utf-8",
    },
    body: JSON.stringify({ line: commandLine() }),
  });
  const payload = await response.json();
  if (!response.ok) {
    throw new Error(payload.error || `命令请求失败: ${response.status}`);
  }
  if (payload.output) {
    console.log(payload.output);
  }
}

main().catch((error) => {
  console.error(`[remote-command] ${error instanceof Error ? error.message : String(error)}`);
  process.exit(1);
});
