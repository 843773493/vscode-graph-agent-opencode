import { app, BrowserWindow, session } from 'electron';
import { mkdirSync } from 'node:fs';
import { access, readFile, writeFile } from 'node:fs/promises';
import { homedir } from 'node:os';
import { resolve, join } from 'node:path';
import { once } from 'node:events';
import { createWorkbenchServer } from '../../workbench/scripts/server.mjs';

const profile = resolve(process.env.BOXTEAM_HOME || join(homedir(), '.boxteams'), 'clients/electron');
mkdirSync(profile, { recursive: true });
app.setPath('userData', profile);
app.setPath('sessionData', profile);
app.setName('BoxTeam');

if (!app.requestSingleInstanceLock()) {
  app.exit(0);
} else {
  // 入口求值必须先结束，Electron 才能派发 ready；不能顶层等待 whenReady。
  void start().catch(cause => {
    console.error('Electron 工作台启动失败', cause);
    app.exit(1);
  });
}

async function start() {
  // Electron 会重排 Chromium 开关和应用位置，不能对 process.argv 固定切片。
  const gatewayValue = app.commandLine.getSwitchValue('gateway');
  if (!gatewayValue) throw new Error('请通过 --gateway=<HTTP(S)地址> 指定已有 Workspace Gateway');
  const gateway = new URL(gatewayValue);
  if (!['http:', 'https:'].includes(gateway.protocol) || gateway.username || gateway.password) {
    throw new Error('Gateway 必须是无内嵌凭据的 HTTP(S) 地址');
  }
  await app.whenReady();
  const stateFile = join(profile, 'renderer-origin.json');
  const saved = await readFile(stateFile, 'utf8').catch(cause => {
    if (cause.code === 'ENOENT') return null;
    throw cause;
  });
  const state = saved === null ? null : JSON.parse(saved);
  if (state !== null && (state.version !== 1 || !Number.isInteger(state.port) || state.port < 1 || state.port > 65535)) {
    throw new Error(`Electron 页面来源运行态损坏：${stateFile}`);
  }
  const rendererRoot = join(app.getAppPath(), 'renderer');
  await access(join(rendererRoot, 'index.html'));
  const server = createWorkbenchServer({ root: rendererRoot, gateway: gateway.href });
  server.listen(state?.port ?? 0, '127.0.0.1');
  await once(server, 'listening');
  const address = server.address();
  if (!address || typeof address === 'string') throw new Error('Electron 页面服务未绑定回环端口');
  const origin = `http://127.0.0.1:${address.port}`;
  if (!state) await writeFile(stateFile, JSON.stringify({ version: 1, port: address.port }), { flag: 'wx', mode: 0o600 });
  server.on('error', cause => { console.error('Electron 页面服务失败', cause); app.exit(1); });

  const partition = session.fromPartition('persist:boxteam');
  partition.setPermissionRequestHandler((_contents, permission, callback, details) => {
    callback(details.requestingUrl.startsWith(origin + '/') && permission === 'clipboard-sanitized-write');
  });
  let quitting = false;
  app.on('before-quit', () => { quitting = true; });
  app.on('will-quit', () => { server.close(); server.closeAllConnections(); });
  app.on('window-all-closed', () => { if (process.platform !== 'darwin' || quitting) app.quit(); });
  app.on('activate', () => { if (!BrowserWindow.getAllWindows().length) openWindow(); });
  app.on('second-instance', () => {
    const window = BrowserWindow.getAllWindows()[0];
    if (window?.isMinimized()) window.restore();
    window?.focus();
  });
  const webPreferences = { partition: 'persist:boxteam', nodeIntegration: false, contextIsolation: true, sandbox: true, webviewTag: false };

  /** @param {BrowserWindow} window @param {string} navigationOrigin */
  function attachWindow(window, navigationOrigin) {
    const contents = window.webContents;
    /** @param {Electron.Event} event @param {string} url */
    const restrictNavigation = (event, url) => { if (new URL(url).origin !== navigationOrigin) event.preventDefault(); };
    contents.on('will-navigate', restrictNavigation);
    contents.on('will-redirect', restrictNavigation);
    contents.setWindowOpenHandler(({ url }) => {
      if (['https:', 'http:'].includes(new URL(url).protocol)) {
        return { action: 'allow', overrideBrowserWindowOptions: { webPreferences } };
      }
      console.error(`拒绝不受支持的窗口协议：${new URL(url).protocol}`);
      return { action: 'deny' };
    });
    contents.on('will-attach-webview', event => event.preventDefault());
    contents.on('did-create-window', (child, details) => attachWindow(child, new URL(details.url).origin));
    contents.on('render-process-gone', (_event, details) => {
      console.error('Electron renderer 退出', details);
      app.exit(1);
    });
    let closing = false;
    let allowClose = false;
    contents.on('will-prevent-unload', () => {
      // 工作台可能因未保存文件否决关闭，下一次仍需重新提交缓存。
      closing = false;
      allowClose = false;
      quitting = false;
    });
    window.on('close', event => {
      if (allowClose) return;
      event.preventDefault();
      if (closing) return;
      closing = true;
      void (async () => {
        // 固定的 renderer 提交函数，不向页面暴露原生执行或文件系统入口。
        await contents.executeJavaScript('globalThis.boxteamWorkbenchLifecycle?.prepareToClose()');
        partition.flushStorageData();
        // 继续原生关闭，让原版工作台处理 beforeunload、视图保存及未保存文件。
        allowClose = true;
        window.close();
      })().catch(cause => {
        closing = false;
        quitting = false;
        console.error('关闭窗口前保存历史缓存失败', cause);
        const message = `会话历史缓存未保存，窗口保持打开。请处理错误后重试关闭。\n${cause instanceof Error ? cause.message : String(cause)}`;
        void contents.executeJavaScript(`document.getElementById('startup-error').textContent = ${JSON.stringify(message)}`)
          .catch(error => console.error('显示缓存保存错误失败', error));
      });
    });
  }
  function openWindow() {
    const window = new BrowserWindow({ width: 1440, height: 960, title: 'BoxTeam', webPreferences });
    attachWindow(window, origin);
    void window.loadURL(`${origin}/`).catch(cause => { console.error('加载 Electron 工作台失败', cause); app.exit(1); });
  }
  // TODO：Windows/macOS 原生安装、签名与系统能力需各自平台验收。
  openWindow();
}
