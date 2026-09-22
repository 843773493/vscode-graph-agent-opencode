/** 把未知抛出物归一成可展示的错误文本，供各 hook 与组件复用。 */
export function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
