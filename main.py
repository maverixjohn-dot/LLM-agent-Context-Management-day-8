"""Точка входа. Запуск в веб-режиме: python main.py -> http://localhost:8550
Страница пользователя: /        Страница администратора: /admin
"""

import flet as ft

from ui.app import main

if __name__ == "__main__":
    ft.app(target=main, view=ft.AppView.WEB_BROWSER, port=8550)
