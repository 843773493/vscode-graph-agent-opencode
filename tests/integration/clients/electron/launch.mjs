import assert from 'node:assert/strict';
import { createWriteStream } from 'node:fs';
import { mkdir } from 'node:fs/promises';
import { join } from 'node:path';
import { once } from 'node:events';
import { finished } from 'node:stream/promises';
import { _electron } from 'playwright/test';

const required = name => {
  const value = process.env[name];
  if (!value) throw new Error(`Electron 打包验收缺少 ${name}`);
  return value;
};

export async function launchElectron({ run, result, usePage, extraArgs = [], observeWillPreventUnload = false, observe = (_label, operation) => operation() }) {
  const runtime = required('BOXTEAM_ELECTRON_RUNTIME'), artifacts = required('BOXTEAM_ELECTRON_ARTIFACTS');
  const cwd = join(runtime, 'launch-directory'), emptyPath = join(runtime, 'empty-path'), home = join(runtime, 'profile');
  await mkdir(cwd, { recursive: true });
  await mkdir(emptyPath, { recursive: true });
  const native = result.native = { launches: [], exits: [], consoleErrors: [], dialogs: [] };
  result.boundary += '；Linux 真实打包 ASAR，独立 cwd／空 PATH／同 profile 正常关窗重启，无开发服务器或 renderer bridge';
  let current, origin;
  const preventedReloadDialogs = [];
  const close = async instance => {
    await observe('electron.nativeClose', async () => {
      const processHandle = instance.process();
      const exited = once(processHandle, 'exit', { signal: AbortSignal.timeout(30_000) });
      await instance.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].close());
      const [code, signal] = await exited;
      native.exits.push({ code, signal, pid: processHandle.pid });
      assert.equal(code, 0); assert.equal(signal, null);
      if (current === instance) current = undefined;
    });
  };
  const open = async () => {
    const number = native.launches.length + 1;
    const args = ['--no-sandbox', ...extraArgs, `--gateway=${required('BOXTEAM_ELECTRON_GATEWAY')}`];
    const instance = await observe('electron.launch', () => _electron.launch({
      executablePath: required('BOXTEAM_ELECTRON_EXECUTABLE'), args, cwd,
      env: { ...process.env, BOXTEAM_HOME: home, PATH: emptyPath }, timeout: 60_000,
    }));
    const processHandle = instance.process(), logs = [];
    current = instance;
    run.addCleanup(`canonical-native-${number}`, async () => {
      const failures = [];
      try { if (processHandle.exitCode === null && processHandle.signalCode === null) await close(instance); }
      catch (error) { failures.push(error); }
      for (const { channel, log } of logs) {
        processHandle[channel].unpipe(log);
        if (!log.writableEnded) log.end();
        try { await finished(log); } catch (error) { failures.push(error); }
      }
      const storageErrors = native.consoleErrors.filter(entry => /IndexedDB|database.*closed/i.test(entry.text));
      if (storageErrors.length) failures.push(new Error(`原生关闭存储失败：${JSON.stringify(storageErrors)}`));
      if (failures.length) throw new AggregateError(failures, 'Electron 打包验收关闭失败');
    });
    for (const channel of ['stdout', 'stderr']) {
      const log = createWriteStream(join(artifacts, `electron-${number}.${channel}.log`), { flags: 'wx' });
      processHandle[channel].pipe(log); logs.push({ channel, log });
    }
    const location = await instance.evaluate(({ app }) => ({
      appPath: app.getAppPath(), packaged: app.isPackaged, userData: app.getPath('userData'),
      cwd: process.cwd(), path: process.env.PATH,
    }));
    assert.equal(location.packaged, true); assert.ok(location.appPath.endsWith('/resources/app.asar'));
    assert.equal(location.userData, join(home, 'clients/electron'));
    assert.equal(location.cwd, cwd); assert.equal(location.path, emptyPath);
    const launch = { args, location, pid: processHandle.pid };
    native.launches.push(launch);
    const page = await instance.firstWindow();
    usePage(page);
    page.on('console', message => {
      if (message.type() === 'error') native.consoleErrors.push({ launch: number, text: message.text(), location: message.location() });
    });
    page.on('dialog', async dialog => {
      native.dialogs.push({ launch: number, type: dialog.type(), message: dialog.message() });
      if (observeWillPreventUnload && dialog.type() === 'beforeunload') {
        preventedReloadDialogs.push(dialog);
        return;
      }
      try {
        if (dialog.type() === 'beforeunload') await dialog.accept();
        else await dialog.dismiss();
      } catch (cause) {
        if (!String(cause).includes('Protocol error (Page.handleJavaScriptDialog): No dialog is showing')) throw cause;
        native.dialogs.push({ launch: number, type: `${dialog.type()}-settled`, driverError: String(cause) });
      }
    });
    await page.waitForURL(url => url.protocol === 'http:' && url.hostname === '127.0.0.1'
      && url.pathname === '/' && url.search === '', { waitUntil: 'domcontentloaded' });
    const nextOrigin = new URL(page.url()).origin;
    if (origin) assert.equal(nextOrigin, origin);
    else origin = nextOrigin;
    launch.url = page.url();
    if (observeWillPreventUnload) {
      await instance.evaluate(({ BrowserWindow }) => {
        const contents = BrowserWindow.getAllWindows()[0].webContents;
        contents.__boxteamTestWillPreventUnload = [];
        contents.on('will-prevent-unload', event => {
          contents.__boxteamTestWillPreventUnload.push({ defaultPrevented: event.defaultPrevented });
        });
      });
    }
    assert.deepEqual(await page.evaluate(() => ({ require: typeof globalThis.require, process: typeof globalThis.process })),
      { require: 'undefined', process: 'undefined' });
  };
  await open();
  return { baseUrl: origin,
    async willPreventUnloadEvents() {
      if (!observeWillPreventUnload || !current) throw new Error('Electron will-prevent-unload observation is unavailable');
      native.willPreventUnload = await current.evaluate(({ BrowserWindow }) => [...BrowserWindow.getAllWindows()[0].webContents.__boxteamTestWillPreventUnload]);
      return native.willPreventUnload;
    },
    async settlePreventedReload() {
      assert.ok(preventedReloadDialogs.length, '缺少实际 beforeunload 对话框');
      const state = await current.evaluate(({ BrowserWindow }) => {
        const contents = BrowserWindow.getAllWindows()[0].webContents;
        return { loading: contents.isLoading(), events: contents.__boxteamTestWillPreventUnload };
      });
      assert.equal(state.loading, false);
      assert.equal(state.events.at(-1)?.defaultPrevented, false);
      for (const dialog of preventedReloadDialogs.splice(0)) {
        try { await dialog.dismiss(); }
        catch (cause) {
          // Electron 45 已完成原生否决；dismiss 仍须结清 Playwright 的导航等待。
          if (!String(cause).includes('Protocol error (Page.handleJavaScriptDialog): No dialog is showing')) throw cause;
          native.dialogs.push({ launch: native.launches.length, type: 'beforeunload-settled', nativePrevented: true, driverError: String(cause) });
        }
      }
    },
    async reload() { await close(current); await open(); },
    async leave() { await close(current); },
    async restore() { await open(); },
  };
}
