from __future__ import annotations
import os
import json
import textwrap
import time
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any, List, Callable

import asyncio
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import MemorySaver
from deepagents import create_deep_agent
from deepagents.backends.filesystem import FilesystemBackend
from langchain_core.tools import tool
from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain.agents.middleware import ModelFallbackMiddleware
from langchain.tools.tool_node import ToolCallRequest
from langchain.messages import ToolMessage

from app.core.path_utils import get_workspace_root, get_logs_dir
from app.core.job_event_bus import EventType, JobEventBus
from app.core.background_message_bus import BackgroundMessageBus
from app.core.background_task_registry import BackgroundTaskRegistry
from app.schemas.background_message import BackgroundMessageKind
from app.schemas.common import RunMode
from app.schemas.message import MessageCreate, MessageRunRequest, RunOptions
from app.services.config_service import ConfigService
from app.services.message_service import MessageService


class LLMLoggingMiddleware(AgentMiddleware):
    """唯一职责：存储每个LLM调用的完整原始请求/响应"""

    def __init__(self):
        self._prepared_session_dirs: set[str] = set()
    
    def _get_session_id(self, runtime) -> str:
        """直接读取 LangChain 的 thread_id。"""
        execution_info = runtime.execution_info
        return execution_info.thread_id

    def _ensure_session_dir(self, session_id: str) -> Path:
        logs_dir = get_logs_dir() / "llm_requests" / session_id
        if session_id not in self._prepared_session_dirs:
            logs_dir.mkdir(exist_ok=True, parents=True)
            self._prepared_session_dirs.add(session_id)
        return logs_dir
    
    def _save_log(self, session_id: str, request: ModelRequest, response: ModelResponse) -> None:
        try:
            logs_dir = self._ensure_session_dir(session_id)
            
            timestamp = int(time.time() * 1000)
            log_file = logs_dir / f"{timestamp}.json"
            
            def serialize_object(obj):
                if hasattr(obj, '__dict__'):
                    result = {}
                    for key, value in obj.__dict__.items():
                        if not key.startswith('_'):
                            try:
                                json.dumps(value, default=str)
                                result[key] = value
                            except:
                                result[key] = str(value)
                    return result
                return str(obj)
            
            log_data = {
                "timestamp": timestamp,
                "session_id": session_id,
                "request": serialize_object(request),
                "response": serialize_object(response)
            }
            
            with open(log_file, "w", encoding="utf-8") as f:
                json.dump(log_data, f, ensure_ascii=False, indent=2, default=str)
                
        except Exception:
            pass

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        session_id = self._get_session_id(request.runtime)
        model_name = getattr(request.model, "model_name", str(request.model))
        
        response = handler(request)
        
        self._save_log(session_id, request, response)
        
        try:
            bus = JobEventBus.get_instance()
            import asyncio
            asyncio.create_task(bus.publish(
                job_id=session_id,
                event_type=EventType.LLM_REQUEST,
                payload={"model": model_name, "timestamp": int(time.time() * 1000)},
                agent_id="deep_agent"
            ))
        except Exception:
            pass

        return response

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        session_id = self._get_session_id(request.runtime)
        model_name = getattr(request.model, "model_name", str(request.model))
        
        response = await handler(request)
        
        self._save_log(session_id, request, response)
        
        try:
            bus = JobEventBus.get_instance()
            await bus.publish(
                job_id=session_id,
                event_type=EventType.LLM_REQUEST,
                payload={"model": model_name, "timestamp": int(time.time() * 1000)},
                agent_id="deep_agent"
            )
        except Exception:
            pass

        return response


class ExecutionTraceMiddleware(AgentMiddleware):
    """唯一职责：存储完整的执行轨迹事件"""
    
    def __init__(self):
        self._session_start_times = {}
    
    def _get_session_id(self, runtime) -> str:
        """直接读取 LangChain 的 thread_id。"""
        execution_info = runtime.execution_info
        return execution_info.thread_id
    
    def _save_trace_event(self, session_id: str, event_type: str, data: dict) -> None:
        try:
            logs_dir = get_logs_dir() / "traces"
            logs_dir.mkdir(exist_ok=True, parents=True)
            
            log_file = logs_dir / f"trace_{session_id}.jsonl"
            
            timestamp = int(time.time() * 1000)
            log_data = {
                "timestamp": timestamp,
                "event_type": event_type,
                "data": data
            }
            
            # 追加写入JSONL格式，每个事件一行
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(log_data, ensure_ascii=False, default=str) + "\n")
                
        except Exception:
            pass

    def before_agent(self, state: dict[str, Any], runtime):
        session_id = self._get_session_id(runtime)
        self._save_trace_event(session_id, "agent_start", {"message_count": len(state.get("messages", []))})
        return None

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command],
    ) -> ToolMessage | Command:
        session_id = self._get_session_id(request.runtime)
        tool_call = request.tool_call
        tool_name = tool_call.get("name", "unknown_tool")
        
        self._save_trace_event(session_id, "tool_call_start", {
            "tool_name": tool_name,
            "args": tool_call.get("args", {})
        })
        
        result = handler(request)
        
        self._save_trace_event(session_id, "tool_call_end", {
            "tool_name": tool_name,
            "result": str(result)
        })
        
        return result

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command],
    ) -> ToolMessage | Command:
        session_id = self._get_session_id(request.runtime)
        tool_call = request.tool_call
        tool_name = tool_call.get("name", "unknown_tool")
        
        self._save_trace_event(session_id, "tool_call_start", {
            "tool_name": tool_name,
            "args": tool_call.get("args", {})
        })
        
        result = await handler(request)
        
        self._save_trace_event(session_id, "tool_call_end", {
            "tool_name": tool_name,
            "result": str(result)
        })
        
        return result

    def after_agent(self, state: dict[str, Any], runtime):
        session_id = self._get_session_id(runtime)
        self._save_trace_event(session_id, "agent_end", {"final_message_count": len(state.get("messages", []))})
        return None


class AgentExecutionService:
    _instance: Optional[AgentExecutionService] = None
    
    def __init__(self):
        config_service = ConfigService.get_instance()
        providers = config_service.get_llm_providers()
        
        # 构建模型列表，支持fallback
        models = []
        for provider in providers:
            model = ChatOpenAI(
                model=provider["model"],
                api_key=provider["api_key"],
                base_url=provider["endpoint"],
                use_responses_api=(provider.get("interface") == "responses"),
                max_retries=3,
            )
            models.append(model)
        
        # 主模型和fallback模型
        self.model = models[0] if models else None
        self.midware_fallback_models = ModelFallbackMiddleware(*models[1:]) if len(models) > 1 else None
        self._agent_cache = {}

    def _get_repo_root(self) -> Path:
        return Path(__file__).resolve().parents[2]

    def _get_python_executable(self) -> Path:
        candidates = []

        env_python = os.environ.get("BOXTEAM_PYTHON_EXECUTABLE")
        if env_python:
            candidates.append(Path(env_python))

        candidates.extend(
            [
                get_workspace_root() / ".venv" / "Scripts" / "python.exe",
                self._get_repo_root() / ".venv" / "Scripts" / "python.exe",
            ]
        )

        for python_executable in candidates:
            if python_executable.exists():
                return python_executable

        candidate_list = "\n".join(str(path) for path in candidates)
        raise RuntimeError(
            "未找到可用的 Python 解释器。\n"
            "已检查以下路径：\n"
            f"{candidate_list}\n"
            "请确认仓库根目录或工作区根目录下存在 .venv\\Scripts\\python.exe，"
            "或者通过 BOXTEAM_PYTHON_EXECUTABLE 显式指定。"
        )

    def _create_python_execution_tool(self, session_id: str, agent_id: str = "deep_agent"):
        python_executable = self._get_python_executable()

        @tool("python_exec")
        async def python_exec(code: str, timeout_seconds: int = 30) -> Dict[str, Any]:
            """使用工作区 .venv\\Scripts\\python.exe 执行 Python 代码。"""
            if not code.strip():
                raise ValueError("code 不能为空")

            workspace_root = get_workspace_root()
            cache_dir = workspace_root / ".boxteam" / "cache" / "python_exec"
            cache_dir.mkdir(parents=True, exist_ok=True)

            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                suffix=".py",
                dir=cache_dir,
                delete=False,
            ) as temp_file:
                script_path = Path(temp_file.name)
                temp_file.write(textwrap.dedent(code))

            env = os.environ.copy()
            existing_pythonpath = env.get("PYTHONPATH", "")
            env["PYTHONPATH"] = os.pathsep.join(
                [str(workspace_root), existing_pythonpath] if existing_pythonpath else [str(workspace_root)]
            )

            try:
                process = await asyncio.create_subprocess_exec(
                    str(python_executable),
                    str(script_path),
                    cwd=str(workspace_root),
                    env=env,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )

                try:
                    stdout_bytes, stderr_bytes = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
                except asyncio.TimeoutError:
                    process.kill()
                    stdout_bytes, stderr_bytes = await process.communicate()
                    raise RuntimeError(
                        "Python 代码执行超时。\n"
                        f"超时时间: {timeout_seconds}s\n"
                        f"STDOUT:\n{stdout_bytes.decode('utf-8', errors='replace')}\n"
                        f"STDERR:\n{stderr_bytes.decode('utf-8', errors='replace')}"
                    )
            finally:
                try:
                    script_path.unlink(missing_ok=True)
                except OSError:
                    pass

            stdout_text = stdout_bytes.decode("utf-8", errors="replace")
            stderr_text = stderr_bytes.decode("utf-8", errors="replace")

            result = {
                "python_executable": str(python_executable),
                "returncode": process.returncode,
                "stdout": stdout_text,
                "stderr": stderr_text,
            }

            if process.returncode != 0:
                raise RuntimeError(
                    "Python 代码执行失败。\n"
                    f"退出码: {process.returncode}\n"
                    f"STDOUT:\n{stdout_text}\n"
                    f"STDERR:\n{stderr_text}"
                )

            return result

        return python_exec

    def _create_system_time_emitter_tool(self, session_id: str, agent_id: str = "deep_agent"):
        @tool("emit_system_time_messages")
        async def emit_system_time_messages(
            interval_seconds: float = 1.0,
            message_count: int = 5,
            source_id: str | None = None,
        ) -> Dict[str, Any]:
            """按固定间隔向后台消息总线发送当前系统时间。"""
            if interval_seconds <= 0:
                raise ValueError("interval_seconds 必须大于 0")
            if message_count <= 0:
                raise ValueError("message_count 必须大于 0")

            resolved_source_id = source_id or f"time_{session_id}_{int(time.time() * 1000)}"
            emitted_messages = []
            message_service = BackgroundMessageBus.get_instance()

            for index in range(message_count):
                current_time = datetime.now().isoformat(timespec="seconds")
                message = message_service.emit(
                    session_id,
                    agent_id,
                    current_time,
                    kind=BackgroundMessageKind.normal,
                    source_id=resolved_source_id,
                    payload={
                        "index": index + 1,
                        "message_count": message_count,
                        "interval_seconds": interval_seconds,
                    },
                )
                emitted_messages.append(message.model_dump(mode="json"))

                if index < message_count - 1:
                    await asyncio.sleep(interval_seconds)

            return {
                "session_id": session_id,
                "agent_id": agent_id,
                "source_id": resolved_source_id,
                "interval_seconds": interval_seconds,
                "message_count": message_count,
                "messages": emitted_messages,
            }

        return emit_system_time_messages

    def _create_monitor_session_agent_end_tool(self, session_id: str, agent_id: str = "deep_agent"):
        @tool("monitor_session_agent_end")
        async def monitor_session_agent_end(
            target_session_id: str,
            timeout_seconds: int = 300,
            poll_interval_seconds: float = 1.0,
        ) -> Dict[str, Any]:
            """启动后台任务监控另一个 session 的 AGENT_END 事件，并返回任务句柄。"""
            if not target_session_id:
                raise ValueError("target_session_id 不能为空")
            if timeout_seconds <= 0:
                raise ValueError("timeout_seconds 必须大于 0")
            if poll_interval_seconds <= 0:
                raise ValueError("poll_interval_seconds 必须大于 0")

            submitted_at = datetime.now()

            async def _monitor_background_task() -> dict[str, Any]:
                job_event_bus = JobEventBus.get_instance()
                message_bus = BackgroundMessageBus.get_instance()
                deadline = asyncio.get_running_loop().time() + timeout_seconds

                while True:
                    events = await job_event_bus.list_events(target_session_id, limit=1000)

                    for event in events:
                        if event.type != EventType.AGENT_END:
                            continue
                        if event.timestamp <= submitted_at:
                            continue

                        final_text = event.payload.get("final_text")
                        if not final_text:
                            continue

                        emitted_message = message_bus.emit(
                            session_id,
                            agent_id,
                            final_text,
                            kind=BackgroundMessageKind.interrupt,
                            source_id=f"monitor:{target_session_id}",
                            payload={
                                "target_session_id": target_session_id,
                                "target_event_id": event.event_id,
                                "target_event_timestamp": event.timestamp.isoformat(),
                                "final_text": final_text,
                            },
                        )

                        return {
                            "target_session_id": target_session_id,
                            "target_event_id": event.event_id,
                            "target_event_timestamp": event.timestamp.isoformat(),
                            "final_text": final_text,
                            "emitted_background_message": emitted_message.model_dump(mode="json"),
                        }

                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        raise TimeoutError(f"监控 session {target_session_id} 的 AGENT_END 超时")

                    await asyncio.sleep(min(poll_interval_seconds, remaining))

            handle = BackgroundTaskRegistry.get_instance().spawn(
                session_id=session_id,
                task_name="monitor_session_agent_end",
                runner=_monitor_background_task,
                metadata={
                    "target_session_id": target_session_id,
                    "timeout_seconds": timeout_seconds,
                    "poll_interval_seconds": poll_interval_seconds,
                    "submitted_at": submitted_at.isoformat(),
                },
            )

            return handle.to_dict()

        return monitor_session_agent_end

    def _create_background_message_collection_tool(self, session_id: str, agent_id: str = "deep_agent"):
        @tool("collect_background_messages")
        async def collect_background_messages(
            source_id: str | None = None,
            after_message_id: str | None = None,
            timeout_seconds: int = 300,
            poll_interval_seconds: float = 1.0,
            stop_on_interrupt: bool = True,
        ) -> Dict[str, Any]:
            """持续收集当前 session/agent 的后台消息。

            默认会一直等待到超时；如果收到 interrupt 类消息，就立即停止并返回。
            如果后台 Python 代码在循环输出，建议为同一条流显式传入稳定的 source_id。
            """
            batch = await BackgroundMessageBus.get_instance().collect(
                session_id,
                agent_id,
                source_id=source_id,
                after_message_id=after_message_id,
                timeout_seconds=timeout_seconds,
                poll_interval_seconds=poll_interval_seconds,
                stop_on_interrupt=stop_on_interrupt,
            )
            return batch.model_dump(mode="json")

        return collect_background_messages

    def _create_send_message_to_session_tool(self, agent_id: str = "deep_agent"):
        @tool("send_message_to_session")
        async def send_message_to_session(
            target_session_id: str,
            content: str,
        ) -> Dict[str, Any]:
            """模拟用户向目标 session 发送消息，并立即启动目标 session 的新任务。"""
            if not target_session_id:
                raise ValueError("target_session_id 不能为空")
            if not content.strip():
                raise ValueError("content 不能为空")

            run_request = MessageRunRequest(
                message=MessageCreate(
                    role="user",
                    content=content,
                ),
                run=RunOptions(
                    mode=RunMode.single_agent,
                    agent_id=agent_id,
                ),
            )

            result = await MessageService.get_instance().create_and_run(target_session_id, run_request)
            return result.model_dump(mode="json")

        return send_message_to_session
    
    @classmethod
    def get_instance(cls) -> AgentExecutionService:
        if cls._instance is None:
            cls._instance = AgentExecutionService()
        return cls._instance
    
    def _get_or_create_agent(self, session_id: str):
        if session_id in self._agent_cache:
            return self._agent_cache[session_id]
        
        workspace_root = get_workspace_root()
        backend = FilesystemBackend(
            root_dir=str(workspace_root),
            virtual_mode=True,
        )
        
        checkpointer = MemorySaver()
        
        # 构建中间件列表，添加fallback中间件
        middleware_list = [
            LLMLoggingMiddleware(),
            ExecutionTraceMiddleware(),
        ]
        
        if self.midware_fallback_models:
            middleware_list.append(self.midware_fallback_models)

        python_execution_tool = self._create_python_execution_tool(session_id)
        system_time_emitter_tool = self._create_system_time_emitter_tool(session_id)
        monitor_session_agent_end_tool = self._create_monitor_session_agent_end_tool(session_id)
        background_message_collection_tool = self._create_background_message_collection_tool(session_id)
        send_message_to_session_tool = self._create_send_message_to_session_tool()
        
        agent = create_deep_agent(
            model=self.model,
            tools=[
                python_execution_tool,
                system_time_emitter_tool,
                monitor_session_agent_end_tool,
                background_message_collection_tool,
                send_message_to_session_tool,
            ],
            backend=backend,
            system_prompt="You are a helpful assistant.",
            checkpointer=checkpointer,
            middleware=middleware_list
        )
        
        self._agent_cache[session_id] = agent
        return agent
    
    @classmethod
    async def run_step(cls, session_id: str, message: str) -> str:
        """
        执行单步Agent调用
        
        Args:
            session_id: 会话ID
            message: 用户输入消息
            
        Returns:
            Agent响应内容
        """
        instance = cls.get_instance()
        agent = instance._get_or_create_agent(session_id)
        bus = JobEventBus.get_instance()
        
        # 发布AGENT_START事件
        await bus.publish(
            job_id=session_id,
            event_type=EventType.AGENT_START,
            payload={"message": message},
            agent_id="deep_agent"
        )
        
        config = {
            "configurable": {
                "thread_id": session_id,
            }
        }
        
        # 发布AGENT_STEP事件
        await bus.publish(
            job_id=session_id,
            event_type=EventType.AGENT_STEP,
            payload={"phase": "invoking_agent"},
            agent_id="deep_agent"
        )
        
        try:
            result = await agent.ainvoke(
                {"messages": [{"role": "user", "content": message}]},
                config=config
            )
            
            response_content = result["messages"][-1].content
            
            # 发布AGENT_END事件
            await bus.publish(
                job_id=session_id,
                event_type=EventType.AGENT_END,
                payload={
                    "response_length": len(response_content),
                    "final_text": response_content,
                },
                agent_id="deep_agent"
            )
            
            return response_content

        except Exception as e:
            # 发布ERROR事件
            await bus.publish(
                job_id=session_id,
                event_type=EventType.ERROR,
                payload={"error": str(e), "phase": "agent_execution"},
                agent_id="deep_agent"
            )
            raise

    @classmethod
    @classmethod
    def get_for_session(cls, session_id: str):
        """
        获取指定会话的Agent实例
        """
        instance = cls.get_instance()
        return instance._get_or_create_agent(session_id)
    
    @classmethod
    def get_available_tools(cls) -> List[Dict[str, Any]]:
        """
        获取DeepAgent支持的所有可用工具列表
        
        本地运行原则：失败时快速崩溃，不静默降级，不隐藏问题
        """
        # 从agent实例动态获取真实工具列表
        session_id = "tools_inspection_session"
        agent = cls.get_instance()._get_or_create_agent(session_id)
        
        # 使用正确的inspect_agent_tools实现
        tool_map = {}
        graph_view = agent.get_graph()
        nodes = getattr(graph_view, "nodes", {}) or {}
        
        for _, node in nodes.items():
            candidate = getattr(node, "data", node)
            if hasattr(candidate, "tools_by_name"):
                tool_map.update(candidate.tools_by_name)
        
        if not tool_map:
            raise RuntimeError(
                "无法从Agent实例中提取工具列表！\n"
                "Agent图中未找到包含tools_by_name属性的节点。\n"
                "这是严重错误，需要立即修复，不能静默降级。"
            )
        
        tools = []
        for tool_name, tool in tool_map.items():
            tool_def = {
                "id": tool_name,
                "name": tool_name,
                "description": getattr(tool, "description", ""),
                "parameters": tool.args_schema.schema() if hasattr(tool, 'args_schema') else {"type": "object", "properties": {}},
                "category": "general"
            }
            tools.append(tool_def)
        
        return tools
