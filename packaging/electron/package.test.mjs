import { expect, test } from 'bun:test';
import { spawnSync } from 'node:child_process';
import { createHash, randomUUID } from 'node:crypto';
import { chmodSync, mkdirSync, readFileSync, statSync, symlinkSync, writeFileSync } from 'node:fs';
import path from 'node:path';

const outputRoot = path.resolve('out/tests/packaging/electron/package');
const runId = randomUUID();
const entry = new URL('./package.mjs', import.meta.url).href;

function fixture(scenario) {
  const workspace = path.join(outputRoot, 'workspace', runId, scenario);
  const artifacts = path.join(outputRoot, 'artifacts', runId, scenario);
  const output = path.join(workspace, '发布 with spaces %&');
  const bundle = path.join(output, 'bundles', 'BoxTeam 中文.app');
  mkdirSync(artifacts, { recursive: true });
  const json = (relative, value) => {
    const file = path.join(workspace, relative);
    mkdirSync(path.dirname(file), { recursive: true });
    writeFileSync(file, JSON.stringify(value));
  };
  json(path.relative(workspace, path.join(output, 'app/package.json')), { version: '1.2.3' });
  json(path.relative(workspace, path.join(output, 'app/build.json')), { sourceFingerprint: 'fixture-source' });
  json('src/clients/electron/package.json', { devDependencies: { electron: '39.0.0' } });
  json('src/clients/electron/node_modules/electron/checksums.json', {});
  const driver = path.join(artifacts, 'packager-driver.mjs');
  // 只替代 Electron 下载和平台 bundle 生成；归档、摘要和清单走真实发布入口。
  writeFileSync(driver, `import { mock } from 'bun:test';
mock.module('@electron/packager', () => ({ packager: async () => [${JSON.stringify(bundle)}] }));
process.argv = [process.execPath, ${JSON.stringify(entry)}, '--project-root', ${JSON.stringify(workspace)}, '--output-root', ${JSON.stringify(output)}];
await import(${JSON.stringify(entry)});
`);
  function run(label, command, environment = process.env) {
    const result = spawnSync(command[0], command.slice(1), {
      cwd: workspace, env: environment, encoding: 'utf8', timeout: 15_000,
    });
    writeFileSync(path.join(artifacts, `${label}.stdout.raw`), result.stdout ?? '');
    writeFileSync(path.join(artifacts, `${label}.stderr.raw`), result.stderr ?? '');
    writeFileSync(path.join(artifacts, `${label}.result.json`), JSON.stringify({ command, cwd: workspace,
      status: result.status, signal: result.signal, error: result.error?.message ?? null }));
    expect(result.error).toBeUndefined();
    expect(result.signal).toBeNull();
    return result;
  }
  return { workspace, artifacts, output, bundle, run,
    package() { return run('package', [process.execPath, driver], { ...process.env, PATH: '' }); },
  };
}

test('没有外部 tar 时仍生成可迁移归档，保留正文、执行位、隐藏文件、空目录和符号链接', () => {
  const probe = fixture('roundtrip');
  mkdirSync(path.join(probe.bundle, 'empty'), { recursive: true });
  mkdirSync(path.join(probe.bundle, 'Contents/Frameworks'), { recursive: true });
  const binary = Buffer.alloc(1024 * 1024);
  for (let index = 0; index < binary.length; index++) binary[index] = index % 251;
  const executable = path.join(probe.bundle, 'boxteam');
  writeFileSync(executable, binary);
  chmodSync(executable, 0o755);
  writeFileSync(path.join(probe.bundle, '.hidden'), '隐藏内容\n');
  if (process.platform !== 'win32') {
    symlinkSync('../../boxteam', path.join(probe.bundle, 'Contents/Frameworks/Current'));
  }
  const result = probe.package();
  expect(result.status).toBe(0);
  const manifest = JSON.parse(readFileSync(path.join(probe.output, 'release.json'), 'utf8'));
  expect(manifest.sourceFingerprint).toBe('fixture-source');
  expect(manifest.platform).toBe(process.platform);
  expect(manifest.arch).toBe(process.arch);
  expect(manifest.archives).toHaveLength(1);
  const { archive, sha256, bundle } = manifest.archives[0];
  expect(bundle).toBe(probe.bundle);
  expect(path.basename(archive)).toBe(`BoxTeam-1.2.3-${process.platform}-${process.arch}.tar.gz`);
  expect(sha256).toBe(createHash('sha256').update(readFileSync(archive)).digest('hex'));
  expect(readFileSync(`${archive}.sha256`, 'utf8')).toBe(`${sha256}  ${path.basename(archive)}\n`);
  const verifier = path.join(probe.artifacts, 'verify-archive.py');
  // 独立标准库核对归档并迁移解压，不让写入库和读取库互相掩盖问题。
  writeFileSync(verifier, `import hashlib, os, sys, tarfile
from pathlib import Path
archive, output, top, digest, mode = sys.argv[1:]
with tarfile.open(archive, 'r:gz') as source:
    names = set(source.getnames())
    assert names == {top, top+'/boxteam', top+'/.hidden', top+'/empty', top+'/Contents', top+'/Contents/Frameworks'} | ({top+'/Contents/Frameworks/Current'} if os.name != 'nt' else set())
    assert source.getmember(top+'/boxteam').mode & 0o777 == int(mode)
    source.extractall(output, filter='data')
root = Path(output) / top
assert hashlib.sha256((root/'boxteam').read_bytes()).hexdigest() == digest
assert (root/'.hidden').read_text() == '隐藏内容\\n'
assert (root/'empty').is_dir()
if os.name != 'nt':
    link = root/'Contents/Frameworks/Current'
    assert link.is_symlink() and os.readlink(link) == '../../boxteam'
    assert link.read_bytes() == (root/'boxteam').read_bytes()
`);
  expect(probe.run('verify', ['uv', 'run', '--no-sync', '--project', process.cwd(), 'python', verifier, archive,
    path.join(probe.workspace, '迁移位置'), path.basename(bundle),
    createHash('sha256').update(binary).digest('hex'), String(statSync(executable).mode & 0o777)]).status).toBe(0);
});

test('bundle 缺失时归档失败，不能生成摘要或成功清单', () => {
  const probe = fixture('missing-bundle');
  mkdirSync(path.dirname(probe.bundle), { recursive: true });
  const result = probe.package();
  expect(result.status).not.toBe(0);
  expect(result.stderr).toContain('ENOENT');
  expect(() => readFileSync(path.join(probe.output, 'release.json'))).toThrow();
  const archive = path.join(probe.output, 'releases', `BoxTeam-1.2.3-${process.platform}-${process.arch}.tar.gz`);
  expect(() => readFileSync(`${archive}.sha256`)).toThrow();
});
