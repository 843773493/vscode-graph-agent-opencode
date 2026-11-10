import { join } from 'node:path';
import ts from 'typescript';

export const onboardingKeymapSourcePaths = [
  'src/vs/workbench/contrib/welcomeOnboarding/browser/onboardingVariationA.ts',
];

function replaceMembers(path, source, ownerName, replacements) {
  const parsed = ts.createSourceFile(path, source, ts.ScriptTarget.Latest, true);
  const owners = parsed.statements.filter(node => ts.isClassDeclaration(node) && node.name?.text === ownerName);
  if (owners.length !== 1) throw new Error(`新手引导 owner 不唯一：${ownerName}`);

  const edits = [];
  for (const [name, replacement] of replacements) {
    const members = owners[0].members.filter(member => member.name?.getText(parsed) === name);
    if (members.length !== 1) throw new Error(`新手引导成员不唯一：${ownerName}.${name}`);
    edits.push({ start: members[0].getFullStart(), end: members[0].end, replacement });
  }

  for (const edit of edits.sort((a, b) => b.start - a.start)) {
    source = source.slice(0, edit.start) + edit.replacement + source.slice(edit.end);
  }
  return source;
}

/** 移除新手引导中依赖 VSIX/Gallery 的外部键位包安装流程，保留主题选择。 */
export function transformOnboardingKeymaps(root, path, source, replaceOnce) {
  if (path !== join(root, 'upstream', onboardingKeymapSourcePaths[0])) return source;

  let contents = source;
  contents = replaceOnce(contents,
    "import { isWindows, isMacintosh, isLinux } from '../../../../base/common/platform.js';\n",
    "import { isMacintosh } from '../../../../base/common/platform.js';\n");
  for (const line of [
    "import { EXTENSION_INSTALL_SKIP_WALKTHROUGH_CONTEXT, IExtensionGalleryService, IExtensionManagementService } from '../../../../platform/extensionManagement/common/extensionManagement.js';\n",
    "import { CancellationToken } from '../../../../base/common/cancellation.js';\n",
    "import { URI } from '../../../../base/common/uri.js';\n",
    "import { IFileService } from '../../../../platform/files/common/files.js';\n",
    "import { IPathService } from '../../../services/path/common/pathService.js';\n",
  ]) contents = replaceOnce(contents, line, '');

  for (const line of [
    '\t\t@IExtensionGalleryService private readonly extensionGalleryService: IExtensionGalleryService,\n',
    '\t\t@IExtensionManagementService private readonly extensionManagementService: IExtensionManagementService,\n',
    '\t\t@IFileService private readonly fileService: IFileService,\n',
    '\t\t@IPathService private readonly pathService: IPathService,\n',
    '\t\tthis._detectInstalledEditors().then(ids => { this._detectedEditorIds = ids; });\n',
    '\t\t\t\tthis._applyStepSelections(this.steps[this.currentStepIndex]);\n',
    '\t\t\tthis._applyStepSelections(leavingStep);\n',
  ]) contents = replaceOnce(contents, line, '');

  contents = replaceMembers(path, contents, 'OnboardingVariationA', new Map([
    ['_applyStepSelections', ''],
    ['_applyKeymap', ''],
    ['_hasOtherEditors', ''],
    ['_detectInstalledEditors', ''],
    ['selectedKeymapId', ''],
    ['_detectedEditorIds', ''],
    ['_renderPersonalizeStep', `private _renderPersonalizeStep(container: HTMLElement): void {
		const wrapper = append(container, $('.onboarding-a-personalize'));

		const themeLabel = append(wrapper, $('div.onboarding-a-section-label'));
		themeLabel.textContent = localize('onboarding.personalize.theme', "Color Theme");

		const themeHint = append(wrapper, $('div.onboarding-a-theme-hint'));
		themeHint.textContent = localize('onboarding.personalize.themeHint', "You can change your color theme later in Appearance settings.");

		const themeGrid = append(wrapper, $('.onboarding-a-theme-grid'));
		themeGrid.setAttribute('role', 'radiogroup');
		themeGrid.setAttribute('aria-label', localize('onboarding.personalize.themeLabel', "Choose a color theme"));
		themeGrid.classList.add('theme-grid-expanded');

		const themeCards: HTMLElement[] = [];
		for (const theme of product.onboardingThemes ?? []) {
			this._createThemeCard(themeGrid, theme, themeCards);
		}
		for (const card of themeCards) {
			card.setAttribute('tabindex', '0');
		}
	}`],
  ]));

  contents = replaceOnce(contents, 'Personalize — Theme selection grid + keymap pills', 'Personalize — Theme selection grid');
  return contents;
}
