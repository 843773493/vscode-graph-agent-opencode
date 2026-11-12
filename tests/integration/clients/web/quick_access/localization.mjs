import { chromium } from 'playwright';
import { TestRunContext } from '../../../../harness/js/run-context.mjs';
import { runLocalization } from './localization_scenario.mjs';

await runLocalization({
  run: TestRunContext.fromTestFile('tests/integration/clients/web/quick_access/test_localization.py'),
  async launch({ run, usePage }) {
    const executablePath = process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH;
    const baseUrl = process.env.BOXTEAM_BROWSER_BASE_URL;
    if (!executablePath || !baseUrl) throw new Error('显示语言验收缺少 Chromium 或 Gateway 地址');
    const browser = await chromium.launch({ executablePath, headless: true,
      args: ['--no-sandbox', '--disable-dev-shm-usage'] });
    run.addCleanup('显示语言 Chromium', () => browser.close());
    const context = await browser.newContext({ locale: 'en-US', viewport: { width: 1700, height: 1000 } });
    usePage(await context.newPage());
    return { baseUrl };
  },
});
