// 该文件由程序生成，请勿手写。
import { spawn } from 'node:child_process';
import { access, mkdir, readFile, readdir, rm, writeFile } from 'node:fs/promises';
import { constants } from 'node:fs';
import { createRequire } from 'node:module';
import path from 'node:path';

const workspaceRoot = path.resolve(process.env.BOXTEAM_PROJECT_ROOT ?? process.cwd());
const sourceDir = path.join(workspaceRoot, 'app', 'schemas', 'public_v2');
const outputDir = path.join(workspaceRoot, 'src', 'web', 'src', 'types', 'gen');
const isWindows = process.platform === 'win32';
const pydantic2tsExecutable = isWindows
	? path.join(workspaceRoot, '.venv', 'Scripts', 'pydantic2ts.exe')
	: path.join(workspaceRoot, '.venv', 'bin', 'pydantic2ts');
const pythonExecutable = isWindows
	? path.join(workspaceRoot, '.venv', 'Scripts', 'python.exe')
	: path.join(workspaceRoot, '.venv', 'bin', 'python');
const json2tsExecutable = isWindows
	? path.join(workspaceRoot, 'node_modules', '.bin', 'json2ts.cmd')
	: path.join(workspaceRoot, 'node_modules', '.bin', 'json2ts');

async function generateSseRuntimeValidators() {
	const webRequire = createRequire(path.join(workspaceRoot, 'src', 'web', 'package.json'));
	const Ajv = webRequire('ajv').default;
	const standaloneCode = webRequire('ajv/dist/standalone').default;
	const schemaPath = path.join(outputDir, 'sse_runtime_schemas.json');
	const payload = JSON.parse(await readFile(schemaPath, 'utf8'));
	if (!payload || typeof payload !== 'object' || !payload.schemas) {
		throw new Error(`SSE runtime schema 文件结构无效: ${schemaPath}`);
	}
	const ajv = new Ajv({
		allErrors: true,
		strict: false,
		validateFormats: false,
		code: { esm: true, source: true },
	});
	const exportsByName = {};
	for (const [name, schema] of Object.entries(payload.schemas)) {
		const schemaId = schema?.$id;
		if (typeof schemaId !== 'string' || !schemaId) {
			throw new Error(`SSE runtime schema 缺少 $id: ${name}`);
		}
		ajv.addSchema(schema, schemaId);
		exportsByName[`validate${name}`] = schemaId;
	}
	const outputPath = path.join(workspaceRoot, 'src', 'shared', 'sseRuntimeValidators.js');
	const declarationPath = path.join(workspaceRoot, 'src', 'shared', 'sseRuntimeValidators.d.ts');
	const validatorSource = standaloneCode(ajv, exportsByName).replace(
		/const (\w+) = require\("([^"]+)"\)\.default;/g,
		'import $1 from "$2";',
	).replace(
		/import (\w+) from "ajv\/dist\/runtime\/ucs2length";/g,
		`const $1 = (value) => {
	let length = 0;
	let position = 0;
	while (position < value.length) {
		length += 1;
		const first = value.charCodeAt(position++);
		if (first >= 0xd800 && first <= 0xdbff && position < value.length) {
			const second = value.charCodeAt(position);
			if (second >= 0xdc00 && second <= 0xdfff) position += 1;
		}
	}
	return length;
};`,
	);
	if (validatorSource.includes('require(') || validatorSource.includes('from "ajv/')) {
		throw new Error('生成的 SSE runtime validator 仍依赖 Ajv runtime');
	}
	await writeFile(
		outputPath,
		`// 该文件由程序生成，请勿手写。\n${validatorSource}\n`,
		'utf8',
	);
	await writeFile(
		declarationPath,
		`// 该文件由程序生成，请勿手写。\n${Object.keys(exportsByName)
			.map((name) => `export const ${name}: { (value: unknown): boolean; errors?: Array<{ instancePath: string; keyword: string; message?: string }> | null };`)
			.join('\n')}\n`,
		'utf8',
	);
}

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

async function appendGeneratedTypeAliases(moduleName, filePath) {
	if (moduleName !== 'session_interaction') {
		return;
	}
	const content = await readFile(filePath, 'utf8');
	const alias = '\nexport type SessionExecutionEventDTO = SessionExecutionSseDTO["event"];\n';
	if (!content.includes(alias.trim())) {
		await writeFile(filePath, `${content.trimEnd()}${alias}`, 'utf8');
	}
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
		await appendGeneratedTypeAliases(moduleName, outputFile);
	}

	await runCommand(pythonExecutable, [
		'-m',
		'scripts.export_sse_runtime_schemas',
	]);
	await generateSseRuntimeValidators();

	const indexLines = [
		'// 该文件由程序生成，请勿手写。',
		'//',
		'// pydantic2ts 会在多个模块中重复生成同名类型；这里显式导出，避免 TypeScript 通配导出冲突。',
		'',
		"export type { AgentDTO } from './agent';",
		"export type { ArtifactDTO } from './artifact';",
		"export type { EntityRef, LogSnapshotResultDTO, TimestampedDTO } from './common';",
		"export type { ConfigDTO, ConfigReloadStatusDTO, ConfigUpdateRequest } from './config';",
		"export type { JobDispatchSnapshotDTO, JobDTO, JobStatus, RunMode, StepDTO, StepStatus } from './job';",
		"export type { LLMRequestLogRecordDTO } from './llm_request_log';",
		"export type { AttachmentRef } from './attachment';",
		"export type { MessageDTO, MessageRunAccepted, MessageRunRequest, RunOptions } from './message';",
		"export type { PendingRequestDTO, PendingRequestListDTO, PendingRequestOrderItem, PendingRequestReorderRequest, PendingRequestUpdateRequest } from './pending_request';",
		"export type { RuntimeInfoDTO, RuntimeShutdownDTO, RuntimeShutdownResultDTO, RuntimeStatusDTO, UiSnapshotResultDTO } from './runtime';",
		"export type { SessionInformationSnapshotDTO, SessionDTO, SessionListResultDTO } from './session';",
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
		"export type { TeamBoardDTO, TeamEventDTO, TeamListDTO, TeamMemberDTO, TeamMemberOperationDTO, TeamTaskDTO, TeamTaskOperationDTO } from './team';",
		"export type { ToolDTO, ToolSelectionChange, ToolSelectionPatchRequest } from './tool';",
		"export type { ToolTestAttemptDTO, ToolTestProviderResultDTO, ToolTestRunDTO, ToolTestRunListDTO, ToolTestStartRequest } from './tool_test';",
		"export type { SseErrorDTO } from './sse';",
		"export type { TraceEventDTO } from './trace';",
		"export type { SessionTurnBootstrapDTO, StaleTurnCursorErrorDTO, TurnAttachmentDTO, TurnCursorDTO, TurnDetailBatchDTO, TurnDetailBatchRequest, TurnDetailDTO, TurnJobSummaryDTO, TurnPageDTO, TurnProjectionCorruptedErrorDTO, TurnSummaryDTO, TurnUserMessageDTO, TurnUserMessageSummaryDTO } from './turn';",
		"export type { WorkspaceContextDTO, WorkspaceDTO, WorkspaceFileChangeBatchDTO, WorkspaceFileChangeDTO, WorkspaceFileContentDTO, WorkspaceFileListDTO, WorkspaceFileNodeDTO, WorkspaceFileUpdateRequest, WorkspaceFileWatchRequest } from './workspace';",
	];
	await writeFile(path.join(outputDir, 'index.ts'), `${indexLines.join('\n')}\n`, 'utf8');
}

await main();
