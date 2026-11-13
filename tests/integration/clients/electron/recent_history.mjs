import { TestRunContext } from '../../../harness/js/run-context.mjs';
import { runDeletedThreadHistory } from '../web/quick_access/deleted_thread_history.mjs';
import { launchElectron } from './launch.mjs';

await runDeletedThreadHistory({
  run: TestRunContext.fromTestFile('tests/integration/clients/electron/test_recent_history.py'), launch: launchElectron,
});
