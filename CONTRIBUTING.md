# Разработка и проверка

Проверенное окружение интеграции: Linux, Python 3.14, HA 2026.10.4.
Версии тестовых зависимостей зафиксированы в `requirements-test.txt`.

```sh
python3.14 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-test.txt
ruff check custom_components tests
ruff format --check custom_components tests
python -X faulthandler -m pytest tests -q -p no:cacheprovider --tb=short --show-capture=no
```

Тесты используют вымышленные аккаунты и перехваченный транспорт; реальные
SMS и команды открытия не отправляются. Не добавляйте в них рабочие ключи,
полные журналы или обращения к реальному сервису.

Архив установки собирается из корня репозитория обычным Python 3:

```sh
python tools/build.py
```

В `dist/` появляются ZIP, TAR.GZ и `build-info.json` с SHA256. В архивы попадают
только файлы компонента; документация, тесты и Windows-помощник туда не входят.
Версия берётся из `custom_components/vezdehod/manifest.json`.

После отправки изменений GitHub Actions запускает HACS, Hassfest, Ruff и pytest.
Перед выпуском дождитесь успешного завершения всех проверок.

Для помощника требуется отдельная сборка на Windows:
[инструкция `tools/key-helper`](tools/key-helper/README.md#сборка-из-исходников).
Полные сторонние лицензии сохраняйте без изменений.
