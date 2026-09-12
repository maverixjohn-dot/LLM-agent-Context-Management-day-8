"""Слой ui: страница пользователя.

Опции пользователя: выбор агента и старт диалога. Диалогов может быть несколько,
название диалогу после первого запроса один раз присваивает LLM
(можно переименовать вручную). Логи класса «Агент» — на странице
администрирования (/admin).
"""

import asyncio

import flet as ft

from domain.llm_clients import LLMError

DEFAULT_TITLE = "Новый диалог"


class UserPage:
    def __init__(self, page: ft.Page, state):
        self.page = page
        self.state = state
        self.current_session_id: str | None = None

    # ---------- построение ----------

    def build(self):
        agents = self.state.config_store.all()

        self.agent_dropdown = ft.Dropdown(
            label="Агент",
            width=260,
            options=[ft.dropdown.Option(a["id"], a["name"]) for a in agents],
            value=agents[0]["id"] if agents else None,
        )
        self.dialogs_list = ft.ListView(expand=True, spacing=4)

        self.chat_list = ft.ListView(expand=True, spacing=8, auto_scroll=True)
        self.session_title = ft.Text("Выберите или создайте диалог", size=16,
                                     weight=ft.FontWeight.BOLD, expand=True)
        self.request_field = ft.TextField(
            label="Запрос",
            hint_text="Введите запрос и нажмите Enter",
            expand=True,
            on_submit=self.on_send,
        )
        self.send_button = ft.ElevatedButton("Отправить", icon=ft.icons.SEND, on_click=self.on_send)
        self.token_status = ft.Text("", size=12, color=ft.colors.BLUE_GREY_600)

        self._refresh_dialogs()

        header = ft.Row(
            [
                ft.Text("LLM Chat", size=20, weight=ft.FontWeight.BOLD),
                ft.Container(expand=True),
                ft.TextButton("Администрирование", icon=ft.icons.SETTINGS,
                              on_click=lambda e: self.page.go("/admin")),
            ]
        )

        left_col = ft.Container(
            width=280,
            content=ft.Column(
                [
                    self.agent_dropdown,
                    ft.ElevatedButton("Новый диалог", icon=ft.icons.ADD, on_click=self.on_new_dialog),
                    ft.Divider(),
                    ft.Text("Диалоги", weight=ft.FontWeight.BOLD),
                    ft.Container(self.dialogs_list, expand=True),
                ],
                expand=True,
            ),
        )

        center_col = ft.Container(
            expand=True,
            content=ft.Column(
                [
                    ft.Row(
                        [
                            self.session_title,
                            ft.IconButton(icon=ft.icons.EDIT, tooltip="Переименовать тему",
                                          on_click=self.on_rename),
                        ],
                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    ),
                    ft.Container(self.chat_list, expand=True, border=ft.border.all(1, ft.colors.BLACK12),
                                 border_radius=8, padding=10),
                    ft.Row([self.request_field, self.send_button]),
                    self.token_status,
                ],
                expand=True,
            ),
        )

        self.page.add(
            ft.Column([header, ft.Divider(),
                       ft.Row([left_col, ft.VerticalDivider(width=1), center_col],
                              expand=True, vertical_alignment=ft.CrossAxisAlignment.START)],
                      expand=True)
        )

    # ---------- диалоги ----------

    def _refresh_dialogs(self):
        self.dialogs_list.controls.clear()
        for s in self.state.session_store.all():
            sid = s["id"]
            self.dialogs_list.controls.append(
                ft.Container(
                    bgcolor=ft.colors.BLUE_GREY_50 if sid == self.current_session_id else None,
                    border_radius=6,
                    padding=6,
                    content=ft.Row(
                        [
                            ft.Text(s["title"], expand=True, size=13, max_lines=1,
                                    overflow=ft.TextOverflow.ELLIPSIS),
                            ft.IconButton(icon=ft.icons.DELETE_OUTLINE, icon_size=16,
                                          tooltip="Удалить диалог",
                                          on_click=lambda e, x=sid: self.on_delete_dialog(x)),
                        ],
                        spacing=0,
                    ),
                    on_click=lambda e, x=sid: self.on_select_dialog(x),
                )
            )

    def _render_messages(self, session: dict):
        self.chat_list.controls.clear()
        for m in session["messages"]:
            self._append_bubble(m["role"], m["content"])

    def _append_bubble(self, role: str, text: str):
        """Пузырь сообщения с ограниченной шириной: текст переносится построчно."""
        is_user = role == "user"
        bubble = ft.Container(
            bgcolor=ft.colors.BLUE_50 if is_user else ft.colors.GREEN_50,
            border_radius=10,
            padding=10,
            expand=3,  # ограничение ширины -> Text переносит строки
            content=ft.Text(("Вы: " if is_user else "LLM: ") + text,
                            selectable=True, size=13),
        )
        spacer = ft.Container(expand=1)
        self.chat_list.controls.append(
            ft.Row(
                [spacer, bubble] if is_user else [bubble, spacer],
                vertical_alignment=ft.CrossAxisAlignment.START,
            )
        )

    def _update_token_status(self, session: dict | None):
        """Строка подсчёта токенов: текущий запрос/ответ и суммарно по диалогу."""
        stats = (session or {}).get("token_stats") or {}
        if not stats:
            self.token_status.value = ""
            return
        last = (session or {}).get("last_usage") or {}
        self.token_status.value = (
            f"Токены — запрос: {last.get('prompt_tokens', 0)} вх. / "
            f"{last.get('completion_tokens', 0)} отв.  ·  "
            f"диалог: {stats.get('total_tokens', 0)} "
            f"(запросов: {stats.get('requests', 0)}, "
            f"вх.: {stats.get('prompt_tokens', 0)}, отв.: {stats.get('completion_tokens', 0)})"
        )

    def on_new_dialog(self, e):
        agent_id = self.agent_dropdown.value
        if not agent_id:
            self._snack("Сначала выберите агента (настраивается на странице администратора)")
            return
        session = self.state.session_store.create(agent_id)
        self.current_session_id = session["id"]
        self.session_title.value = session["title"]
        self.chat_list.controls.clear()
        self._update_token_status(session)
        self._refresh_dialogs()
        self.page.update()

    def _load_session(self, session_id: str) -> dict | None:
        """Сессия с полной историей.

        При keep_user_history=False сообщения живут только в памяти агента,
        поэтому спрашиваем сессию у него; диск — запасной вариант.
        """
        session = self.state.session_store.get(session_id)
        if session is None:
            return None
        try:
            agent = self.state.get_agent(session["agent_id"])
        except (LLMError, ValueError):
            return session  # без ключей API показываем то, что есть на диске
        return agent.get_session(session_id) or session

    def on_select_dialog(self, session_id: str):
        self.current_session_id = session_id
        session = self._load_session(session_id)
        if session is None:
            self._snack("Сессия не найдена")
            return
        self.session_title.value = session["title"]
        self._render_messages(session)
        self._update_token_status(session)
        self._refresh_dialogs()
        self.page.update()

    def on_delete_dialog(self, session_id: str):
        self.state.session_store.delete(session_id)
        if self.current_session_id == session_id:
            self.current_session_id = None
            self.chat_list.controls.clear()
            self.session_title.value = "Выберите или создайте диалог"
        self._refresh_dialogs()
        self.page.update()

    # ---------- отправка запроса ----------

    async def on_send(self, e):
        text = (self.request_field.value or "").strip()
        if not text:
            return
        if not self.current_session_id:
            self._snack("Создайте диалог кнопкой «Новый диалог»")
            return
        session = self.state.session_store.get(self.current_session_id)
        if session is None:
            self._snack("Сессия не найдена")
            return
        try:
            agent = self.state.get_agent(session["agent_id"])
        except (LLMError, ValueError) as exc:
            self._snack(str(exc))
            return

        self.send_button.disabled = True
        self.request_field.value = ""
        self._append_bubble("user", text)
        self.page.update()

        try:
            answer = await asyncio.to_thread(agent.chat, self.current_session_id, text)
        except LLMError as exc:
            self._append_bubble("assistant", f"[Ошибка LLM] {exc}")
            self.send_button.disabled = False
            self.page.update()
            return
        self._append_bubble("assistant", answer)

        # название диалога генерируется LLM один раз — после первого запроса
        stored = self.state.session_store.get(self.current_session_id)
        if stored["title"] == DEFAULT_TITLE:
            title = await asyncio.to_thread(agent.generate_title, self.current_session_id)
            self.session_title.value = title
            self._refresh_dialogs()
        # токены: берём сессию с актуальной историей (память приватного режима)
        self._update_token_status(agent.get_session(self.current_session_id) or stored)

        self.send_button.disabled = False
        self.page.update()

    # ---------- переименование ----------

    def on_rename(self, e):
        if not self.current_session_id:
            return
        field = ft.TextField(label="Название диалога", value=self.session_title.value, autofocus=True)

        def save(ev):
            new_title = (field.value or "").strip()
            if new_title:
                session = self.state.session_store.get(self.current_session_id)
                session["title"] = new_title[:80]
                self.state.session_store.save(session)
                self.session_title.value = session["title"]
                self._refresh_dialogs()
            dlg.open = False
            self.page.update()

        dlg = ft.AlertDialog(
            title=ft.Text("Сменить тему диалога"),
            content=field,
            actions=[ft.TextButton("Сохранить", on_click=save),
                     ft.TextButton("Отмена", on_click=lambda ev: self._close(dlg))],
        )
        self.page.dialog = dlg
        dlg.open = True
        self.page.update()

    def _close(self, dlg):
        dlg.open = False
        self.page.update()

    def _snack(self, text: str):
        self.page.snack_bar = ft.SnackBar(ft.Text(text))
        self.page.snack_bar.open = True
        self.page.update()
