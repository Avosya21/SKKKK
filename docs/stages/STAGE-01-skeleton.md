# Этап 1. Каркас портала

## Цель

Работающее веб-приложение с входом по трём паролям и проектами. GPU не задействован. На выходе — то, что уже можно показать команде.

## Предусловия

Этап 0 зелёный.

## Шаги

1. **Виртуальное окружение и зависимости.**

```powershell
cd C:\1C_NEW_SKKKK
python -m venv venv
venv\Scripts\activate
pip install fastapi "uvicorn[standard]" sqlalchemy alembic "python-jose[cryptography]" "passlib[bcrypt]" python-multipart aiofiles httpx pydantic-settings jinja2 pytest pytest-asyncio
pip freeze > app\requirements.txt
```

2. **Конфигурация** `app/app/config.py`: `pydantic-settings`, чтение `.env`, обязательные поля падают при старте с внятной ошибкой (не с `KeyError` посреди запроса).

3. **Хеши паролей.** Скрипт `scripts/make_hashes.py`: принимает три пароля, печатает bcrypt-хеши для `.env`. Пароли в открытом виде нигде не сохраняются.

4. **Модели SQLAlchemy** `app/app/models/`: `users`, `projects`, `artifacts`, `jobs` (остальные таблицы добавляются на своих этапах, чтобы миграции были осмысленными).

5. **Alembic**: инициализация, первая миграция, применение.

6. **Аутентификация** `app/app/routers/auth.py` и `app/app/dependencies.py`:
   - `POST /auth/login` принимает `name`, `telegram_id`, `password`;
   - существующий пользователь — сверка с его `password_hash`;
   - новый — сверка со всеми тремя хешами из `.env`, роль по совпавшему;
   - неподошедший пароль — 401, без подсказки, какая роль не сошлась;
   - JWT с `sub`, `role`, `exp`;
   - `get_current_user(token)` и `require_role(*roles)`.

7. **Проекты** `app/app/routers/projects.py`: список (все), создание (`admin`, `manager`), детали (все), архивация (`admin`). При создании — дерево каталогов проекта на диске одной функцией `services/storage.py: create_project_tree()`.

8. **Шаблоны** `app/app/templates/`: `login.html`, `index.html` (список проектов), `project.html` (заглушка списка файлов). Bootstrap 5, русский интерфейс.

9. **Точка входа** `app/app/main.py`: роутеры, статика, обработчики ошибок, старт логирования в `data/logs/api.log`.

## Что появляется на диске

```
C:\1C_NEW_SKKKK\
├── venv\
├── app\
│   ├── requirements.txt
│   ├── alembic.ini
│   ├── alembic\versions\
│   └── app\
│       ├── config.py  dependencies.py  main.py
│       ├── models\  schemas\  routers\  services\  templates\  static\
│       └── tests\
├── scripts\make_hashes.py
└── data\
    ├── app.db
    └── logs\api.log
```

## Полезные проекты на GitHub

Не тащить целиком — стек уже зафиксирован в [DECISIONS.md](../DECISIONS.md) (SQLite, без Redis/Celery). Смотреть на структуру и решения по auth, не копировать зависимости пачкой.

| Проект | Что даёт | Как используем |
|---|---|---|
| [benavlabs/FastAPI-boilerplate](https://github.com/benavlabs/FastAPI-boilerplate) | Зрелая структура `app/{models,schemas,routers,services}`, вынесенная конфигурация на `pydantic-settings`, обработчики ошибок | Ориентир для раскладки `app/app/` — берём структуру папок, не Redis/кэш из него |
| [justyn-clark/fastapi-sqlalchemy-starter](https://github.com/justyn-clark/fastapi-sqlalchemy-starter) | Минимальный пример FastAPI + async SQLAlchemy 2 + Alembic + JWT, с CI на pytest | Референс для `alembic/env.py` и порядка первой миграции — у нас проще (SQLite, не Postgres) |
| [jpadilla/pyjwt](https://github.com/jpadilla/pyjwt) | Альтернатива `python-jose` для выпуска/проверки JWT, меньше поверхность CVE | Если `python-jose` даст проблемы с зависимостями на Windows — прямая замена в `dependencies.py`, контракт токена не меняется |
| [zhanymkanov/fastapi-best-practices](https://github.com/zhanymkanov/fastapi-best-practices) | Не библиотека, а чек-лист антипаттернов FastAPI (что не класть в роутер, как валидировать) | Прогнать по этому списку `routers/auth.py` и `routers/projects.py` перед закрытием этапа |

## Проверки

**Открыть страницу и сравнить со скриншотом.** После запуска uvicorn (см. ниже) открыть `http://127.0.0.1:1111/login` в браузере, сделать скриншот формы логина и страницы списка проектов после входа — сверить с тем, что описано в цели этапа («форма из трёх полей», «список проектов, роль в шапке»). Расхождение по UI (не тексту таблицы) — повод доделать шаблоны, а не «доделаем в этапе 8».

Запуск:

```powershell
cd C:\1C_NEW_SKKKK
venv\Scripts\activate
python -m app.app.main api --host 127.0.0.1 --port 1111 --reload
```

| Проверка | Действие | Ожидание |
|---|---|---|
| Старт | Открыть `http://127.0.0.1:1111/login` | Форма из трёх полей |
| Вход админом | Имя, Telegram ID, пароль admin | Редирект на список проектов, в шапке роль |
| Повторный вход | То же **имя**, любой Telegram ID, тот же пароль | Вход, новый пользователь **не** создан, Telegram ID обновлён |
| Другой пароль роли | То же имя, пароль manager | 200, роль в БД стала manager |
| Создание проекта менеджером | Форма нового проекта | Проект создан, на диске появилось дерево `data\projects\{id}\raw_inputs\calls` и остальные |
| Создание проекта сотрудником | Тот же запрос под `user` | 403 |
| Удаление проекта менеджером | `DELETE /projects/{id}` | 403 |
| Без токена | `GET /projects` без заголовка | 401 |
| Битый токен | Подменить подпись | 401 |

## Тесты

`app/app/tests/test_auth.py`

- `test_login_creates_user_with_admin_role` — новое **имя** + admin-пароль → пользователь с ролью `admin`.
- `test_login_existing_user_wrong_password_401` — существующий ID, другой пароль → 401.
- `test_login_existing_user_takes_role_from_password` — пользователь `user` вводит пароль `admin` → 200, роль стала admin.
- `test_token_contains_role_and_expiry`.
- `test_expired_token_rejected` — токен с истёкшим `exp` → 401.
- `test_tampered_token_rejected`.

`app/app/tests/test_projects.py`

- `test_manager_can_create_project`
- `test_user_cannot_create_project_403`
- `test_user_cannot_delete_project`
- `test_manager_purge_removes_files_and_db`
- `test_admin_can_delete_project`
- `test_create_project_makes_directory_tree` — проверяются все шесть подкаталогов.
- `test_project_name_with_path_chars_is_sanitized` — имя `../../etc` не создаёт каталог вне `data\projects`.
- `test_list_projects_visible_to_all_roles`

`app/app/tests/conftest.py` — фикстуры: временная БД SQLite в памяти, временный `DATA_DIR` в `tmp_path`, три токена (admin/manager/user), `TestClient`.

Запуск:

```powershell
cd C:\1C_NEW_SKKKK
venv\Scripts\activate
pytest app -m "not gpu" -q
```

## Критерий готовности

- [ ] Три пароля дают три разные роли, эскалация невозможна
- [ ] Проект создаётся вместе с деревом каталогов
- [ ] Права на создание и удаление проекта соблюдаются
- [ ] Тесты `test_auth.py` и `test_projects.py` зелёные
- [ ] Приложение стартует с пустой БД без ручных правок

## Типичные отказы

| Симптом | Причина | Что делать |
|---|---|---|
| `passlib` ругается на bcrypt | Несовместимость версий | Зафиксировать `bcrypt` в `requirements.txt` |
| БД заблокирована | Параллельные записи в SQLite | Включить WAL: `PRAGMA journal_mode=WAL` при старте |
| Кириллица в именах ломает путь | Кодировка пути | В путь идёт только сгенерированный `stored_filename`, оригинал — в БД |
