// 该文件由程序生成，请勿手写。
import { spawn } from 'node:child_process';
import { mkdir, readdir, rm, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const workspaceRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const sourceDir = path.join(workspaceRoot, 'app', 'schemas', 'public_v2');
const outputDir = path.join(workspaceRoot, 'src', 'web', 'src', 'types', 'gen');
const pydantic2tsExecutable = path.join(workspaceRoot, '.venv', 'Scripts', 'pydantic2ts.exe');
const json2tsExecutable = path.join(workspaceRoot, 'node_modules', '.bin', 'json2ts.cmd');

function runCommand(command, args, options = {}) {
	return new Promise((resolve, reject) => {
		const child = spawn(command, args, {
			cwd: workspaceRoot,
			env: {
				...process.env,
				PYTHONPATH: workspaceRoot,
			},
			stdio: 'inherit',
			...options,
		});

		child.on('error', reject);
		child.on('exit', (code) => {
			if (code === 0) {
				resolve();
				return;
			}

			reject(new Error(`命令执行失败: ${command} ${args.join(' ')}，退出码: ${code}`));
		});
	});
}

async function getPublicPythonFiles() {
	return (await readdir(sourceDir))
		.filter((fileName) => fileName.endsWith('.py') && fileName !== '__init__.py')
		.sort();
}

async function ensureDirectory(directoryPath) {
	await mkdir(directoryPath, { recursive: true });
}

async function cleanGeneratedTsFiles() {
	for (const existingFile of await readdir(outputDir)) {
		if (existingFile.endsWith('.ts') && existingFile !== 'index.ts' && existingFile !== 'backend.ts' && existingFile !== 'frontend.ts') {
			await rm(path.join(outputDir, existingFile), { force: true });
		}
	}
}

async function main() {
	await ensureDirectory(outputDir);

	const publicFiles = await getPublicPythonFiles();
	if (publicFiles.length === 0) {
		throw new Error(`未找到任何 Python 文件: ${sourceDir}`);
	}

	await cleanGeneratedTsFiles();

	for (const fileName of publicFiles) {
		const moduleName = path.basename(fileName, '.py');
		const inputModule = `app.schemas.public_v2.${moduleName}`;
		const outputFile = path.join(outputDir, `${moduleName}.ts`);

		await runCommand(pydantic2tsExecutable, [
			'--module',
			inputModule,
			'--output',
			outputFile,
			'--json2ts-cmd',
			json2tsExecutable,
		]);
	}

	const indexLines = ['// 该文件由程序生成，请勿手写。', ''];
	for (const fileName of publicFiles) {
		indexLines.push(`export type * from './${path.basename(fileName, '.py')}';`);
	}
	await writeFile(path.join(outputDir, 'index.ts'), `${indexLines.join('\n')}\n`, 'utf8');
}

await main();
