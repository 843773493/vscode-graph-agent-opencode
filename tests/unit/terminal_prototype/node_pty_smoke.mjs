import path from "node:path";

let pty;
try {
  ({ default: pty } = await import("node-pty"));
} catch (error) {
  console.error(JSON.stringify({
    ok: false,
    error: "node-pty not installed",
    detail: String(error),
  }));
  process.exit(2);
}

const shell = process.platform === "win32"
  ? (process.env.COMSPEC || "cmd.exe")
  : (process.env.SHELL || "/bin/sh");
const commandArgs = process.platform === "win32"
  ? ["/d", "/s", "/c", "echo node-pty-ok"]
  : ["-c", "printf node-pty-ok"];
const cwd = process.argv[2] ? path.resolve(process.argv[2]) : process.cwd();
const terminal = pty.spawn(shell, commandArgs, {
  name: "xterm-color",
  cols: 80,
  rows: 24,
  cwd,
  env: { ...process.env },
});

let output = "";
let exited = false;
const timeout = setTimeout(() => {
  if (exited) return;
  terminal.kill();
  console.error(JSON.stringify({ ok: false, error: "timeout", output: output.trim() }));
  process.exit(3);
}, 5000);

terminal.onData((data) => {
  output += data;
});

terminal.onExit(({ exitCode, signal }) => {
  exited = true;
  clearTimeout(timeout);
  const ok = exitCode === 0 && output.includes("node-pty-ok");
  console.log(JSON.stringify({
    ok,
    shell,
    cwd,
    exitCode,
    signal,
    output: output.trim(),
  }));
  if (!ok) process.exitCode = 1;
});
