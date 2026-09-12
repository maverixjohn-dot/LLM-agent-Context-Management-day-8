"""Слой data: персистентное хранение конфигураций агентов, сессий диалогов и логов.

Всё хранится в JSON/JSONL-файлах в каталоге data_files рядом с проектом.
Потокобезопасно (LLM-вызовы выполняются в рабочих потоках).
"""

import json
import os
import threading
import uuid
from collections import deque
from datetime import datetime

BASE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data_files")
os.makedirs(BASE_DIR, exist_ok=True)


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


class _JsonFile:
    """Базовый потокобезопасный доступ к JSON-файлу."""

    def __init__(self, filename: str, default):
        self._path = os.path.join(BASE_DIR, filename)
        self._lock = threading.Lock()
        if not os.path.exists(self._path):
            self._write(default)

    def _read(self):
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, FileNotFoundError):
            return {}

    def _write(self, data) -> None:
        tmp = self._path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self._path)


class AgentConfigStore(_JsonFile):
    """Конфигурации агентов, заданные администратором."""

    def __init__(self):
        super().__init__("agents.json", {})

    def all(self) -> list:
        with self._lock:
            return list(self._read().values())

    def get(self, agent_id: str) -> dict | None:
        with self._lock:
            return self._read().get(agent_id)

    def save(self, config: dict) -> dict:
        with self._lock:
            data = self._read()
            if not config.get("id"):
                config["id"] = _new_id()
            data[config["id"]] = config
            self._write(data)
            return config

    def delete(self, agent_id: str) -> None:
        with self._lock:
            data = self._read()
            data.pop(agent_id, None)
            self._write(data)


class SessionStore(_JsonFile):
    """Сессии диалогов пользователей (история сообщений, название, суммаризация)."""

    def __init__(self):
        super().__init__("sessions.json", {})

    def all(self) -> list:
        with self._lock:
            return sorted(self._read().values(), key=lambda s: s.get("created_at", ""))

    def get(self, session_id: str) -> dict | None:
        with self._lock:
            return self._read().get(session_id)

    def create(self, agent_id: str) -> dict:
        session = {
            "id": _new_id(),
            "agent_id": agent_id,
            "title": "Новый диалог",
            "created_at": _now(),
            "messages": [],
            "summary": "",
        }
        with self._lock:
            data = self._read()
            data[session["id"]] = session
            self._write(data)
        return session

    def save(self, session: dict) -> None:
        with self._lock:
            data = self._read()
            data[session["id"]] = session
            self._write(data)

    def delete(self, session_id: str) -> None:
        with self._lock:
            data = self._read()
            data.pop(session_id, None)
            self._write(data)


class LogStore:
    """Журнал событий класса «Агент»: отправка запроса, получение ответа, ошибки.

    Хранит ограниченный буфер в памяти + дублирует в logs.jsonl.
    Поддерживает подписку — UI получает события в реальном времени.
    """

    MAX_ENTRIES = 2000

    def __init__(self, filename: str = "logs.jsonl"):
        self._path = os.path.join(BASE_DIR, filename)
        self._entries: deque = deque(maxlen=self.MAX_ENTRIES)
        self._listeners: list = []
        self._lock = threading.Lock()
        if os.path.exists(self._path):
            with open(self._path, "r", encoding="utf-8") as f:
                for line in f.readlines()[-self.MAX_ENTRIES:]:
                    line = line.strip()
                    if line:
                        try:
                            self._entries.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue

    def subscribe(self, callback) -> None:
        with self._lock:
            self._listeners.append(callback)

    def add(self, agent_name: str, event: str, details: dict) -> dict:
        entry = {
            "ts": _now(),
            "agent": agent_name,
            "event": event,
            "details": details,
        }
        with self._lock:
            self._entries.append(entry)
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            listeners = list(self._listeners)
        for cb in listeners:
            try:
                cb(entry)
            except Exception:
                pass  # слушатель не должен ронять логирование
        return entry

    def filter(self, agent: str | None = None, event: str | None = None) -> list:
        with self._lock:
            entries = list(self._entries)
        if agent:
            entries = [e for e in entries if e["agent"] == agent]
        if event:
            entries = [e for e in entries if e["event"] == event]
        return entries

    def stats(self) -> dict:
        with self._lock:
            entries = list(self._entries)
        by_event: dict = {}
        for e in entries:
            by_event[e["event"]] = by_event.get(e["event"], 0) + 1
        return {"total": len(entries), "by_event": by_event}
