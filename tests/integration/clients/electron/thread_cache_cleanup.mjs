import { TestRunContext } from '../../../harness/js/run-context.mjs';
import { runThreadCacheCleanup } from '../web/quick_access/thread_cache_cleanup.mjs';
import { launchElectron } from './launch.mjs';

await runThreadCacheCleanup({
  run: TestRunContext.fromTestFile('tests/integration/clients/electron/test_thread_cache_cleanup.py'),
  launch: launchElectron,
});
