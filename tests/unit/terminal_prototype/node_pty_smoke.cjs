const path = require('node:path');

let pty;
try {
  pty = require('node-pty');
} catch (error) {
  console.error(JSON.stringify({ ok: false, error: 'node-pty not installed', detail: String(error) }));
  process.exit(2);
}

const shell = process.platform === 'win32'
  ? (process.env.COMSPEC || 'cmd.exe')
  : (process.env.SHELL || '/bin/sh');

const cwd = process.argv[2] ? path.resolve(process.argv[2]) : process.cwd();
const env = { ...process.env };

const terminal = pty.spawn(
  shell,
  process.platform === 'win32'
    ? ['/d', '/s', '/c', 'echo node-pty-ok']
    : ['-lc', 'printf node-pty-ok'],
  {
  name: 'xterm-color',
  cols: 80,
  rows: 24,
  cwd,
  env,
  },
);

let output = '';
let exited = false;

terminal.onData((data) => {
  output += data;

  if (process.platform === 'win32' && output.includes('node-pty-ok')) {
    terminal.kill();
  }
});

terminal.onExit(({ exitCode, signal }) => {
  exited = true;
  console.log(JSON.stringify({
    ok: true,
    shell,
    cwd,
    exitCode,
    signal,
    output: output.trim(),
  }));
});

setTimeout(() => {
  if (!exited) {
    terminal.kill();
    console.error(JSON.stringify({ ok: false, error: 'timeout', output: output.trim() }));
    process.exit(3);
  }
}, 5000);