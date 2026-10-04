# QR‑2.0 — Интеграция с банком: неактивные черновики

Дата: 2026-10-01. Только локальная разработка в `R:\TradeSpace`.

## Исходная версия и сохранность

- QR‑1.2 checkpoint: **`1dd4ed8728c2526742dd94da91650019cb9d1368`**; source branch `codex/qr1-static-nspk`.
- Исходный `git status --short`: пустой; staged/untracked изменений не было. Повторно фиксировать QR‑1.2 не требовалось. Его отчёт дополнен фактическим SHA/status.
- Новая локальная ветка: **`codex/qr2-bank-drafts`**, создана от указанного checkpoint. История сохранена, remote не добавлялся.
- Применимых AGENTS.md в корне R:, проекте и вложенных каталогах не обнаружено.
- До правок сохранены HEAD/branch/status/index/diff и исходный Freeze в `R:\TradeSpace-Archive\qr2_0-before-20261001-115011`. Перед миграцией создана резервная копия `dev-before-0029.dump` (вне проекта, не для Git).
- Исходный отдельный проект не изменялся. Предыдущие Rapira/TeamLead/merchant/логотип/UI изменения входят в исходный checkpoint и сохранены.

## Реализованный раздел

**Trader → Интеграция с банком**. Desktop: самостоятельный пункт основной навигации. Mobile: пункт в «Ещё», прежние четыре основных назначения сохранены.

- Пустое состояние «Банки пока не подключены», действие «Добавить банк».
- Альфа-Банк (`alfabank`) и Модульбанк (`modulbank`); названия и проверка банка из существующего `app/core/russian_banks.py`, справочник не менялся.
- Название: trim, 1–120 символов, без управляющих символов; необязательный собственный реквизит QR NSPK.
- Создание, изменение, удаление с подтверждением; несколько черновиков одного банка разрешены. Данные сохраняются в PostgreSQL между запросами/перезагрузкой/повторным входом.
- Единственный статус: **«Не подключён»**. Пояснение: «Подключение станет доступно после получения доступа от банка».
- Нет полей секретов, ключей, паролей, сертификатов, кнопок активации/проверки или банковских HTTP клиентов.
- Ошибки валидации сохраняют введённые данные, выводятся в форме (422). Статус подключения нельзя активировать ни через форму, ни прямым UPDATE: DB check допускает только `not_connected`.

### Routes и граница команд

| Метод / путь для Trader | Назначение |
|---|---|
| GET `/trader/cabinet/tradespace/bank-integrations` | Список/пустое состояние; существующий dispatcher, отдельное чтение без financial view model |
| GET тот же путь `?new=1` | Форма нового черновика |
| GET тот же путь `?edit=<id>` | Форма своего черновика; чужой/невалидный ID → 404 |
| POST `/trader/cabinet/bank-integrations/create` | Сохранение |
| POST `/trader/cabinet/bank-integrations/<id>/edit` | Изменение своего черновика |
| POST `/trader/cabinet/bank-integrations/<id>/delete` | Удаление своего черновика |

Существующий realm middleware сопоставляет prefixed browser URLs внутренним `/cabinet/...`. Новые команды зарегистрированы отдельным router; ни один существующий финансовый handler не заменяется. GET presentation router остаётся GET-only.

### Ownership, безопасность и транзакции

- Разрешена строго роль `trader`. Operator, Merchant, TeamLead, Support, Admin, Superadmin, Aggregator не получили новых прав; прямые GET/POST проверяются сервером.
- Вход и действительность пользователя/сессии проверяются прежним `_authenticated_user/get_current_web_user`; session fingerprint, realm cookies, CSRF и 2FA не изменялись. Подменённый session_view → 409; отсутствующий CSRF → 403.
- Поиск черновика ограничен одновременно `id` и `trader_id`; edit/delete берут row lock с повторным чтением (`populate_existing`). Чужой ID не раскрывает данные.
- При привязке блокируется выбранный Requisite и проверяются owner, `method=sbp_qr`, неархивный/неудалённый статус и канонический банк из справочника. Неверный/чужой/не-QR/другой банк → 422 без записи.
- GET не вызывает финансовый reader с side effects. POST меняет только draft и AuditLog (`bank_draft_created/updated/deleted`) в одной транзакции. Аудит содержит actor, bank code и ID привязки, без секретов.
- Ответы страниц `Cache-Control: no-store, max-age=0`. Экранирование пользовательского названия выполняет Jinja.

## Модель и миграция

Новая миграция **`0029_bank_connection_drafts`**, parent `0028_static_nspk_qr`.

Таблица `trader_bank_connections`: UUID id, trader_id FK users, bank_code, name, nullable requisite_id FK requisites, status, created_at/updated_at. Индексы owner и requisite; CHECK: два поддерживаемых банка, непустое имя, только `not_connected`. Нет уникальности owner+bank — повторы разрешены. Нет финансовых полей, credentials JSON или переключателя активации.

FK от черновика к реквизиту имеет ON DELETE SET NULL; обратного удаления нет. Удаление черновика не меняет реквизит. Привязка не участвует в routing/QR выдаче и не активирует динамический QR. Даже черновик без привязки сохраняется. Downgrade удаляет только новую таблицу настроек, не реквизиты и не платежи; применять его к нужным черновикам без сохранения настроек нельзя.

## Точный scope файлов

| Файлы | Изменение |
|---|---|
| `app/models/bank_connections.py`, `app/models/__init__.py` | Модель и регистрация |
| `alembic/versions/0029_bank_connection_drafts.py` | Одна аддитивная таблица; прежние миграции неизменны |
| `app/services/bank_connections.py` | Изолированный draft CRUD, ownership/банк/метод, аудит |
| `app/web/bank_connections.py`, `app/web/__init__.py` | Render/form commands, регистрация |
| `app/presentation/tradespace/routes.py`, `navigation.py` | GET dispatch + Trader-only пункт |
| `app/templates/tradespace/trader/bank_connections.html` | Список, форма, delete confirmation |
| `app/static/tradespace/css/bank-connections.css` | Только стили нового раздела, desktop/mobile |
| `tests/test_qr2_bank_drafts.py` | Пять содержательных тестов (оба origin параметризованы) |
| `tests/test_migrations_postgres.py`, `tests/test_qr1_migration.py` | Только четыре ожидания текущего Alembic head → 0029; денежные assertions и запреты downgrade не менялись |
| `scripts/platform1_freeze_guard.py`, `tests/golden/freeze_zone_baseline.json` | Дополнительная защита новых обработчиков; принятие только объяснённых SHA |
| `docs/QR1_2_ACCEPTANCE.md`, `docs/QR2_0_REPORT.md` | Checkpoint clarification и этот отчёт |

## Проверки

Проверяемый код: текущая ветка QR‑2.0; команды/logs/manifest в указанном внешнем архиве. Финальные результаты приведены ниже; все проверки завершены до локального коммита.

| Проверка | Фактический результат |
|---|---|
| Целевой PostgreSQL/HTTP набор | **10 passed**, 1 warning, 38.09 s (`targeted-fixed.log`); новая модель/CRUD, ownership/банки/методы/роли, оба API static QR, migration + existing auth/Jinja |
| Обязательный full suite | **467 passed**, 10 warnings, **1032.41 s**, `full.log`; команда `.\.venv\Scripts\python.exe -m scripts.recovery_run test` |
| Финальный целевой повтор после UI/Freeze | **14 passed**, 1 warning, **60.20 s**; QR‑2.0 + preparation guards + auth shell + Jinja/OpenAPI, `final-targeted.log` |
| Native Golden `--verify` | **PASS 16/16**, реально исполнены **23 oracles** (39.40 s), `golden-native.log`; snapshots/index не обновлялись |
| Native Freeze, без override | **PASS 97 файлов / 18 областей**, `freeze-native.log`; `--record-baseline` только после review семи объяснённых paths |
| Preflight | **19/19 PASS**, current=head 0029; включая live Rapira ask, `preflight.log` |
| HTTP smoke | **4/4 PASS**, `smoke.log` |
| Recovery/business | **14/14 PASS**, `recovery.log`; реальный локальный HTTP500 → scheduled retry → 204, HMAC и reconciliation |
| Linux Docker | Новый **`tradespace-qr20:local`**, **7 passed**, 1 warning, 27.48 s; clean migrations, ownership/CRUD/static QR и import/Jinja. `docker-final-qa.log` |
| Image parity | **197/197** текущих app/alembic/config/dependency файлов совпали по SHA256, `image-parity.json`; image `sha256:3b8fad971bedd424299bcde5821b3fdae57832da6a01ae1e23657292c82ec912` |
| Dependency audit | **68 pinned dependencies, 0 known vulnerabilities**, pip-audit 2.10.1, exact requirements.lock, `dependency-audit.json`; временный контейнер, зависимости приложения не менялись |
| Compile / package | compileall app/scripts/tests PASS; pip check PASS; verify_package_install PASS |
| Whitespace / artifact policy | `git diff --check`, `git diff --cached --check` — exit 0; `scripts.check_release_artifacts` PASS. Старые исторические whitespace замечания не исправлялись |

Предыдущий полный QR‑1.1 suite имел 462 теста; **+5 QR‑2.0 случаев** дают 467. Это новый полный запуск, а не унаследованный PASS. После его начала финализировались только текущие шаблон/стиль и docstring; финальная presentation-версия отдельно повторно проверена browser QA, Linux tests и 14 targeted tests. С момента `code-during-full.json` исполняемые app/test файлы далее не менялись. Native Golden запускался последовательно после full/recovery, без гонки evidence JSON.

Проверка 0029 включает реальный Alembic upgrade, constraint против connected, сохранность Requisite при удалении draft и downgrade/reupgrade, а также `alembic check` без drift. Первоначальный тестовый raw SQL seed не заполнял обязательные поля User и был исправлен на обычный ORM seed; первоначальный FAIL не назван PASS. Регистрация router исправлена под текущий FastAPI (`routes.extend`, без deferred `_IncludedRouter` в старом списке маршрутов). Финансовые handlers и expectations не менялись.

Staged tree отдельно экспортирован без Git filters: **346/346 blobs** совпали с worktree; Freeze на этом экспорте также **PASS 97/18** (`staged-freeze.log`). **Gitleaks 8.30.1: 0 findings** по полному публикуемому составу с существующей конфигурацией; новых исключений нет (`staged-secrets.json`). В коммите только 17 объяснённых файлов этой задачи. `.env`, credentials, БД/backup, screenshots, logs, uploads, venv и временные QA scripts не staged. `git diff --cached --check` повторён после финального отчёта, exit 0.


### Browser QA

Реальный Edge/Chromium через Playwright, реальное приложение на `127.0.0.1:8002`, отдельная синтетическая PostgreSQL schema. Desktop **1440×1000, DPR1**; mobile **390×844, DPR2, touch**. Не эмуляция HTML и не подстановка успешного ответа.

Проверены вход, пункт навигации (на mobile через «Ещё»), пустое состояние, форма обоих банков, собственный QR, создание, reload, изменение, диалог удаления и возвращение пустого состояния. **Нет горизонтального overflow**, JS errors **0**, external browser requests **0**. HTTP тест дополнительно проверяет logout/relogin persistence и обе origin QR операции.

Шесть окончательных снимков вне Git: `desktop-empty.png`, `desktop-form.png`, `desktop-saved.png`, `mobile-empty.png`, `mobile-form.png`, `mobile-saved.png` в архиве задачи. Метрики — `browser-results.json`.

Встроенный CUA не смог инициализировать служебный каталог (`failed to write kernel assets`); проверка выполнена другим локальным браузером, не объявляется проверкой встроенной вкладки. Первый быстрый browser проход получил штатный 429 в конце mobile удаления; повтор выполнен с паузой между viewport, **rate limit не отключался**. В процессе QA исправлены точные accessible names select и локальное наложение Add-button на success message; общие стили кабинетов не менялись.

### Freeze review

Сохранены все 93 прежних protected paths и 18 областей. 90 файлов из старого baseline побайтно неизменны. Объяснённые изменения:

| Путь | Область / основание принятия |
|---|---|
| `app/models/__init__.py` | FZ‑01: импорт единственной новой модели |
| `app/models/bank_connections.py` | FZ‑01: новая неактивная сущность с CHECK |
| `alembic/versions/0029_bank_connection_drafts.py` | FZ‑01: аддитивная миграция |
| `app/presentation/tradespace/routes.py` | FZ‑03: три строки GET dispatch до общего financial reader |
| `app/web/__init__.py` | FZ‑03: отдельный router в существующем middleware, уточнение docstring |
| `app/web/bank_connections.py` | FZ‑03: новый server authorization/формы/readonly render |
| `app/services/bank_connections.py` | FZ‑03: новый ownership/validation CRUD |

Новые service/web paths явно добавлены в FZ‑03; модель/миграция покрыты прежними glob. Правила guard, сравнение hashes и запрет неизвестных изменений не отключались; `--allow` не использовался. Итоговое покрытие **97 файлов / 18 областей**. Golden snapshots, index и финансовые ожидания **не меняются**.

## Ограничения и handoff

Это подготовка **настроек будущего подключения**, а не банковская интеграция. Для следующего этапа нужны согласованные конкретные API/договор/доступ от каждого банка, безопасная схема credentials, отдельные адаптеры и тестирование. В этой версии нет запросов к Альфа-Банку/Модульбанку, проверки банковского счёта, получения транзакций, dynamic QR или автоматического подтверждения.

Bank draft не управляет static QR; изменение/удаление реквизита в другом разделе впоследствии может сделать сохранённую привязку недоступной. Это не подключение к банку и не permission на активацию. Существующие QR‑1.2 ограничения (ручная проверка денег, поздние/повторные переводы, Aggregator replay contract) сохранены.

Нет push, merge, deploy, Production-активации или реальных платежей. Локальный dev runtime: `http://127.0.0.1:8000`; миграция 0029 применена после backup. Синтетический preview и QA контейнеры не являются Production.

## Итог

**QR‑2.0 готов как локальный раздел сохраняемых неактивных черновиков.** Блокеров этого объёма не осталось. Банковские интеграции не реализованы и не активированы.

Локальный коммит выполняется на `codex/qr2-bank-drafts` после проверки staged-состава; фактический SHA и post-commit status сохраняются во внешнем `checkpoint-verification.json` и сообщаются пользователю. Source checkpoint SHA приведён в начале документа. Ни один QA screenshot, fixture access file, backup, log или runtime credential не входит в Git.
