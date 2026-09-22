import { HttpRequestError } from "../api/http";

/** 把未知抛出物归一成可展示的错误文本，供各 hook 与组件复用。 */
export function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

/**
 * 后端已按业务语义区分状态码（403 权限、404 不存在、409 冲突），但异常文本里只有
 * 英文 statusText 与原始 detail。这里只依据 HttpRequestError 的状态码补一句可执行
 * 的中文提示；不认识的错误一律返回 null，绝不猜测。
 */
function httpStatusHint(error: unknown): string | null {
  if (!(error instanceof HttpRequestError)) return null;
  switch (error.status) {
    case 403:
      return "没有访问权限，请检查文件系统权限或切换当前登录用户";
    case 404:
      return "目标不存在，可能已被移动或删除，请刷新后重试";
    case 409:
      return "目标已存在或状态冲突，请刷新后重试";
    default:
      return null;
  }
}

/**
 * 展示用错误文本：能按状态码给出可执行提示时前置该提示，并保留原始异常文本用于诊断；
 * 否则退化为 errorMessage 的原样输出。
 */
export function errorDisplayMessage(error: unknown): string {
  const hint = httpStatusHint(error);
  const message = errorMessage(error);
  return hint ? `${hint}（${message}）` : message;
}
