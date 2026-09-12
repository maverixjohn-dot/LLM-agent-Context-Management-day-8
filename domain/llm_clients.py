"""Слой domain: низкоуровневые клиенты LLM API.

DeepSeek — OpenAI-совместимый Chat Completions (base_url https://api.deepseek.com).
GigaChat — OAuth 2.0 (client credentials) + Chat Completions
(endpoint https://api.giga.chat/v1/chat/completions).

Клиенты только выполняют HTTP-вызовы. Логирование, история и суммаризация —
зона ответственности класса Agent (agent.py).
"""

import os
import time
import uuid

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class LLMError(Exception):
    """Ошибка обращения к LLM API."""


class DeepSeekClient:
    """DeepSeek API. Ключ: переменная окружения DEEPSEEK_API_KEY."""

    BASE_URL = "https://api.deepseek.com"

    def __init__(self, api_key: str | None = None, timeout: int = 120):
        self.api_key = api_key or os.environ.get("DEEPSEEK_API_KEY", "")
        if not self.api_key:
            raise LLMError("Не задана переменная окружения DEEPSEEK_API_KEY")
        self.timeout = timeout

    def chat(self, model: str, messages: list, temperature: float = 0.7,
             max_tokens: int | None = None) -> dict:
        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "stream": False,
        }
        if max_tokens:
            payload["max_tokens"] = max_tokens
        try:
            resp = requests.post(
                f"{self.BASE_URL}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise LLMError(f"DeepSeek: сетевая ошибка: {exc}") from exc
        if resp.status_code != 200:
            raise LLMError(f"DeepSeek: HTTP {resp.status_code}: {resp.text[:500]}")
        data = resp.json()
        try:
            return {
                "content": data["choices"][0]["message"]["content"],
                "usage": data.get("usage", {}),
            }
        except (KeyError, IndexError) as exc:
            raise LLMError(f"DeepSeek: неожиданный формат ответа: {str(data)[:500]}") from exc


class GigaChatClient:
    """GigaChat API (Сбер).

    Авторизация: GIGACHAT_CREDENTIALS (Base64 client credentials) ->
    OAuth-токен на ngw.devices.sberbank.ru. Токен кешируется до истечения.
    Генерация: https://api.giga.chat/v1/chat/completions.
    """

    OAUTH_URL = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
    API_URL = "https://api.giga.chat/v1/chat/completions"

    def __init__(self, credentials: str | None = None, scope: str | None = None,
                 timeout: int | None = None, verify_ssl: bool = False):
        self.credentials = credentials or os.environ.get("GIGACHAT_CREDENTIALS", "")
        if not self.credentials:
            raise LLMError("Не задана переменная окружения GIGACHAT_CREDENTIALS")
        # Обязательные параметры конфигурации: дефолты из переменных окружения
        self.scope = scope or os.environ.get("GIGACHAT_SCOPE", "GIGACHAT_API_PERS")
        self.timeout = timeout or int(os.environ.get("GIGACHAT_TIMEOUT", "300"))
        self.verify_ssl = verify_ssl
        self._token: str | None = None
        self._token_expires_at: float = 0.0

    def _get_token(self) -> str:
        # expires_at у GigaChat в миллисекундах; обновляем с запасом 30 с
        if self._token and time.time() < self._token_expires_at - 30:
            return self._token
        try:
            resp = requests.post(
                self.OAUTH_URL,
                headers={
                    "Authorization": f"Basic {self.credentials}",
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json",
                    "RqUID": uuid.uuid4().hex,
                },
                data={"scope": self.scope},
                timeout=self.timeout,
                verify=self.verify_ssl,
            )
        except requests.RequestException as exc:
            raise LLMError(f"GigaChat: ошибка авторизации: {exc}") from exc
        if resp.status_code != 200:
            raise LLMError(f"GigaChat OAuth: HTTP {resp.status_code}: {resp.text[:500]}")
        data = resp.json()
        self._token = data["access_token"]
        self._token_expires_at = data.get("expires_at", 0) / 1000.0
        return self._token

    def chat(self, model: str, messages: list, temperature: float = 0.7,
             max_tokens: int | None = None) -> dict:
        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "stream": False,
        }
        if max_tokens:
            payload["max_tokens"] = max_tokens
        try:
            resp = requests.post(
                self.API_URL,
                headers={
                    "Authorization": f"Bearer {self._get_token()}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                json=payload,
                timeout=self.timeout,
                verify=self.verify_ssl,
            )
        except requests.RequestException as exc:
            raise LLMError(f"GigaChat: сетевая ошибка: {exc}") from exc
        if resp.status_code != 200:
            raise LLMError(f"GigaChat: HTTP {resp.status_code}: {resp.text[:500]}")
        data = resp.json()
        try:
            return {
                "content": data["choices"][0]["message"]["content"],
                "usage": data.get("usage", {}),
            }
        except (KeyError, IndexError) as exc:
            raise LLMError(f"GigaChat: неожиданный формат ответа: {str(data)[:500]}") from exc
