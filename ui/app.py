"""Слой ui: оболочка приложения и маршрутизация.

/       — страница пользователя (чат)
/admin  — страница администратора (конфигурация агентов, отчётность по логам)
"""

import asyncio
import threading

import flet as ft

from data.storage import AgentConfigStore, SessionStore, LogStore
from domain.agent import Agent, AgentConfig
from ui.user_page import UserPage
from ui.admin_page import AdminPage


class AppState:
    """Общее состояние: хранилища + кэш экземпляров Agent."""

    def __init__(self):
        self.config_store = AgentConfigStore()
        self.session_store = SessionStore()
        self.log_store = LogStore()
        self._agents: dict[str, Agent] = {}
        self._lock = threading.Lock()
        # цикл событий сессии; выставляется в main (обработчики Flet исполняются
        # в пуле потоков, где running loop отсутствует)
        self.loop: asyncio.AbstractEventLoop | None = None

    def get_agent(self, agent_id: str) -> Agent:
        """Возвращает Agent. Бросает LLMError, если не заданы ключи API."""
        with self._lock:
            if agent_id not in self._agents:
                raw = self.config_store.get(agent_id)
                if raw is None:
                    raise ValueError(f"Агент {agent_id} не найден")
                self._agents[agent_id] = Agent(
                    AgentConfig.from_dict(raw), self.log_store, self.session_store
                )
            return self._agents[agent_id]

    def invalidate(self, agent_id: str | None = None) -> None:
        """Сброс кэша после изменения конфигурации администратором."""
        with self._lock:
            if agent_id:
                self._agents.pop(agent_id, None)
            else:
                self._agents.clear()


async def main(page: ft.Page):
    state = AppState()
    state.loop = asyncio.get_running_loop()
    page.title = "LLM Chat — Агент"
    page.theme_mode = ft.ThemeMode.LIGHT
    page.padding = 10

    def show(route: str):
        page.controls.clear()
        if route.startswith("/admin"):
            AdminPage(page, state).build()
        else:
            UserPage(page, state).build()
        page.update()

    def on_route_change(e):
        show(page.route)

    page.on_route_change = on_route_change
    show(page.route or "/")
