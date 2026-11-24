import { failureMessage, formatFailureDiagnostic } from '@boxteam/plugin-sdk/failures';
import { failureSummary } from '../../shared/core/failurePresentation.js';
import { copyTextToClipboard } from '../../web/src/utils/clipboard.js';
import bootstrapMessages from '../../shared/core/i18n/bootstrapMessages.json';

interface StartupFailureLabels {
  diagnostic: string;
  copyDiagnostic: string;
  copied: string;
}

export function renderStartupFailure(title: string, detail: unknown, labels: StartupFailureLabels,
  recover?: { label: string; run: () => void }): void {
  const status = document.getElementById('startup-status');
  const error = document.getElementById('startup-error');
  if (!status || !error) throw new Error(title);
  status.textContent = title;
  const diagnostic = detail === undefined ? '' : formatFailureDiagnostic(detail, { maxBytes: 8 * 1024 });
  error.replaceChildren();
  if (diagnostic) {
    const summary = document.createElement('p');
    summary.className = 'startup-failure-summary';
    summary.textContent = failureSummary(failureMessage(detail));
    const details = document.createElement('details');
    const label = document.createElement('summary');
    label.textContent = labels.diagnostic;
    const content = document.createElement('pre');
    content.textContent = diagnostic;
    details.append(label, content);
    const copy = document.createElement('button');
    copy.textContent = labels.copyDiagnostic;
    const feedback = document.createElement('p');
    feedback.setAttribute('role', 'status');
    copy.addEventListener('click', () => {
      void copyTextToClipboard(diagnostic).then(
        () => { feedback.textContent = labels.copied; },
        cause => { feedback.textContent = failureSummary(failureMessage(cause)); },
      );
    });
    error.append(summary, details, copy, feedback);
    console.error(diagnostic);
  }
  if (recover) {
    const button = document.createElement('button');
    button.textContent = recover.label;
    button.addEventListener('click', recover.run, { once: true });
    error.append(button);
  }
}

/** 只由内联入口安装，首个业务脚本下载失败时仍能本地展开、复制和重载。 */
export function installStartupFailureListeners(): void {
  const messages = bootstrapMessages['zh-cn'];
  const show = (detail: unknown) => {
    if (document.querySelector('.monaco-workbench')) return;
    renderStartupFailure(messages['bootstrap.workbenchLoadFailed'], detail, {
      diagnostic: messages['bootstrap.diagnostic'],
      copyDiagnostic: messages['bootstrap.copyDiagnostic'],
      copied: messages['bootstrap.copied'],
    }, { label: messages['bootstrap.retry'], run: () => location.reload() });
  };
  window.addEventListener('error', event => {
    if (event.target instanceof HTMLScriptElement) {
      show(new Error(`${messages['bootstrap.workbenchLoadFailed']}：${event.target.src}`));
    } else if (event.error || event.message) show(event.error ?? event.message);
  }, true);
  window.addEventListener('unhandledrejection', event => show(event.reason));
}
