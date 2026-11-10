import { join } from 'node:path';

export const testingExtensionSourcePaths = [
  'src/vs/workbench/contrib/testing/common/constants.ts',
  'src/vs/workbench/contrib/testing/browser/testExplorerActions.ts',
  'src/vs/workbench/contrib/testing/browser/testing.contribution.ts',
];

/** 移除仍指向已退役 Gallery 的测试扩展欢迎入口。 */
export function transformTestingExtensions(root, path, contents, replaceOnce) {
  const upstream = join(root, 'upstream');

  if (path === join(upstream, testingExtensionSourcePaths[0])) {
    return replaceOnce(contents, "\tSearchForTestExtension = 'testing.searchForTestExtension',\n", '');
  }

  if (path === join(upstream, testingExtensionSourcePaths[1])) {
    contents = replaceOnce(contents,
      "import { IExtensionsWorkbenchService } from '../../extensions/common/extensions.js';\n", '');
    contents = replaceOnce(contents, `export class SearchForTestExtension extends Action2 {
	constructor() {
		super({
			id: TestCommandId.SearchForTestExtension,
			title: localize2('testing.searchForTestExtension', 'Search for Test Extension'),
		});
	}

	public async run(accessor: ServicesAccessor) {
		accessor.get(IExtensionsWorkbenchService).openSearch('@category:"testing"');
	}
}

`, '');
    return replaceOnce(contents, '\tSearchForTestExtension,\n', '');
  }

  if (path === join(upstream, testingExtensionSourcePaths[2])) {
    return replaceOnce(contents, `viewsRegistry.registerViewWelcomeContent(Testing.ExplorerViewId, {
	content: '[' + localize('searchForAdditionalTestExtensions', "Install Additional Test Extensions...") + \`](command:\${TestCommandId.SearchForTestExtension})\`,
	order: 10
});

`, '');
  }

  return contents;
}
