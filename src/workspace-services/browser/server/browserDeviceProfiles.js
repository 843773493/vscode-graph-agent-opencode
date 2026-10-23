import { devices } from "playwright";

export const DEFAULT_BROWSER_DEVICE_PROFILE = "desktop";
export const DEFAULT_BROWSER_DEVICE_ORIENTATION = "portrait";
export const DEFAULT_BROWSER_NETWORK_PROFILE = "none";

const ORIENTATIONS = new Set(["portrait", "landscape"]);
const DESKTOP_CHROME = devices["Desktop Chrome"];

/**
 * 网络仿真预设；参数原值交给CDP Network.emulateNetworkConditions。
 * @typedef {object} BrowserNetworkThrottleProfile
 * @property {string} id 本地预设名称，不是连接或网络请求身份。
 * @property {string} label attach页面中的展示名称。
 * @property {boolean} offline 是否模拟离线，与吞吐限速分别设置。
 * @property {number} latency 请求发出到收到响应头的最小延迟，单位毫秒；不是整次下载耗时。
 * @property {number} downloadThroughput 最大聚合下载吞吐，单位字节/秒；-1表示关闭下载限速。
 * @property {number} uploadThroughput 最大聚合上传吞吐，单位字节/秒；-1表示关闭上传限速。
 */
/** @type {Map<string, BrowserNetworkThrottleProfile>} */
const NETWORK_THROTTLE_PROFILES = new Map([
  ["none", {
    id: "none",
    label: "无限制",
    offline: false,
    latency: 0,
    downloadThroughput: -1,
    uploadThroughput: -1,
  }],
  ["fast-3g", {
    id: "fast-3g",
    label: "快速 3G",
    offline: false,
    latency: 150,
    downloadThroughput: 1_600_000,
    uploadThroughput: 750_000,
  }],
  ["slow-3g", {
    id: "slow-3g",
    label: "慢速 3G",
    offline: false,
    latency: 400,
    downloadThroughput: 50_000,
    uploadThroughput: 50_000,
  }],
  ["offline", {
    id: "offline",
    label: "离线",
    offline: true,
    latency: 0,
    downloadThroughput: -1,
    uploadThroughput: -1,
  }],
]);

const DEVICE_PROFILE_DEFINITIONS = new Map([
  ["desktop", {
    id: "desktop",
    label: "桌面",
    descriptor: {
      viewport: { width: 1280, height: 800 },
      screen: { width: 1280, height: 800 },
      deviceScaleFactor: 1,
      isMobile: false,
      hasTouch: false,
      userAgent: null,
      platform: null,
    },
  }],
  ["iphone-13", {
    id: "iphone-13",
    label: "iPhone 13",
    descriptor: {
      ...devices["iPhone 13"],
      platform: "iPhone",
    },
  }],
  ["pixel-7", {
    id: "pixel-7",
    label: "Pixel 7",
    descriptor: {
      ...devices["Pixel 7"],
      platform: "Linux armv8l",
    },
  }],
  ["ipad-pro-11", {
    id: "ipad-pro-11",
    label: "iPad Pro 11",
    descriptor: {
      ...devices["iPad Pro 11"],
      platform: "iPad",
    },
  }],
]);

function assertOrientation(orientation) {
  if (!ORIENTATIONS.has(orientation)) {
    throw new Error(`未知浏览器设备方向: ${orientation}`);
  }
}

function rotateSize(size, orientation) {
  if (orientation !== "landscape") {
    return { ...size };
  }
  return { width: size.height, height: size.width };
}

function resolvedOrientation(profile, requestedOrientation) {
  if (!profile.descriptor.isMobile) {
    return DEFAULT_BROWSER_DEVICE_ORIENTATION;
  }
  const orientation = requestedOrientation || DEFAULT_BROWSER_DEVICE_ORIENTATION;
  assertOrientation(orientation);
  return orientation;
}

export function getBrowserDeviceProfile(profileId = DEFAULT_BROWSER_DEVICE_PROFILE) {
  const profile = DEVICE_PROFILE_DEFINITIONS.get(profileId || DEFAULT_BROWSER_DEVICE_PROFILE);
  if (!profile) {
    throw new Error(`未知浏览器设备配置: ${profileId}`);
  }
  return profile;
}

export function resolveBrowserDeviceState(
  profileId = DEFAULT_BROWSER_DEVICE_PROFILE,
  requestedOrientation = DEFAULT_BROWSER_DEVICE_ORIENTATION,
  overrides = {},
) {
  const profile = getBrowserDeviceProfile(profileId);
  const orientation = resolvedOrientation(profile, requestedOrientation);
  const descriptor = profile.descriptor;
  // base值已应用方向、尚未应用自定义覆盖；尺寸单位为CSS像素，不是截图物理像素。
  const baseViewport = rotateSize(descriptor.viewport, orientation);
  const baseScreen = rotateSize(descriptor.screen || descriptor.viewport, orientation);
  const hasViewportOverride = overrides.viewport !== undefined && overrides.viewport !== null;
  const deviceScaleFactor = overrides.deviceScaleFactor ?? descriptor.deviceScaleFactor ?? 1;
  const hasTouch = overrides.touchEnabled ?? descriptor.hasTouch === true;
  const userAgent = overrides.userAgent === undefined
    ? descriptor.userAgent || null
    : overrides.userAgent || null;
  const platform = overrides.platform === undefined
    ? descriptor.platform || null
    : overrides.platform || null;
  return {
    id: profile.id,
    label: profile.label,
    orientation,
    angle: orientation === "landscape" ? 90 : 0,
    viewport: hasViewportOverride ? { ...overrides.viewport } : baseViewport,
    baseViewport,
    screen: hasViewportOverride ? { ...overrides.viewport } : baseScreen,
    baseScreen,
    deviceScaleFactor,
    baseDeviceScaleFactor: descriptor.deviceScaleFactor || 1,
    isMobile: descriptor.isMobile === true,
    hasTouch,
    baseHasTouch: descriptor.hasTouch === true,
    userAgent,
    baseUserAgent: descriptor.userAgent || null,
    platform,
    basePlatform: descriptor.platform || null,
  };
}

export function browserDeviceContextOptions(
  profileId = DEFAULT_BROWSER_DEVICE_PROFILE,
  orientation = DEFAULT_BROWSER_DEVICE_ORIENTATION,
  viewport = null,
  overrides = {},
) {
  const state = resolveBrowserDeviceState(profileId, orientation, {
    ...overrides,
    ...(viewport ? { viewport } : {}),
  });
  return {
    viewport: viewport ? { ...viewport } : { ...state.viewport },
    screen: { ...state.screen },
    deviceScaleFactor: state.deviceScaleFactor,
    isMobile: state.isMobile,
    hasTouch: state.hasTouch,
    ...(state.userAgent ? { userAgent: state.userAgent } : {}),
  };
}

export function browserDeviceEmulationOptions(
  profileId = DEFAULT_BROWSER_DEVICE_PROFILE,
  orientation = DEFAULT_BROWSER_DEVICE_ORIENTATION,
  {
    fallbackUserAgent = null,
    fallbackPlatform = null,
    viewport = null,
    deviceScaleFactor = null,
    touchEnabled = null,
    userAgent = undefined,
    platform = undefined,
  } = {},
) {
  const state = resolveBrowserDeviceState(profileId, orientation, {
    ...(viewport ? { viewport } : {}),
    ...(deviceScaleFactor !== null ? { deviceScaleFactor } : {}),
    ...(touchEnabled !== null ? { touchEnabled } : {}),
    ...(userAgent !== undefined ? { userAgent } : {}),
    ...(platform !== undefined ? { platform } : {}),
  });
  return {
    width: state.viewport.width,
    height: state.viewport.height,
    deviceScaleFactor: state.deviceScaleFactor,
    mobile: state.isMobile,
    screenWidth: state.screen.width,
    screenHeight: state.screen.height,
    screenOrientation: {
      type: state.orientation === "landscape" ? "landscapePrimary" : "portraitPrimary",
      angle: state.angle,
    },
    touchEnabled: state.hasTouch,
    maxTouchPoints: state.hasTouch ? 5 : 0,
    userAgent: state.userAgent || fallbackUserAgent || DESKTOP_CHROME.userAgent,
    platform: state.platform || fallbackPlatform || "Win32",
  };
}

export function listBrowserDeviceProfiles() {
  return [...DEVICE_PROFILE_DEFINITIONS.values()].map((profile) => {
    const portrait = resolveBrowserDeviceState(profile.id, "portrait");
    return {
      id: portrait.id,
      label: portrait.label,
      is_mobile: portrait.isMobile,
      has_touch: portrait.hasTouch,
      device_scale_factor: portrait.deviceScaleFactor,
      portrait: {
        width: portrait.viewport.width,
        height: portrait.viewport.height,
      },
      landscape: profile.descriptor.isMobile
        ? resolveBrowserDeviceState(profile.id, "landscape").viewport
        : null,
    };
  });
}

/** @returns {BrowserNetworkThrottleProfile} */
export function getBrowserNetworkProfile(networkProfileId = DEFAULT_BROWSER_NETWORK_PROFILE) {
  const networkProfile = NETWORK_THROTTLE_PROFILES.get(networkProfileId || DEFAULT_BROWSER_NETWORK_PROFILE);
  if (!networkProfile) {
    throw new Error(`未知网络限制配置: ${networkProfileId}`);
  }
  return { ...networkProfile };
}

/** @returns {BrowserNetworkThrottleProfile[]} */
export function listBrowserNetworkProfiles() {
  return [...NETWORK_THROTTLE_PROFILES.values()].map((networkProfile) => ({ ...networkProfile }));
}
