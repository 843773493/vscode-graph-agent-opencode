/** Node Debug hook 单测专用浏览器全局 fixture。 */
import {
  installWindowWithTimers,
  restoreGlobalDescriptor,
} from "../../tests/testGlobals";

export function installNodeDebugBrowserGlobals(): () => void {
  const originalBroadcastChannel = Object.getOwnPropertyDescriptor(
    globalThis,
    "BroadcastChannel",
  );
  const restoreWindow = installWindowWithTimers();
  Object.defineProperty(globalThis, "BroadcastChannel", {
    configurable: true,
    value: undefined,
  });

  return () => {
    restoreWindow();
    restoreGlobalDescriptor("BroadcastChannel", originalBroadcastChannel);
  };
}
