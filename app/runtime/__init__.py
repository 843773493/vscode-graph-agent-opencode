"""
运行时依赖注入和懒加载工厂。

此模块集中管理应用服务的懒加载，解决模块间的循环依赖问题。
所有服务都通过 get_xxx() 函数获取，确保单例和延迟初始化。

注意：标准库模块（如 asyncio, time, uuid）应直接在各自模块顶部导入，
      不要使用懒加载，以保持代码清晰和 IDE 友好。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.services.job_service import JobService
    from app.services.message_service import MessageService
    from app.services.session_service import SessionService
    from app.services.agent_execution_service import AgentExecutionService
    from app.services.config_service import ConfigService

# 缓存已创建的单例实例
_job_service_instance: JobService | None = None
_message_service_instance: MessageService | None = None
_session_service_instance: SessionService | None = None
_agent_execution_service_instance: AgentExecutionService | None = None
_config_service_instance: ConfigService | None = None


def get_job_service() -> JobService:
    """获取 JobService 单例（懒加载）"""
    global _job_service_instance
    if _job_service_instance is None:
        # 延迟导入以避免循环依赖
        from app.services.job_service import JobService
        _job_service_instance = JobService.get_instance()
    return _job_service_instance


def get_message_service() -> MessageService:
    """获取 MessageService 单例（懒加载）"""
    global _message_service_instance
    if _message_service_instance is None:
        from app.services.message_service import MessageService
        _message_service_instance = MessageService.get_instance()
    return _message_service_instance


def get_session_service() -> SessionService:
    """获取 SessionService 单例（懒加载）"""
    global _session_service_instance
    if _session_service_instance is None:
        from app.services.session_service import SessionService
        _session_service_instance = SessionService.get_instance()
    return _session_service_instance


def get_agent_execution_service() -> AgentExecutionService:
    """获取 AgentExecutionService 单例（懒加载）"""
    global _agent_execution_service_instance
    if _agent_execution_service_instance is None:
        from app.services.agent_execution_service import AgentExecutionService
        _agent_execution_service_instance = AgentExecutionService.get_instance()
    return _agent_execution_service_instance


def get_config_service() -> ConfigService:
    """获取 ConfigService 单例（懒加载）"""
    global _config_service_instance
    if _config_service_instance is None:
        from app.services.config_service import ConfigService
        _config_service_instance = ConfigService.get_instance()
    return _config_service_instance
