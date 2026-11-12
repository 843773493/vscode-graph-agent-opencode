import { t } from '../../../shared/core/i18n/index.js';
import { errorMessage } from "../utils/errorMessage";

export const BACKGROUND_SETTING = "boxteam.appearance.background";
export const DEFAULT_THEME_BACKGROUND_OVERLAY = "linear-gradient(180deg, rgb(17 19 24 / 0.08) 0%, rgb(17 19 24 / 0.16) 54%, rgb(17 19 24 / 0.44) 100%)";

/** 当前 Workbench profile 的展示配置，不属于 Gateway 业务协议。 */
export interface AppearanceBackground {
  image: string;
  position: string;
  size: string;
  repeat: "no-repeat" | "repeat" | "repeat-x" | "repeat-y" | "space" | "round";
  overlay: string;
}

export const DEFAULT_BACKGROUND: AppearanceBackground = {
  image: "", position: "center", size: "cover", repeat: "no-repeat", overlay: DEFAULT_THEME_BACKGROUND_OVERLAY,
};

export function readAppearanceBackground(value: unknown): AppearanceBackground {
  if (typeof value !== "object" || value === null || Array.isArray(value)) throw new Error(t('appearance.objectRequired', BACKGROUND_SETTING));
  const background = { ...DEFAULT_BACKGROUND, ...value };
  for (const key of Object.keys(background)) {
    if (!Object.hasOwn(DEFAULT_BACKGROUND, key) || typeof background[key as keyof AppearanceBackground] !== "string") {
      throw new Error(t('appearance.propertyInvalid', BACKGROUND_SETTING, key));
    }
  }
  if (background.image) {
    const url = new URL(background.image);
    if (!["http:", "https:"].includes(url.protocol)) throw new Error(t('appearance.httpImageRequired'));
  }
  return background;
}

export function preloadThemeBackground(
  url: string,
  createImage: () => HTMLImageElement = () => new Image(),
): Promise<void> {
  if (!url) return Promise.resolve();
  return new Promise((resolve, reject) => {
    const image = createImage();
    image.onload = () => resolve();
    image.onerror = () => reject(new Error(t('appearance.imageLoadFailed', url)));
    image.src = url;
  });
}

export function applyBackground(background: AppearanceBackground, root: HTMLElement): void {
  root.style.setProperty("--bt-background-image", background.image ? `url(${JSON.stringify(background.image)})` : "none");
  for (const key of ["position", "size", "repeat", "overlay"] as const) {
    if (background.image) root.style.setProperty(`--bt-background-${key}`, background[key]);
    else root.style.removeProperty(`--bt-background-${key}`);
  }
}

/** 只发布当前配置的图片；配色和 color-scheme 始终由 Workbench 决定。 */
export async function loadAndApplyBackground(
  background: AppearanceBackground,
  options: { root: HTMLElement; createImage?: () => HTMLImageElement; signal?: AbortSignal },
): Promise<{ backgroundWarning: string | null }> {
  options.signal?.throwIfAborted();
  try {
    await preloadThemeBackground(background.image, options.createImage);
  } catch (error) {
    options.signal?.throwIfAborted();
    applyBackground({ ...background, image: "" }, options.root);
    return { backgroundWarning: t('appearance.imageWarning', errorMessage(error)) };
  }
  options.signal?.throwIfAborted();
  applyBackground(background, options.root);
  return { backgroundWarning: null };
}
