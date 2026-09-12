"""Слой ui: страница администратора.

- CRUD конфигураций агентов (провайдер, модель, обязательные параметры запросов:
  таймаут GigaChat (env GIGACHAT_TIMEOUT, по умолчанию 300 с),
  scope (env GIGACHAT_SCOPE, по умолчанию GIGACHAT_API_PERS), verify SSL);
- настройки диалогов: хранить историю сессии (LLM «помнит» контекст),
  хранить историю пользователя, порог суммаризации;
- отчётность по логам: фильтры, таблица, статистика.
"""

import asyncio
import json
import os

import flet as ft

from domain.agent import AgentConfig, PROVIDER_MODELS


class AdminPage:
    def __init__(self, page: ft.Page, state):
        self.page = page
        self.state = state
        self.editing_id: str | None = None
        self._log_queue: asyncio.Queue = asyncio.Queue()

    # ---------- построение ----------

    def build(self):
        self.agents_column = ft.Column(spacing=6)
        self._refresh_agents_list()

        # --- форма агента ---
        self.f_name = ft.TextField(label="Имя агента", width=320)
        self.f_provider = ft.Dropdown(
            label="Провайдер LLM", width=320,
            options=[ft.dropdown.Option("deepseek", "DeepSeek"),
                     ft.dropdown.Option("gigachat", "GigaChat")],
            value="deepseek",
            on_change=self._on_provider_change,
        )
        self.f_model = ft.Dropdown(label="Модель", width=320,
                                   options=self._model_options("deepseek"),
                                   value=PROVIDER_MODELS["deepseek"][0])
        self.f_temperature = ft.TextField(label="Temperature", width=150, value="0.7")
        self.f_max_tokens = ft.TextField(label="Max tokens", width=150, value="2048")
        self.f_timeout = ft.TextField(
            label="Таймаут, с (0 = по умолчанию)", width=240, value="0",
            hint_text="GigaChat: env GIGACHAT_TIMEOUT, дефолт 300")
        self.f_scope = ft.TextField(
            label="GigaChat scope (пусто = env GIGACHAT_SCOPE)", width=320,
            hint_text="GIGACHAT_API_PERS / GIGACHAT_API_B2B / GIGACHAT_API_CORP")
        self.f_verify_ssl = ft.Checkbox(label="GigaChat: проверять SSL-сертификат", value=False)
        self.f_system_prompt = ft.TextField(label="System prompt", multiline=True,
                                            min_lines=2, max_lines=4, width=660)
        self.f_keep_session = ft.Checkbox(
            label="Хранить историю сессии (LLM «помнит» предыдущие сообщения)", value=True)
        self.f_keep_user = ft.Checkbox(
            label="Хранить историю пользователя (сохранять диалоги на диск)", value=True)
        self.f_summarize = ft.TextField(
            label="Суммаризация после N сообщений (0 = выкл.)", width=320, value="20")
        self.f_count_tokens = ft.Checkbox(
            label="Подсчёт токенов (запрос / ответ / диалог / сессия)", value=True)

        form = ft.Container(
            border=ft.border.all(1, ft.colors.BLACK12), border_radius=8, padding=12,
            content=ft.Column(
                [
                    ft.Text("Конфигурация агента", size=16, weight=ft.FontWeight.BOLD),
                    ft.Row([self.f_name, self.f_provider, self.f_model], wrap=True),
                    ft.Row([self.f_temperature, self.f_max_tokens, self.f_timeout], wrap=True),
                    ft.Row([self.f_scope, self.f_verify_ssl], wrap=True),
                    self.f_system_prompt,
                    ft.Text("Настройки диалогов", weight=ft.FontWeight.BOLD),
                    ft.Row([self.f_keep_session, self.f_keep_user, self.f_summarize], wrap=True),
                    self.f_count_tokens,
                    ft.Row([
                        ft.ElevatedButton("Сохранить агента", icon=ft.icons.SAVE,
                                          on_click=self.on_save_agent),
                        ft.TextButton("Сбросить форму", on_click=lambda e: self._reset_form()),
                    ]),
                ],
                spacing=10,
            ),
        )

        # --- отчётность по логам ---
        self.filter_agent = ft.Dropdown(label="Агент", width=220, options=[ft.dropdown.Option("", "Все")],
                                        value="", on_change=lambda e: self._refresh_logs())
        self.filter_event = ft.Dropdown(
            label="Событие", width=240, value="",
            options=[ft.dropdown.Option("", "Все"),
                     ft.dropdown.Option("request_sent", "request_sent (запрос в LLM)"),
                     ft.dropdown.Option("response_received", "response_received (ответ LLM)"),
                     ft.dropdown.Option("error", "error"),
                     ft.dropdown.Option("summary_created", "summary_created"),
                     ft.dropdown.Option("title_generated", "title_generated")],
            on_change=lambda e: self._refresh_logs())
        self.logs_stats = ft.Text("", size=13)
        self.logs_table = ft.DataTable(
            columns=[
                ft.DataColumn(ft.Text("Время")),
                ft.DataColumn(ft.Text("Агент")),
                ft.DataColumn(ft.Text("Событие")),
                ft.DataColumn(ft.Text("Детали")),
            ],
            rows=[],
        )
        self.tokens_table = ft.DataTable(
            columns=[
                ft.DataColumn(ft.Text("Агент")),
                ft.DataColumn(ft.Text("Запросов")),
                ft.DataColumn(ft.Text("Входящие")),
                ft.DataColumn(ft.Text("Ответные")),
                ft.DataColumn(ft.Text("Всего")),
                ft.DataColumn(ft.Text("Диалогов")),
                ft.DataColumn(ft.Text("Ср. на запрос")),
            ],
            rows=[],
        )
        self._refresh_logs()

        # живое обновление: новые записи LogStore -> очередь -> async-цикл.
        # build() может исполняться в потоке пула (без running loop), поэтому
        # цикл берём из AppState, где его сохранила async-функция main.
        loop = self.state.loop
        if loop is not None and loop.is_running():
            self.state.log_store.subscribe(
                lambda entry: loop.call_soon_threadsafe(self._log_queue.put_nowait, entry)
            )
            asyncio.run_coroutine_threadsafe(self._drain_logs(), loop)
        # иначе — без живого обновления; остаётся кнопка «Обновить»

        logs_block = ft.Container(
            border=ft.border.all(1, ft.colors.BLACK12), border_radius=8, padding=12,
            content=ft.Column(
                [
                    ft.Row([ft.Text("Отчётность по логам", size=16, weight=ft.FontWeight.BOLD),
                            ft.ElevatedButton("Обновить", icon=ft.icons.REFRESH,
                                              on_click=lambda e: self._refresh_logs(page_update=True))]),
                    ft.Row([self.filter_agent, self.filter_event]),
                    self.logs_stats,
                    ft.Container(ft.Column([self.logs_table], scroll=ft.ScrollMode.AUTO, height=320),
                                 border=ft.border.all(1, ft.colors.BLACK12), border_radius=6),
                    ft.Text("Токены по агентам", size=16, weight=ft.FontWeight.BOLD),
                    ft.Container(ft.Column([self.tokens_table], scroll=ft.ScrollMode.AUTO, height=200),
                                 border=ft.border.all(1, ft.colors.BLACK12), border_radius=6),
                ],
                spacing=10,
            ),
        )

        header = ft.Row([
            ft.Text("Администрирование", size=20, weight=ft.FontWeight.BOLD),
            ft.Container(expand=True),
            ft.TextButton("К чату", icon=ft.icons.CHAT, on_click=lambda e: self.page.go("/")),
        ])
        env_info = ft.Text(
            f"DEEPSEEK_API_KEY: {'задан' if os.environ.get('DEEPSEEK_API_KEY') else 'НЕ ЗАДАН'}   |   "
            f"GIGACHAT_CREDENTIALS: {'заданы' if os.environ.get('GIGACHAT_CREDENTIALS') else 'НЕ ЗАДАНЫ'}   |   "
            f"GIGACHAT_TIMEOUT={os.environ.get('GIGACHAT_TIMEOUT', '300')}   |   "
            f"GIGACHAT_SCOPE={os.environ.get('GIGACHAT_SCOPE', 'GIGACHAT_API_PERS')}",
            size=12, color=ft.colors.BLUE_GREY_700)

        self.page.add(
            ft.Column([header, env_info, ft.Divider(),
                       self.agents_column, ft.Divider(),
                       form, ft.Divider(),
                       logs_block],
                      scroll=ft.ScrollMode.AUTO, expand=True, spacing=10)
        )

    # ---------- агенты ----------

    def _model_options(self, provider: str):
        return [ft.dropdown.Option(m) for m in PROVIDER_MODELS.get(provider, [])]

    def _on_provider_change(self, e):
        provider = self.f_provider.value
        self.f_model.options = self._model_options(provider)
        self.f_model.value = PROVIDER_MODELS[provider][0]
        self.page.update()

    def _refresh_agents_list(self):
        self.agents_column.controls = [ft.Text("Агенты", size=16, weight=ft.FontWeight.BOLD)]
        for a in self.state.config_store.all():
            self.agents_column.controls.append(
                ft.Container(
                    border=ft.border.all(1, ft.colors.BLACK12), border_radius=6, padding=8,
                    content=ft.Row([
                        ft.Text(f"{a['name']}  —  {a['provider']} / {a['model']}", expand=True),
                        ft.TextButton("Редактировать", on_click=lambda e, x=a: self._load_into_form(x)),
                        ft.TextButton("Удалить", on_click=lambda e, x=a["id"]: self.on_delete_agent(x)),
                    ]),
                )
            )

    def _load_into_form(self, a: dict):
        self.editing_id = a["id"]
        self.f_name.value = a["name"]
        self.f_provider.value = a["provider"]
        self.f_model.options = self._model_options(a["provider"])
        self.f_model.value = a["model"]
        self.f_temperature.value = str(a.get("temperature", 0.7))
        self.f_max_tokens.value = str(a.get("max_tokens", 2048))
        self.f_timeout.value = str(a.get("timeout", 0))
        self.f_scope.value = a.get("gigachat_scope", "")
        self.f_verify_ssl.value = a.get("gigachat_verify_ssl", False)
        self.f_system_prompt.value = a.get("system_prompt", "")
        self.f_keep_session.value = a.get("keep_session_history", True)
        self.f_keep_user.value = a.get("keep_user_history", True)
        self.f_summarize.value = str(a.get("summarize_after", 20))
        self.f_count_tokens.value = a.get("count_tokens", True)
        self.page.update()

    def _reset_form(self):
        self.editing_id = None
        self.f_name.value = ""
        self.f_system_prompt.value = ""
        self.page.update()

    def on_save_agent(self, e):
        if not (self.f_name.value or "").strip():
            self._snack("Укажите имя агента")
            return
        cfg = AgentConfig(
            id=self.editing_id or "",
            name=self.f_name.value.strip(),
            provider=self.f_provider.value,
            model=self.f_model.value,
            temperature=float(self.f_temperature.value or 0.7),
            max_tokens=int(self.f_max_tokens.value or 2048),
            system_prompt=self.f_system_prompt.value or "",
            keep_session_history=bool(self.f_keep_session.value),
            keep_user_history=bool(self.f_keep_user.value),
            summarize_after=int(self.f_summarize.value or 0),
            count_tokens=bool(self.f_count_tokens.value),
            timeout=int(self.f_timeout.value or 0),
            gigachat_scope=(self.f_scope.value or "").strip(),
            gigachat_verify_ssl=bool(self.f_verify_ssl.value),
        )
        saved = self.state.config_store.save(cfg.to_dict())
        self.state.invalidate(saved["id"])
        self.editing_id = None
        self._refresh_agents_list()
        self._refresh_logs()
        self.page.update()

    def on_delete_agent(self, agent_id: str):
        self.state.config_store.delete(agent_id)
        self.state.invalidate(agent_id)
        self._refresh_agents_list()
        self.page.update()

    # ---------- логи ----------

    def _refresh_logs(self, page_update: bool = False):
        agents = self.state.config_store.all()
        self.filter_agent.options = [ft.dropdown.Option("", "Все")] + [
            ft.dropdown.Option(a["name"], a["name"]) for a in agents
        ]
        agent = self.filter_agent.value or None
        event = self.filter_event.value or None
        entries = self.state.log_store.filter(agent=agent, event=event)
        stats = self.state.log_store.stats()
        self.logs_stats.value = (
            f"Всего записей в буфере: {stats['total']}; "
            + "; ".join(f"{k}: {v}" for k, v in sorted(stats["by_event"].items()))
        )
        self.logs_table.rows = [
            ft.DataRow(cells=[
                ft.DataCell(ft.Text(e["ts"], size=12)),
                ft.DataCell(ft.Text(e["agent"], size=12)),
                ft.DataCell(ft.Text(e["event"], size=12)),
                ft.DataCell(ft.Text(json.dumps(e["details"], ensure_ascii=False)[:300], size=12)),
            ])
            for e in reversed(entries[-200:])
        ]
        self._refresh_tokens()
        if page_update:
            self.page.update()

    def _refresh_tokens(self):
        """Агрегация токенов в разрезе агентов.

        Источник — события response_received в LogStore (usage каждого ответа LLM:
        чат, генерация названия, суммаризация). «Запросов» — только обращения чата.
        """
        sessions = self.state.session_store.all()
        rows = []
        for a in self.state.config_store.all():
            responses = [e for e in self.state.log_store.filter(agent=a["name"])
                         if e["event"] == "response_received"]
            chat_responses = [e for e in responses
                              if e["details"].get("purpose") == "chat"]
            prompt = sum(int(e["details"].get("prompt_tokens") or 0) for e in responses)
            completion = sum(int(e["details"].get("completion_tokens") or 0) for e in responses)
            total = sum(int(e["details"].get("total_tokens") or 0) for e in responses)
            dialogs = sum(1 for s in sessions if s["agent_id"] == a["id"])
            avg = round(total / len(chat_responses)) if chat_responses else 0
            rows.append(ft.DataRow(cells=[
                ft.DataCell(ft.Text(a["name"], size=12)),
                ft.DataCell(ft.Text(str(len(chat_responses)), size=12)),
                ft.DataCell(ft.Text(str(prompt), size=12)),
                ft.DataCell(ft.Text(str(completion), size=12)),
                ft.DataCell(ft.Text(str(total), size=12)),
                ft.DataCell(ft.Text(str(dialogs), size=12)),
                ft.DataCell(ft.Text(str(avg), size=12)),
            ]))
        self.tokens_table.rows = rows

    async def _drain_logs(self):
        while True:
            await self._log_queue.get()
            try:
                self._refresh_logs()
                self.logs_stats.update()
                self.logs_table.update()
                self.tokens_table.update()
            except Exception:
                break  # страница закрыта/пересоздана

    def _snack(self, text: str):
        self.page.snack_bar = ft.SnackBar(ft.Text(text))
        self.page.snack_bar.open = True
        self.page.update()
