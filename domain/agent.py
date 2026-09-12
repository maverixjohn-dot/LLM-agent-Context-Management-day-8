"""Слой domain: класс «Агент».

Инкапсулирует всю логику запроса/ответа к LLM:
- построение контекста (system prompt + суммаризация + история сессии);
- процедура логирования (отправка запроса / получение ответа / ошибки);
- суммаризация диалога при превышении порога;
- генерация названия диалога после первого сообщения пользователя.

UI не обращается к клиентам LLM напрямую — только через Agent.
"""

import threading
import time
from dataclasses import dataclass, field, asdict

from .llm_clients import DeepSeekClient, GigaChatClient, LLMError

# Пресеты моделей (администратор может отредактировать список в admin-странице)
PROVIDER_MODELS = {
    "deepseek": ["deepseek-v4-flash", "deepseek-v4-pro", "deepseek-v4-flash-vision-exp"],
    "gigachat": ["GigaChat-3-Ultra", "GigaChat-2-Max", "GigaChat-2-Pro", "GigaChat-2"],
}


@dataclass
class AgentConfig:
    """Конфигурация агента. Обязательные параметры выставляются администратором."""
    id: str = ""
    name: str = "Новый агент"
    provider: str = "deepseek"           # deepseek | gigachat
    model: str = "deepseek-v4-flash"
    temperature: float = 0.7
    max_tokens: int = 2048
    system_prompt: str = "Ты — полезный ассистент. Отвечай по-русски."
    # Настройки диалогов
    keep_session_history: bool = True    # LLM «помнит» предыдущие сообщения сессии
    keep_user_history: bool = True       # история диалогов сохраняется между запусками
    summarize_after: int = 20            # суммаризация после N сообщений (0 = выкл.)
    count_tokens: bool = True            # подсчёт токенов: запрос/ответ/диалог/сессия
    # Параметры запросов провайдеров
    timeout: int = 0                     # 0 = дефолт клиента (GigaChat: env GIGACHAT_TIMEOUT, 300 с)
    gigachat_scope: str = ""             # пусто = env GIGACHAT_SCOPE / GIGACHAT_API_PERS
    gigachat_verify_ssl: bool = False    # сертификаты Минцифры обычно не установлены -> False

    @classmethod
    def from_dict(cls, d: dict) -> "AgentConfig":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})

    def to_dict(self) -> dict:
        return asdict(self)


class Agent:
    """Пользовательский чат-агент поверх LLM API."""

    # Типы логируемых событий
    EVENT_REQUEST = "request_sent"        # отправка запроса в LLM
    EVENT_RESPONSE = "response_received"  # получение ответа от LLM
    EVENT_ERROR = "error"
    EVENT_SUMMARY = "summary_created"
    EVENT_TITLE = "title_generated"

    def __init__(self, config: AgentConfig, log_store, session_store):
        self.config = config
        self._log_store = log_store
        self._session_store = session_store
        self._client = self._build_client()
        self._lock = threading.Lock()
        # история при keep_user_history=False живёт только здесь, на диск не пишется
        self._memory_sessions: dict = {}

    # ---------- процедура логирования ----------

    def log(self, event: str, details: dict) -> None:
        """Процедура логирования действий агента."""
        self._log_store.add(self.config.name, event, details)

    # ---------- внутреннее ----------

    def _build_client(self):
        timeout = self.config.timeout or None
        if self.config.provider == "deepseek":
            return DeepSeekClient(timeout=timeout or 120)
        if self.config.provider == "gigachat":
            return GigaChatClient(
                scope=self.config.gigachat_scope or None,
                timeout=timeout,
                verify_ssl=self.config.gigachat_verify_ssl,
            )
        raise LLMError(f"Неизвестный провайдер: {self.config.provider}")

    def _build_messages(self, session: dict, user_text: str) -> list:
        messages = []
        if self.config.system_prompt:
            messages.append({"role": "system", "content": self.config.system_prompt})
        if session.get("summary"):
            messages.append({
                "role": "system",
                "content": "Краткое содержание предыдущей части диалога: " + session["summary"],
            })
        if self.config.keep_session_history:
            messages.extend(
                {"role": m["role"], "content": m["content"]} for m in session["messages"]
            )
        messages.append({"role": "user", "content": user_text})
        return messages

    @staticmethod
    def _normalize_usage(usage: dict) -> dict:
        """Привести usage ответа LLM к числам (GigaChat/DeepSeek могут не вернуть поле)."""
        prompt = int(usage.get("prompt_tokens") or 0)
        completion = int(usage.get("completion_tokens") or 0)
        total = int(usage.get("total_tokens") or 0) or prompt + completion
        return {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": total}

    def _accumulate_tokens(self, session: dict, usage: dict, is_chat_request: bool) -> None:
        """Накопить статистику токенов диалога, если подсчёт включён администратором."""
        if not self.config.count_tokens:
            return
        stats = session.setdefault("token_stats", {
            "requests": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0})
        if is_chat_request:
            stats["requests"] += 1
            session["last_usage"] = dict(usage)
        stats["prompt_tokens"] += usage["prompt_tokens"]
        stats["completion_tokens"] += usage["completion_tokens"]
        stats["total_tokens"] += usage["total_tokens"]

    def _call_llm(self, messages: list, purpose: str) -> tuple:
        """Единая точка обращения к LLM с логированием запроса и ответа.

        Возвращает (текст_ответа, usage).
        """
        payload_chars = sum(len(m["content"]) for m in messages)
        self.log(self.EVENT_REQUEST, {
            "purpose": purpose,
            "provider": self.config.provider,
            "model": self.config.model,
            "messages_count": len(messages),
            "payload_chars": payload_chars,
        })
        started = time.monotonic()
        try:
            result = self._client.chat(
                model=self.config.model,
                messages=messages,
                temperature=self.config.temperature,
                max_tokens=self.config.max_tokens or None,
            )
        except LLMError as exc:
            self.log(self.EVENT_ERROR, {"purpose": purpose, "error": str(exc)})
            raise
        duration = round(time.monotonic() - started, 2)
        usage = result.get("usage") or {}
        self.log(self.EVENT_RESPONSE, {
            "purpose": purpose,
            "provider": self.config.provider,
            "model": self.config.model,
            "duration_sec": duration,
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "total_tokens": usage.get("total_tokens"),
            "answer_preview": result["content"][:200],
        })
        return result["content"], self._normalize_usage(result.get("usage") or {})

    def _save_session(self, session: dict) -> None:
        """Персистить сессию или держать её только в памяти (keep_user_history=False)."""
        if self.config.keep_user_history:
            self._session_store.save(session)
        else:
            # история не персистится — храним только в памяти текущего процесса
            self._memory_sessions[session["id"]] = session

    # ---------- публичное API для UI ----------

    def get_session(self, session_id: str) -> dict | None:
        """Сессия с актуальной историей: сначала память, потом диск."""
        return self._memory_sessions.get(session_id) or self._session_store.get(session_id)

    def chat(self, session_id: str, user_text: str) -> str:
        """Отправить сообщение пользователя, вернуть ответ LLM.

        Вся история сессии (+ суммаризация) отправляется в LLM,
        если администратор включил keep_session_history.
        """
        with self._lock:
            session = self._memory_sessions.get(session_id) or self._session_store.get(session_id)
            if session is None:
                raise LLMError(f"Сессия {session_id} не найдена")
            messages = self._build_messages(session, user_text)
            answer, usage = self._call_llm(messages, purpose="chat")
            session["messages"].append({"role": "user", "content": user_text})
            session["messages"].append({"role": "assistant", "content": answer})
            self._accumulate_tokens(session, usage, is_chat_request=True)
            self._save_session(session)
            if (self.config.summarize_after
                    and len(session["messages"]) >= self.config.summarize_after):
                self._summarize(session)
            return answer

    def generate_title(self, session_id: str) -> str:
        """Название диалога придумывает LLM по первому сообщению пользователя.

        Вызывается один раз — пока у сессии стоит дефолтное название.
        """
        session = self._memory_sessions.get(session_id) or self._session_store.get(session_id)
        if session["title"] != "Новый диалог":
            return session["title"]  # название уже установлено — не меняем
        first_user = next((m["content"] for m in session["messages"] if m["role"] == "user"), "")
        prompt = (
            "Придумай короткое название (3–6 слов, без кавычек) для диалога, "
            f"который начинается с сообщения: «{first_user[:500]}». Верни только название."
        )
        try:
            title, usage = self._call_llm([{"role": "user", "content": prompt}], purpose="title")
            self._accumulate_tokens(session, usage, is_chat_request=False)
            title = title.strip().strip('"«»')[:80] or first_user[:40]
        except LLMError:
            title = first_user[:40] or "Диалог"
        session["title"] = title
        if self.config.keep_user_history:
            self._session_store.save(session)
        else:
            # в неперсистентном режиме на диск пишем только метаданные (без сообщений)
            stored = self._session_store.get(session_id)
            if stored is not None:
                stored["title"] = title
                self._session_store.save(stored)
            self._memory_sessions[session_id] = session
        self.log(self.EVENT_TITLE, {"session_id": session_id, "title": title})
        return title

    def rename_session(self, session_id: str, new_title: str) -> None:
        """Пользователь может сам сменить тему (название) диалога."""
        session = self._session_store.get(session_id)
        if session:
            session["title"] = new_title.strip()[:80] or session["title"]
            self._session_store.save(session)

    # ---------- суммаризация ----------

    def _summarize(self, session: dict) -> None:
        """Сжать старую часть истории в summary, оставить последние реплики."""
        keep_tail = 4  # последние сообщения оставляем дословно
        old = session["messages"][:-keep_tail]
        if not old:
            return
        transcript = "\n".join(f"{m['role']}: {m['content']}" for m in old)
        if session.get("summary"):
            transcript = "Предыдущее резюме: " + session["summary"] + "\n" + transcript
        prompt = (
            "Сожми следующий фрагмент диалога в краткое резюме (до 200 слов), "
            "сохрани факты, решения и контекст, важные для продолжения:\n\n" + transcript
        )
        try:
            summary, usage = self._call_llm([{"role": "user", "content": prompt}], purpose="summarize")
        except LLMError:
            return  # суммаризация не критична — диалог продолжается без неё
        self._accumulate_tokens(session, usage, is_chat_request=False)
        session["summary"] = summary
        session["messages"] = session["messages"][-keep_tail:]
        self._save_session(session)
        self.log(self.EVENT_SUMMARY, {
            "session_id": session["id"],
            "summary_chars": len(summary),
        })
