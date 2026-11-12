import { expect, test } from "bun:test";
import { spawnSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import { chmodSync, mkdirSync, writeFileSync } from "node:fs";
import path from "node:path";
import { dockerComposeCommand } from "../../tools/cross-platform-development-targets/docker-target.mjs";
import { runChecked } from "../../tools/cross-platform-development-targets/process.mjs";

const outputRoot = path.resolve("out/tests/scripts/launch/docker-command-prefix");
const runId = randomUUID();
const workspace = path.join(outputRoot, "workspace", runId);
const artifacts = path.join(outputRoot, "artifacts", runId);
mkdirSync(workspace, { recursive: true });
mkdirSync(artifacts, { recursive: true });
const target = { docker: { composeProject: "fixture-project" } };

function withPrefix(raw, operation) {
  const original = process.env.BOXTEAM_DOCKER_COMMAND;
  if (raw === null) delete process.env.BOXTEAM_DOCKER_COMMAND;
  else process.env.BOXTEAM_DOCKER_COMMAND = raw;
  try { return operation(); }
  finally {
    if (original === undefined) delete process.env.BOXTEAM_DOCKER_COMMAND;
    else process.env.BOXTEAM_DOCKER_COMMAND = original;
  }
}

function composeCommand() {
  return dockerComposeCommand({ target, projectRoot: workspace }, "ps", "--all");
}

test("Docker前缀与Python真实入口保留引号、转义、空参数并拒绝未闭合输入", () => {
  const cases = [null, "docker", "sudo -n docker", "  docker\t--context\tdesktop-linux  ",
    '"/opt/Docker Tools/docker" --context "开发 环境"',
    String.raw`'C:\Program Files\Docker\docker.exe' --context '开发 环境'`,
    String.raw`docker --context 开发\ 环境`,
    String.raw`docker --label "Peter's Docker" --empty ''`,
    "docker --label '$HOME ${USER} $(echo nope) ; & | * ? # %PATH%'",
    String.raw`docker --label $'literal\n'`,
    "", "   ", "''", "'' --context fixture", 'docker "unfinished', "docker 'unfinished", "docker \\",
  ];
  const words = ["中文 空格", String.raw`C:\Program Files\Docker`, "apostrophe's", 'double"quote',
    "$HOME", "${USER}", "$(echo nope)", "a;&|*?#%", "", "line\ninside", "tab\tinside", "back\\slash"];
  const inputPath = path.join(artifacts, "input.json");
  writeFileSync(inputPath, JSON.stringify({ cases, arguments: words.flatMap(left => words.map(right => ["docker", left, right])) }));
  const observerPath = path.join(artifacts, "python-observer.py");
  // 直接调用既有Python入口；生成用shlex.join，不能以JS解析结果充当期望值。
  writeFileSync(observerPath, `import json, os, shlex, sys
from pathlib import Path
from tests.e2e.system.gateway.gateway_docker import docker_command_prefix
source=json.loads(Path(sys.argv[1]).read_text())
records=[]
for raw in source['cases'] + [shlex.join(argv) for argv in source['arguments']]:
    if raw is None:
        os.environ.pop('BOXTEAM_DOCKER_COMMAND', None)
    else:
        os.environ['BOXTEAM_DOCKER_COMMAND']=raw
    try:
        value=docker_command_prefix()
    except ValueError:
        records.append({'raw':raw, 'error':True})
    else:
        records.append({'raw':raw, 'value':value})
assert [record['value'] for record in records[len(source['cases']):]] == source['arguments']
print(json.dumps(records, ensure_ascii=False))
`);
  const command = ["uv", "run", "--no-sync", "python", observerPath, inputPath];
  const run = spawnSync(command[0], command.slice(1), {
    cwd: process.cwd(), env: { ...process.env, PYTHONPATH: process.cwd() }, encoding: "utf8", timeout: 15000,
  });
  writeFileSync(path.join(artifacts, "python.stdout.raw"), run.stdout ?? "");
  writeFileSync(path.join(artifacts, "python.stderr.raw"), run.stderr ?? "");
  writeFileSync(path.join(artifacts, "python.result.json"), JSON.stringify({ command, cwd: process.cwd(),
    status: run.status, signal: run.signal, error: run.error?.message ?? null }));
  expect(run.status).toBe(0);
  const observations = [];
  for (const record of JSON.parse(run.stdout)) {
    withPrefix(record.raw, () => {
      if (record.error) expect(composeCommand).toThrow();
      else {
        const actual = composeCommand();
        observations.push(actual);
        expect(actual).toEqual([...record.value, "compose", "-f",
          path.join(workspace, "tools/cross-platform-development-targets/docker/compose.yml"),
          "-p", "fixture-project", "ps", "--all"]);
      }
    });
  }
  writeFileSync(path.join(artifacts, "javascript-commands.json"), JSON.stringify(observations, null, 2));
});

test.skipIf(process.platform === "win32")("带空格的前缀经真实命令入口执行，参数不被shell二次解释", () => {
  const executable = path.join(workspace, "Docker 中文 tools");
  writeFileSync(executable, `#!${process.execPath}\nconsole.log(JSON.stringify(process.argv.slice(2)));\n`);
  chmodSync(executable, 0o755);
  // 双引号中的单引号是普通字符，$和分号始终只作为参数交付。
  const result = withPrefix(`'${executable}' --context "开发环境's $HOME; &"`, () =>
    runChecked(composeCommand(), { cwd: workspace, label: "Docker前缀fixture" }));
  writeFileSync(path.join(artifacts, "command-result.json"), JSON.stringify(result, null, 2));
  expect(JSON.parse(result.stdout)).toEqual(["--context", "开发环境's $HOME; &", "compose", "-f",
    path.join(workspace, "tools/cross-platform-development-targets/docker/compose.yml"),
    "-p", "fixture-project", "ps", "--all"]);
});
