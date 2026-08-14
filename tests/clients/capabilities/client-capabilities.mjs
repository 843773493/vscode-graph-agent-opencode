export const clientCapabilities = Object.freeze({
  web: Object.freeze({
    implemented: true,
    runtime: "browser",
    supportsRealE2E: true,
  }),
  electron: Object.freeze({
    implemented: false,
    runtime: "electron",
    todo: "需要独立 OpenSpec 实现真实 main/preload/renderer 驱动",
  }),
  vscode: Object.freeze({
    implemented: false,
    runtime: "vscode-extension-host",
    todo: "需要独立 OpenSpec 迁移并实现 Extension Host 驱动",
  }),
  mobile: Object.freeze({
    implemented: false,
    runtime: "react-native",
    todo: "需要独立 OpenSpec 选择原生设备驱动；RN Web 只算 Integration",
  }),
});
