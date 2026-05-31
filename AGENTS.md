# General Instructions

## Development Standards

### Communication and Delivery

1. Communicate in Chinese; code comments should also be in Chinese, except for professional terms.
2. Do not write summary documents unless the user explicitly requests them.

### Implementation Approach

1. When implementing a feature for the first time, reduce `try/except` usage and focus on core functionality.
2. Whenever you are not confident about a piece of code, add a TODO comment in the code.
3. Whenever you or the user asks you to skip an important implementation detail, add a TODO comment in the code.
4. Whenever you use compatibility-oriented code, add a TODO comment above it.
5. Avoid using `any` where possible; keep it only when necessary for generics or other complex cases.
6. Prefer third-party libraries when appropriate; do not reinvent the wheel.
7. Classes: do not use prototype mixins or mutation. Prefer inheritance or composition.
8. When the user asks you to refactor or modify existing functionality, clean up the original code and implement the feature directly; do not keep compatibility layers.

### Dependencies and Configuration

1. Do not hardcode environment variable values in code.
2. Use `bun install` for JS/TS dependencies and `uv sync` for Python dependencies.
3. If a `.venv` directory exists in the repository root, use `.venv\Scripts\python.exe` and its `pytest` instead of the global Python interpreter.
4. Put lazily loaded packages in the `runtime` module separately.

### Execution and Quality

1. Every time you write a code file, run static analysis.
2. Every time you modify the webview UI (`src/webview-ui/`), rebuild it with `cd src/webview-ui && ..\\..\\tools\\bun.exe run build` and verify the build succeeds before finishing.

### Code Organization

1. If a command in `package.json` becomes too long, move it into an `.mjs` script under `scripts/`.
2. JavaScript code in the repository must consistently use ESM (ES Modules) with `import`/`export`, avoiding CommonJS.
3. The frontend directory is responsible for pages, interactions, state, API calls, and a small amount of presentation logic.
4. The backend directory is responsible for business rules, permissions, databases, order flow, risk control, and core computation.

### Commit and Directory Conventions

1. Git commits should follow a conventional style, be concise, and be grouped logically.
2. Every created subdirectory must include an `AGENTS.md` file with four sections: "Directory Purpose", "May Modify", "Do Not Modify", and "Conventions".

### Failure Handling

1. The program must never fail silently.

## Local Agent Design Principles

### Core Philosophy

1. For tools running on the user's own computer: an honest crash is infinitely better than lying that everything is fine.

### Specific Principles

1. Fail fast instead of degrading gracefully.
2. Never fail silently.
3. Never return fake default values.
4. Throw errors with as much detail as possible.
5. Expose problems immediately.
6. Never hide errors.

## Project-Specific

### Goal

1. This is a backend for an AI coding assistant running in the user's local workspace, paired with a frontend VS Code extension to provide IDE-level autonomous coding experience.

### Local Runtime Design

1. There are no cloud-service features; there is no graceful degradation, high availability, or multi-tenancy.
2. Failures must be transparent: throw detailed errors directly and never fail silently.
3. Be developer-friendly: crash directly when problems occur to make debugging easier.
4. Zero external dependencies: no database, no message queue, and no cloud services required.

### Workspace Safety

1. All software data must be stored in the independent `${workspace_abs_path}/.boxteam/` directory.

### Architecture Principles

1. The frontend communicates with FastAPI; `JobService` schedules `AgentExecutionService`, and `AgentExecutionService` drives `DeepAgent` to execute built-in tools.
2. The event bus pushes real-time updates to the frontend through SSE.

### Frontend State Management Principles

1. The backend is the single source of truth (Backend-First State Management).
2. The frontend does not own the authoritative source of business state; all state changes must go through backend APIs.
3. On success, replace the frontend state entirely with the full object returned by the backend, rather than partially patching fields.
4. On failure, proactively re-fetch data from the backend to ensure consistency.
5. This applies to core business states such as agent switching, session management, and message sending.

### Runtime Instructions

1. Use `bun` for JS/TS environments; use `bun install` to install dependencies and `bun start` to start frontend development.
2. Use `uv` for Python environments; use `uv sync` to install dependencies and `uv run uvicorn app.main:app --host 127.0.0.1 --port 8000` to start the backend server.
3. API documentation is available at http://127.0.0.1:8000/api/v1/docs

### Configuration

1. Use `bun` for all JS/TS-related tooling.
2. Use `uv` for all Python-related tooling.

## Agent Collaboration

### Collaboration Style

1. This project is generated with vibe coding assistance throughout. The agent has limited context and intelligence, so if you encounter anything that does not follow the development standards, actively inform the user.

### Environment Configuration

1. If you encounter environment configuration issues during development, prioritize skipping them, implement the other parts first, and ask the user for configuration at the end; do not make random changes to environment settings.

## Additional

### Extra Instructions Manually Added by the User Based on Agent Feedback

1. Template example; keep this line when organizing `AGENTS.md`.
