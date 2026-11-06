import { expect } from 'playwright/test';
import { TestRunContext } from '../../../harness/js/run-context.mjs';
import { runWebviewResourceCache } from '../web/quick_access/webview_resource_cache.mjs';
import { observeImageCache } from './image_cache.mjs';
import { launchElectron } from './launch.mjs';

await runWebviewResourceCache({
  run: TestRunContext.fromTestFile('tests/integration/clients/electron/test_webview_resource_cache.py'),
  async launch(options) {
    let page;
    const host = await launchElectron({ ...options,
      usePage(nextPage) { page = nextPage; options.usePage(nextPage); },
    });
    const owner = JSON.parse(process.env.BOXTEAM_WEBVIEW_FIXTURE);
    const reads = options.result.imageReads = [];
    await expect(page.locator('.native-session-navigation')).toBeVisible({ timeout: 45000 });
    let image = await observeImageCache(page, reads);
    const cold = await image(owner, 'cold-image', 200);
    const warm = await image(owner, 'warm-image', 304);
    expect(warm.response.etag).toBe(cold.response.etag);
    expect(warm.response['x-request-id']).not.toBe(cold.response['x-request-id']);
    return { ...host,
      async reload() {
        await host.reload();
        await expect(page.locator('.native-session-navigation')).toBeVisible({ timeout: 45000 });
        image = await observeImageCache(page, reads);
        const restarted = await image(owner, 'restart-image', 304);
        expect(restarted.response.etag).toBe(cold.response.etag);
        expect(restarted.response['x-request-id']).not.toBe(warm.response['x-request-id']);
      },
    };
  },
});
