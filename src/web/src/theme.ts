export const BOXTEAM_THEME_EVENT = "boxteam:theme-change";
export const DEFAULT_THEME_BACKGROUND_OVERLAY = "linear-gradient(180deg, rgb(17 19 24 / 0.08) 0%, rgb(17 19 24 / 0.16) 54%, rgb(17 19 24 / 0.44) 100%)";

export type BoxTeamThemeToken = `--bt-${string}`;

export interface BoxTeamThemeConfig {
  id?: string;
  colorScheme?: "light" | "dark";
  tokens?: Partial<Record<BoxTeamThemeToken, string>>;
  backgroundImage?: string | null;
}

export interface GatewayResolvedThemeConfig {
  id: string;
  color_scheme: "light" | "dark";
  tokens: Record<BoxTeamThemeToken, string>;
  background_image_url?: string | null;
}

declare global {
  interface Window {
    __BOXTEAM_THEME__?: BoxTeamThemeConfig;
  }
}

const appliedTokens = new Set<BoxTeamThemeToken>();

function backgroundImageValue(url: string | null | undefined): string {
  if (!url) return "none";
  return `url(${JSON.stringify(url)})`;
}

export function applyBoxTeamTheme(
  config: BoxTeamThemeConfig,
  root: HTMLElement = document.documentElement,
): void {
  const entries = Object.entries(config.tokens ?? {}).map(([rawToken, rawValue]) => {
    if (!rawToken.startsWith("--bt-")) {
      throw new Error(`主题变量必须使用 --bt- 前缀：${rawToken}`);
    }
    if (typeof rawValue !== "string") {
      throw new TypeError(`主题变量 ${rawToken} 的值必须是字符串`);
    }
    const token = rawToken as BoxTeamThemeToken;
    return [token, rawValue.trim()] as const;
  });

  for (const token of appliedTokens) {
    root.style.removeProperty(token);
  }
  appliedTokens.clear();

  root.dataset.boxteamTheme = config.id?.trim() || "warm";
  root.style.colorScheme = config.colorScheme ?? "light";
  for (const [token, value] of entries) {
    if (!value) continue;
    root.style.setProperty(token, value);
    appliedTokens.add(token);
  }
  root.style.setProperty(
    "--bt-background-image",
    backgroundImageValue(config.backgroundImage),
  );
  const meta = typeof document === "undefined"
    ? null
    : document.querySelector<HTMLMetaElement>('meta[name="theme-color"]');
  const themeColor = config.tokens?.["--bt-page-background"];
  if (meta && themeColor) meta.content = themeColor;
}

export function applyResolvedGatewayTheme(theme: GatewayResolvedThemeConfig): void {
  applyBoxTeamTheme({
    id: theme.id,
    colorScheme: theme.color_scheme,
    tokens: theme.tokens,
    backgroundImage: theme.background_image_url,
  });
}

export function preloadThemeBackground(
  url: string | null | undefined,
  createImage: () => HTMLImageElement = () => new Image(),
): Promise<void> {
  if (!url) return Promise.resolve();
  return new Promise((resolve, reject) => {
    const image = createImage();
    image.onload = () => resolve();
    image.onerror = () => reject(new Error(`背景图片加载失败: ${url}`));
    image.src = url;
  });
}

export async function loadAndApplyResolvedGatewayTheme(
  theme: GatewayResolvedThemeConfig,
): Promise<void> {
  try {
    await preloadThemeBackground(theme.background_image_url);
  } catch (error) {
    applyResolvedGatewayTheme({ ...theme, background_image_url: null });
    throw error;
  }
  applyResolvedGatewayTheme(theme);
}

export function installBoxTeamThemeRuntime(): () => void {
  applyBoxTeamTheme(window.__BOXTEAM_THEME__ ?? { id: "warm" });
  const handleThemeChange = (event: Event) => {
    const config = (event as CustomEvent<BoxTeamThemeConfig>).detail;
    if (!config) {
      throw new Error(`${BOXTEAM_THEME_EVENT} 事件缺少主题配置`);
    }
    applyBoxTeamTheme(config);
  };
  window.addEventListener(BOXTEAM_THEME_EVENT, handleThemeChange);
  return () => window.removeEventListener(BOXTEAM_THEME_EVENT, handleThemeChange);
}
