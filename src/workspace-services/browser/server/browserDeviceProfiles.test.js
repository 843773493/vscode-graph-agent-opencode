import { describe, expect, test } from "bun:test";
import {
  browserDeviceContextOptions,
  browserDeviceEmulationOptions,
  listBrowserDeviceProfiles,
  listBrowserNetworkProfiles,
  resolveBrowserDeviceState,
} from "./browserDeviceProfiles.js";

describe("浏览器设备默认参数", () => {
  test("桌面默认值保留现有视口并关闭移动能力", () => {
    const state = resolveBrowserDeviceState("desktop");
    expect(state).toMatchObject({
      id: "desktop",
      orientation: "portrait",
      viewport: { width: 1280, height: 800 },
      deviceScaleFactor: 1,
      isMobile: false,
      hasTouch: false,
    });
  });

  test("移动设备横向切换交换视口和屏幕尺寸", () => {
    const portrait = resolveBrowserDeviceState("iphone-13", "portrait");
    const landscape = resolveBrowserDeviceState("iphone-13", "landscape");
    expect(landscape).toMatchObject({
      angle: 90,
      viewport: {
        width: portrait.viewport.height,
        height: portrait.viewport.width,
      },
      screen: {
        width: portrait.screen.height,
        height: portrait.screen.width,
      },
    });
  });

  test("Context 参数包含 User-Agent、DPR、触摸和移动标记", () => {
    const options = browserDeviceContextOptions("pixel-7", "portrait");
    expect(options).toMatchObject({
      deviceScaleFactor: 2.625,
      isMobile: true,
      hasTouch: true,
      viewport: { width: 412, height: 839 },
    });
    expect(options.userAgent).toContain("Pixel 7");
  });

  test("CDP 参数携带 Firefox RDM 对应的设备指标", () => {
    const options = browserDeviceEmulationOptions("ipad-pro-11", "landscape");
    expect(options).toMatchObject({
      mobile: true,
      touchEnabled: true,
      screenOrientation: { type: "landscapePrimary", angle: 90 },
    });
    expect(options.width).toBe(1194);
    expect(options.height).toBe(834);
  });

  test("设备目录只暴露可供 attach 页面选择的公开参数", () => {
    const profiles = listBrowserDeviceProfiles();
    expect(profiles.map((profile) => profile.id)).toEqual([
      "desktop",
      "iphone-13",
      "pixel-7",
      "ipad-pro-11",
    ]);
    expect(profiles.find((profile) => profile.id === "pixel-7")).toMatchObject({
      is_mobile: true,
      has_touch: true,
      device_scale_factor: 2.625,
    });
  });

  test("允许在设备默认值上覆盖尺寸、DPR 和触摸能力", () => {
    const state = resolveBrowserDeviceState("iphone-13", "portrait", {
      viewport: { width: 264, height: 478 },
      deviceScaleFactor: 1,
      touchEnabled: false,
      userAgent: "Custom Mobile UA",
    });
    expect(state).toMatchObject({
      viewport: { width: 264, height: 478 },
      deviceScaleFactor: 1,
      hasTouch: false,
      userAgent: "Custom Mobile UA",
      baseViewport: { width: 390, height: 664 },
    });
  });

  test("提供可供工具栏选择的网络配置", () => {
    expect(listBrowserNetworkProfiles().map((profile) => profile.id)).toEqual([
      "none",
      "fast-3g",
      "slow-3g",
      "offline",
    ]);
  });
});
