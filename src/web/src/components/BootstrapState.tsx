import { useEffect, useState } from "react";

interface BootstrapStateProps {
  onRetry: () => Promise<void>;
}

function bootstrappingHint(elapsedSeconds: number): string {
  if (elapsedSeconds >= 25) {
    return "本地后端响应仍未完成，可能正在冷启动或请求排队。";
  }
  if (elapsedSeconds >= 10) {
    return "仍在等待本地后端返回工作区和会话数据。";
  }
  return "正在连接本地后端并加载会话数据。";
}

export default function BootstrapState({ onRetry }: BootstrapStateProps): React.ReactNode {
  const [elapsedSeconds, setElapsedSeconds] = useState(0);
  const [retrying, setRetrying] = useState(false);
  const [retryError, setRetryError] = useState<string | null>(null);

  useEffect(() => {
    const startedAt = Date.now();
    const timerId = window.setInterval(() => {
      setElapsedSeconds(Math.floor((Date.now() - startedAt) / 1000));
    }, 1000);
    return () => window.clearInterval(timerId);
  }, []);

  const handleRetry = async () => {
    setRetrying(true);
    setRetryError(null);
    try {
      await onRetry();
    } catch (error: unknown) {
      const message = error instanceof Error ? error.message : String(error);
      setRetryError(`重试失败：${message}`);
    } finally {
      setRetrying(false);
    }
  };

  return (
    <div className="empty-state bootstrap-state" aria-live="polite">
      <div className="empty-state-title">正在加载工作区与会话...</div>
      <div>{bootstrappingHint(elapsedSeconds)}</div>
      {elapsedSeconds >= 10 ? (
        <div className="bootstrap-elapsed">已等待 {elapsedSeconds} 秒</div>
      ) : null}
      {elapsedSeconds >= 10 ? (
        <div className="bootstrap-actions">
          <button type="button" onClick={() => void handleRetry()} disabled={retrying}>
            {retrying ? "正在重试..." : "重试加载"}
          </button>
          {retryError ? <div className="bootstrap-retry-error" role="alert">{retryError}</div> : null}
        </div>
      ) : null}
    </div>
  );
}
