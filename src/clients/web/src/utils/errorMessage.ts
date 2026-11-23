import { failureMessage } from "@boxteam/plugin-sdk/failures";

/** 仅在错误已经交给可见出口后使用；保留原异常供调用方核查。 */
export class ReportedError extends Error {
  constructor(message: string, readonly cause?: unknown) {
    super(message);
    this.name = "ReportedError";
  }
}

/** 复合失败已经整体展示，errors 仍保存每一项原异常。 */
export class ReportedAggregateError extends AggregateError {}

export function isErrorReported(error: unknown): boolean {
  return error instanceof ReportedError
    || error instanceof ReportedAggregateError
    || (error instanceof AggregateError && error.errors.length > 0
      && error.errors.every(isErrorReported));
}

/** 把未知抛出物归一成可展示的错误文本，供各 hook 与组件复用。 */
export function errorMessage(error: unknown): string {
  return failureMessage(error);
}
