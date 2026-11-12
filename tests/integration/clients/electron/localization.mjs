import { expect } from 'playwright/test';
import { TestRunContext } from '../../../harness/js/run-context.mjs';
import { runLocalization } from '../web/quick_access/localization_scenario.mjs';
import { launchElectron } from './launch.mjs';

const result = await runLocalization({
  run: TestRunContext.fromTestFile('tests/integration/clients/electron/test_localization.py'),
  async launch(options) {
    let page;
    const host = await launchElectron({ ...options, extraArgs: ['--lang=en-US'],
      usePage(next) { page = next; options.usePage(next); },
    });
    return { ...host, async leave() {
      const owner = options.result.identities[0];
      const uri = `boxteam://${owner.gatewayId}/workspace/${owner.workspaceId}/resources/files/root/locale.txt`;
      // 关闭前沿产品入口把两组的脏文件置为活动项，重开后直接核恢复，不能靠重开补造状态。
      for (const [index, command] of ['workbench.action.focusFirstEditorGroup', 'workbench.action.focusSecondEditorGroup'].entries()) {
        await page.evaluate(async ({ command, uri }) => {
          await globalThis.workbench.commands.executeCommand(command);
          await globalThis.workbench.commands.executeCommand('workbench.action.quickOpen', uri);
        }, { command, uri });
        await expect(page.locator('.quick-input-widget').locator(`[data-quick-input-id="link:${uri}"]`)).toHaveCount(1);
        await page.keyboard.press('Enter');
        await expect(page.locator('.editor-group-container').nth(index).getByRole('tab', { name: /locale\.txt/ })).toHaveClass(/dirty/);
      }
      await host.leave();
    } };
  },
});
expect(result.native.launches).toHaveLength(2);
expect(result.native.exits).toHaveLength(2);
expect(result.nativeRestartRestored).toBe(true);
