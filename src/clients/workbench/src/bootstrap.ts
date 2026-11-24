import './typography.css';
import { setLocaleResources, t, type DisplayLocale, type MessageCatalog } from '../../shared/core/i18n/index.js';
import bootstrapMessages from '../../shared/core/i18n/bootstrapMessages.json';
import { loadWorkbenchNlsMessages } from './nlsResources.js';
import { renderStartupFailure } from './startupFailure.js';

declare const __BOXTEAM_NLS_MANIFEST__: string;

const localeKey = 'vscode.nls.locale';

function showError(key: string, detail?: unknown, recover?: { labelKey: string; run: () => void }): void {
  renderStartupFailure(t(key), detail, {
    diagnostic: t('bootstrap.diagnostic'), copyDiagnostic: t('bootstrap.copyDiagnostic'), copied: t('bootstrap.copied'),
  }, recover && { label: t(recover.labelKey), run: recover.run });
}

async function start(): Promise<void> {
  document.documentElement.lang = 'zh-Hans';
  let stored: string | null;
  let storageError: unknown;
  try {
    stored = localStorage.getItem(localeKey);
  } catch (error) {
    stored = null;
    storageError = error;
  }

  const requestedLocale = stored === null || stored === '' ? 'zh-cn' : stored.toLowerCase();
  const unsupportedLocale = requestedLocale !== 'zh-cn' && requestedLocale !== 'en';
  const locale: DisplayLocale = requestedLocale === 'en' ? 'en' : 'zh-cn';
  setLocaleResources(locale, bootstrapMessages[locale]);
  if (locale === 'en') document.documentElement.lang = 'en';
  if (storageError !== undefined) {
    showError('bootstrap.readLocaleFailed', storageError);
    return;
  }
  if (unsupportedLocale) {
    showError('bootstrap.unsupportedLocale', `vscode.nls.locale=${stored}`, { labelKey: 'bootstrap.clearLocale', run: () => {
      try { localStorage.removeItem(localeKey); location.reload(); }
      catch (error) { showError('bootstrap.readLocaleFailed', error); }
    } });
    return;
  }

  let messages: MessageCatalog;
  try {
    messages = locale === 'zh-cn'
      ? (await import('../../shared/core/i18n/messages/zh-cn.json')).default
      : (await import('../../shared/core/i18n/messages/en.json')).default;
  } catch (error) {
    showError(locale === 'zh-cn' ? 'bootstrap.loadChineseFailed' : 'bootstrap.loadEnglishFailed', error, {
      labelKey: 'bootstrap.retry', run: () => location.reload(),
    });
    return;
  }
  setLocaleResources(locale, messages);
  document.documentElement.lang = locale === 'zh-cn' ? 'zh-Hans' : 'en';
  const startup = document.getElementById('startup-status');
  if (startup) startup.textContent = t('bootstrap.loading');
  let nlsMessages: Record<string, unknown>;
  try {
    nlsMessages = await loadWorkbenchNlsMessages(locale, fetch, __BOXTEAM_NLS_MANIFEST__);
  } catch (error) {
    showError(locale === 'zh-cn' ? 'bootstrap.loadChineseFailed' : 'bootstrap.loadEnglishFailed',
      error, {
        labelKey: 'bootstrap.clearLocale', run: () => {
          try {
            localStorage.removeItem(localeKey);
            location.reload();
          } catch (cause) {
            showError('bootstrap.readLocaleFailed', cause);
          }
        },
      });
    return;
  }
  Object.assign(globalThis, { _VSCODE_NLS_LANGUAGE: locale, _VSCODE_NLS_MESSAGES: nlsMessages });
  try {
    await import('./main.js');
  } catch (error) {
    showError('bootstrap.workbenchLoadFailed',
      error, {
        labelKey: 'bootstrap.retry', run: () => location.reload(),
      });
  }
}

void start();
