import { TestRunContext } from '../../../harness/js/run-context.mjs';
import { runDeletedThreadHistory } from '../web/quick_access/deleted_thread_history.mjs';
import { runRecentHistory } from '../web/quick_access/recent_history.mjs';
import { launchElectron } from './launch.mjs';

const run = TestRunContext.fromTestFile('tests/integration/clients/electron/test_recent_history.py');
if (process.argv[2] === 'recovery') {
  await runRecentHistory({ run, async launch(context) {
    const host = await launchElectron(context);
    return { ...host, beforeStorageFault: () => host.reload(), restoreAfterFault: () => host.reload() };
  } });
} else if (process.argv[2] === 'deleted') await runDeletedThreadHistory({ run, launch: launchElectron });
else throw new Error(`未知最近历史原生验收场景：${process.argv[2]}`);
