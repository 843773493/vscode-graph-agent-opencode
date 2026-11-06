import { TestRunContext } from '../../../harness/js/run-context.mjs';
import { runFileCanonical } from '../web/quick_access/file_canonical.mjs';
import { launchElectron } from './launch.mjs';

await runFileCanonical({
  run: TestRunContext.fromTestFile('tests/integration/clients/electron/test_file_canonical.py'), launch: launchElectron,
});
