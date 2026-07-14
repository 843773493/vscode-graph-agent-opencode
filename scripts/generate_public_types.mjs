// 该文件由程序生成，请勿手写。
import { spawn } from 'node:child_process';
import { access, mkdir, readFile, readdir, rm, writeFile } from 'node:fs/promises';
import { constants } from 'node:fs';
import path from 'node:path';

const workspaceRoot = path.resolve(process.env.BOXTEAM_PROJECT_ROOT ?? process.cwd());
const sourceDir = path.join(workspaceRoot, 'app', 'schemas', 'public_v2');
const outputDir = path.join(workspaceRoot, 'src', 'web', 'src', 'types', 'gen');
const isWindows = process.platform === 'win32';
const pydantic2tsExecutable = isWindows
	? path.join(workspaceRoot, '.venv', 'Scripts', 'pydantic2ts.exe')
	: path.join(workspaceRoot, '.venv', 'bin', 'pydantic2ts');
const json2tsExecutable = isWindows
	? path.join(workspaceRoot, 'node_modules', '.bin', 'json2ts.cmd')
	: path.join(workspaceRoot, 'node_modules', '.bin', 'json2ts');

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

async function ensurePathExists(filePath, label) {
	await access(filePath, constants.F_OK);
	if (!filePath.startsWith(workspaceRoot)) {
		throw new Error(`${label} 不在项目根目录下: ${filePath}`);
	}
}

async function cleanGeneratedTsFiles() {
	for (const existingFile of await readdir(outputDir)) {
		if (existingFile.endsWith('.ts') && existingFile !== 'index.ts' && existingFile !== 'backend.ts' && existingFile !== 'frontend.ts') {
			await rm(path.join(outputDir, existingFile), { force: true });
		}
	}
}

async function ensureGeneratedHeader(filePath) {
	const header = '// 该文件由程序生成，请勿手写。\n';
	const content = await readFile(filePath, 'utf8');
	if (content.startsWith(header)) {
		return;
	}
	await writeFile(filePath, `${header}${content}`, 'utf8');
}

async function main() {
	await ensurePathExists(path.join(workspaceRoot, 'pyproject.toml'), 'Python 项目文件');
	await ensurePathExists(path.join(workspaceRoot, 'package.json'), '前端项目文件');
	await ensurePathExists(sourceDir, '公开 DTO 源码目录');
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
		await ensureGeneratedHeader(outputFile);
	}

	const indexLines = [
		'// 该文件由程序生成，请勿手写。',
		'//',
		'// pydantic2ts 会在多个模块中重复生成同名类型；这里显式导出，避免 TypeScript 通配导出冲突。',
		'',
		"export type { AgentDTO } from './agent';",
		"export type { ArtifactDTO } from './artifact';",
		"export type { EntityRef, LogSnapshotResultDTO, TimestampedDTO } from './common';",
		"export type { ConfigDTO, ConfigUpdateRequest } from './config';",
		"export type { JobDTO, JobStatus, RunMode, StepDTO, StepStatus } from './job';",
		"export type { LLMRequestLogRecordDTO } from './llm_request_log';",
		"export type { AttachmentRef, MessageDTO, MessageRunAccepted, MessageRunRequest, RunOptions } from './message';",
		"export type { RuntimeInfoDTO, RuntimeShutdownDTO, RuntimeShutdownResultDTO, RuntimeStatusDTO, UiSnapshotResultDTO } from './runtime';",
		"export type { SessionDTO, SessionListResultDTO } from './session';",
		'export type {',
		'  JobProgressDTO,',
		'  MessageDeltaDTO,',
		'  PermissionRequestDTO,',
		'  QuestionInfoDTO,',
		'  QuestionOptionDTO,',
		'  QuestionRequestDTO,',
		'  SessionExecutionEventDTO,',
		'  SessionExecutionSseDTO,',
		"} from './session_interaction';",
		'export type {',
		'  SessionResourceControlRequest,',
		'  SessionResourceControlResultDTO,',
		'  SessionResourceDTO,',
		'  SessionResourceListDTO,',
		"} from './session_resource';",
		"export type { SessionNetworkWaitDTO, SessionObservationStateDTO, SessionStatusDTO } from './session_status';",
		"export type { ToolDTO, ToolInvokeRequest, ToolInvokeResultDTO, ToolSelectionChange, ToolSelectionPatchRequest } from './tool';",
		"export type { ToolTestAttemptDTO, ToolTestProviderResultDTO, ToolTestRunDTO, ToolTestRunListDTO, ToolTestStartRequest } from './tool_test';",
		"export type { TraceEventDTO } from './trace';",
		"export type { WorkspaceContextDTO, WorkspaceDTO, WorkspaceFileContentDTO, WorkspaceFileListDTO, WorkspaceFileNodeDTO } from './workspace';",
	];
	await writeFile(path.join(outputDir, 'index.ts'), `${indexLines.join('\n')}\n`, 'utf8');
}

await main();
