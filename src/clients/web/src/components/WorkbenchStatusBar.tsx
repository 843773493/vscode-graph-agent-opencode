import React from "react";

/** 主窗口底部状态栏：AppState.status 的唯一可见出口。
 *
 * status 的写入方覆盖「进行中 / 成功 / 失败」三类文案，失败统一写成
 * 「xxx 失败: message」或「初始化失败」。此前状态栏是自闭合空元素，写入方却在注释里
 * 假定「错误状态由 AppProvider 写入」，导致所有失败提示在 UI 上完全不可见。
 * 这里必须完整呈现状态文本，不得再引入 toast 等第二套通知机制。
 */
export default function WorkbenchStatusBar({
  status,
  themeBackgroundWarning,
}: {
  status: string;
  /** 主题背景图加载失败的可见警告。它与 status 是两条独立通道：背景图失败是
   * 非致命降级，不能被后续写入 status 的操作覆盖掉，所以单独渲染。 */
  themeBackgroundWarning: string | null;
}): React.ReactNode {
  return (
    <footer className="workbench-status-bar" aria-label="状态栏">
      {status ? (
        <span
          className="workbench-status-text"
          role="status"
          aria-live="polite"
          title={status}
        >
          {status}
        </span>
      ) : null}
      {themeBackgroundWarning ? (
        <span
          className="workbench-status-warning"
          role="status"
          aria-live="polite"
          title={themeBackgroundWarning}
        >
          {themeBackgroundWarning}
        </span>
      ) : null}
    </footer>
  );
}
