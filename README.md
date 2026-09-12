# LLM Chat — пользовательский чат с классом «Агент»

Веб-приложение на Flet: чат пользователя с LLM (DeepSeek / GigaChat),
страница администратора с конфигурацией агентов и отчётностью по логам.

## Структура (слои)

```
llm_chat_app/
├── main.py                 # точка входа, веб-режим Flet (порт 8550)
├── requirements.txt
├── data/                   # слой data: персистентность (JSON/JSONL в data_files/)
│   └── storage.py          # AgentConfigStore, SessionStore, LogStore
├── domain/                 # слой domain: бизнес-логика
│   ├── llm_clients.py      # DeepSeekClient, GigaChatClient (только HTTP)
│   └── agent.py            # класс Agent: запрос/ответ, логирование, суммаризация
└── ui/                     # слой ui: Flet
    ├── app.py              # оболочка, маршрутизация / и /admin
    ├── user_page.py        # страница пользователя
    └── admin_page.py       # страница администратора
```

## Установка и запуск

```bash
pip install -r requirements.txt

export DEEPSEEK_API_KEY="sk-..."
export GIGACHAT_CREDENTIALS="base64-credentials..."
# необязательно (есть дефолты):
export GIGACHAT_TIMEOUT=300
export GIGACHAT_SCOPE=GIGACHAT_API_PERS

python main.py
```

Открыть: http://localhost:8550 — чат; http://localhost:8550/admin — администрирование.

## Класс «Агент» (domain/agent.py)

- Обращается к LLM по API: DeepSeek (`https://api.deepseek.com`, модели
  `deepseek-v4-flash`, `deepseek-v4-pro`, `deepseek-v4-flash-vision-exp`) и GigaChat
  (OAuth на `ngw.devices.sberbank.ru`, генерация `https://api.giga.chat/v1/chat/completions`).
- Процедура логирования `Agent.log()` пишет события: `request_sent`
  (отправка запроса в LLM), `response_received` (получение ответа: длительность,
  токены, превью), `error`, `summary_created`, `title_generated`. Логи — в
  `LogStore` (память + `data_files/logs.jsonl`). Просмотр логов — на странице
  администрирования: таблица с фильтрами, статистика и живое обновление.
- Логика запроса/ответа полностью внутри агента: построение контекста
  (system prompt + summary + история сессии), вызов клиента, обновление истории.
- Суммаризация: после `summarize_after` сообщений старая часть диалога сжимается
  LLM в резюме и подаётся в контекст дальше (история не растёт бесконечно).
- Название диалога после первого запроса пользователя генерирует LLM
  (`Agent.generate_title`); пользователь может переименовать тему вручную.

## Конфигурация агента (задаёт администратор на /admin)

| Параметр | Назначение |
|---|---|
| provider / model | DeepSeek или GigaChat, модель из пресета |
| temperature, max_tokens | параметры генерации |
| timeout | таймаут запроса; для GigaChat 0 = env `GIGACHAT_TIMEOUT` (дефолт 300 с) |
| gigachat_scope | пусто = env `GIGACHAT_SCOPE` (дефолт `GIGACHAT_API_PERS`) |
| gigachat_verify_ssl | проверка TLS; обычно выкл., т.к. нужны сертификаты Минцифры |
| keep_session_history | LLM «помнит» предыдущие сообщения сессии |
| keep_user_history | сохранять диалоги на диск (иначе — только в памяти процесса) |
| summarize_after | порог суммаризации (0 = выключена) |
| count_tokens | подсчёт токенов: текущий запрос/ответ, весь диалог, сессия |

## Подсчёт токенов

При включённом `count_tokens` агент накапливает usage каждого ответа LLM
(чат, генерация названия, суммаризация). На странице пользователя под полем
«Запрос» показываются токены текущего запроса/ответа и суммарно по диалогу.
На странице администрирования таблица «Токены по агентам» собирает показатели
в разрезе агентов: число запросов чата, входящие/ответные/всего токенов,
число диалогов, среднее токенов на запрос. Источник агрегации — события
`response_received` в буфере логов (2000 последних записей).

## Примечания

- Без заданных ключей API создание агента в UI не упадёт — ошибка появится
  при первой отправке запроса и залогируется событием `error`.
- Данные (конфиги, сессии, логи) лежат в `data_files/` рядом с проектом.
- Модель GigaChat по умолчанию — `GigaChat-3-Ultra` (Freemium для физлиц);
  при платном тарифе доступны `GigaChat-2-Max/Pro/2` — выбираются в админке.
