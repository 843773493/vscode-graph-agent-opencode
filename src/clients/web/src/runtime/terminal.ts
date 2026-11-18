import type { Terminal } from "@xterm/xterm";
import type { Unicode11Addon } from "@xterm/addon-unicode11";

let runtime: Promise<{ Terminal: typeof Terminal; Unicode11Addon: typeof Unicode11Addon; stylesheet: string }> | undefined;

/** 历史回放与交互终端使用 xterm 解析控制序列；只在需要回放时加载。 */
export function loadTerminalRuntime() {
  return runtime ??= Promise.all([
    import("@xterm/xterm"),
    import("@xterm/addon-unicode11"),
    import("../../node_modules/@xterm/xterm/css/xterm.css?text"),
  ]).then(([module, unicode, stylesheet]) => ({ Terminal: module.Terminal, Unicode11Addon: unicode.Unicode11Addon, stylesheet: stylesheet.default }));
}
