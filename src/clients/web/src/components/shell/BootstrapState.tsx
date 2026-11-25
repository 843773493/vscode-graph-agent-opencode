import { failureSummary } from "../../../../shared/core/failurePresentation";
import { t, formatNumber } from '../../../../shared/core/i18n/index.js';
import { useEffect, useState } from "react";
import { errorMessage } from "../../utils/errorMessage";

interface BootstrapStateProps {
  onRetry: () => Promise<void>;
}

function bootstrappingHint(elapsedSeconds: number): string {
  if (elapsedSeconds >= 25) {
    return t('shell.bootstrap.slow');
  }
  if (elapsedSeconds >= 10) {
    return t('shell.bootstrap.waiting');
  }
  return t('shell.bootstrap.connecting');
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
      const message = errorMessage(error);
      setRetryError(t('shell.bootstrap.retryFailed', message));
    } finally {
      setRetrying(false);
    }
  };

  return (
    <div className="empty-state bootstrap-state" aria-live="polite">
      <div className="empty-state-title">{t('shell.bootstrap.title')}</div>
      <div>{bootstrappingHint(elapsedSeconds)}</div>
      {elapsedSeconds >= 10 ? (
        <div className="bootstrap-elapsed">{t('shell.bootstrap.elapsed', formatNumber(elapsedSeconds))}</div>
      ) : null}
      {elapsedSeconds >= 10 ? (
        <div className="bootstrap-actions">
          <button type="button" onClick={() => void handleRetry()} disabled={retrying}>
            {retrying ? t('shell.bootstrap.retrying') : t('shell.bootstrap.retry')}
          </button>
          {retryError ? <div className="bootstrap-retry-error failure-summary" role="alert">{failureSummary(retryError)}</div> : null}
        </div>
      ) : null}
    </div>
  );
}
