# Aggregator Sandbox / Production — implementation report and handoff

Дата: 2026-09-24. Проект: `R:\TradeSpace`. Ветка `codex/tradespace-wave1`, HEAD `8b88337e55eb5d8b6d3a3d2ac78e7ed8943d0279`.

## Результат

Требование владельца реализовано. Aggregator API имеет отдельный реестр Sandbox/Production-ключей и собственную сущность Production-допуска. `MerchantProductionAccess` не используется для допуска агрегатора. Выпуск боевого ключа не включает боевой доступ автоматически.

- Новые Sandbox/Production credentials различаются; ключи принимаются только в соответствующем окружении. Миграция сохраняет существующие ключи как Sandbox, без перевыпуска.
- Production-ключи выпускаются/ротируются только при Production ENV + Production binding БД + разрешающем флаге владельца, по подтверждённой команде Superadmin с 2FA. Создание Production-аккаунта не выпускает ключ.
- Активация требует отдельного подтверждения `PRODUCTION`, причины, права Superadmin, флага владельца, доступного аккаунта, ключа и действующих тарифов. Для настроенного callback проверяется публичный HTTPS. В UI показаны фактический режим и причины недоступности.
- Подпись запроса проверяется прежним способом. Чужое окружение, отсутствие/приостановка допуска, приостановленный/отозванный ключ и блокированный/архивный аккаунт отклоняются до финансовых записей и резервирования replay nonce. Возврат к account.api_key как запасному способу авторизации исключён.
- Выдача секрета одноразовая: POST response/no-store или пользовательский Redis flash/TTL/GETDEL + маскированная no-store страница. В cookies только случайный flash ID. Повторный просмотр/повтор выдачи не возвращает секрет. Ротация отзывает старый ключ; повторная ротация старого ID отклоняется. Ключ другого агрегатора изменить нельзя.
- Аудит содержит actor, aggregator ID, UUID ключа, переход, режим, причину и IP. Сами API-ключи и секреты в audit не записываются. Production-допуск и ключи управляются независимо от financial ledger.

UI: **Участники → Агрегаторы → карточка**. Команды API и точные условия описаны в [INTEGRATION_MODES.md](INTEGRATION_MODES.md). Browser-команды сохраняют staff realm, CSRF и подтверждённую 2FA. Старый `/secret` путь вызывает тот же защищённый механизм и больше не позволяет ротацию без явного подтверждения.

## Совместимость и финансовая безопасность

Сохранены URL и payload платежного Aggregator API, `X-API-Key`, `X-Timestamp`, `X-Signature`, `X-Request-ID`, `X-Idempotency-Key`, HMAC timestamp + dot + body, финансовые формулы, routing, hold, fee snapshots, settlement, replay/idempotency и callbacks. Добавлены management endpoints и явные ошибки допуска.

API-секрет по существующему контракту также подписывает platform-to-aggregator callbacks. Ротация атомарно меняет текущий секрет callbacks вместе с API credentials; потребитель должен обновить request signer и callback verifier. Повторные доставки после ротации подписываются новым секретом. Отзыв inbound-ключа сохраняет подпись для уже принятых платежей/callbacks. Downstream merchant callback secrets не менялись. Это отражено в подтверждении, а не скрыто за кнопкой.

В `app/services/aggregators.py` изменены только `create_aggregator_account` и прежний незащищённый helper ротации. AST остальных функций, включая создание платежа и доставку callbacks, совпадает с состоянием до задачи. 29 остальных существовавших файлов services и все 26 ранее существовавших миграций неизменны. Добавлена только миграция `0027_aggregator_credentials` с двумя таблицами доступа/credentials; финансовая схема не изменялась.

Синтетический сценарий в обоих окружениях: платёж 1000 RUB, merchant fee 100, aggregator executor fee 50, platform income 50. По существующей aggregator-ветке резерв Trader = 1000, после подтверждения баланс Trader 100000 → 99000, hold → 0, merchant balance → 900. Повторное подтверждение не начисляет деньги ещё раз. В первом новом тесте ошибочно ожидался hold 950 из Merchant flow; после сверки действующей aggregator-ветки исправлено только тестовое ожидание. Production-код финансов не менялся ради тестов.

Допуск читает актуальное зафиксированное состояние без нового финансового lock order. Уже допущенный запрос может завершиться после отзыва/приостановки. Флаг владельца запрещает новый выпуск/активацию, но не является emergency stop уже активного доступа; для остановки новых запросов используется приостановка доступа или ключа.

## Проверки

| Проверка | Фактический результат |
|---|---|
| Полный PostgreSQL pytest | **345 passed**, 0 failed / errors / skipped, 635.21 s |
| Новые проверки Aggregator | **33 passed** в составе full suite; два режима в отдельных disposable схемах, только synthetic actors/keys |
| Реальная миграция и старый Sandbox HMAC | PASS: старый ключ/шифротекст сохранены, прежняя подпись проходит новый auth; active Production не появляется |
| Production clean DB / immutable environment | PASS существующих миграционных тестов; ожидаемый head обновлён на 0027 |
| Права/условия | Admin, Support, Merchant, Trader, TeamLead, Aggregator не могут выпускать/активировать; Superadmin без 2FA/owner flag/условий получает отказ |
| Ротация/аудит/concurrency | PASS: конкурентный issue 200/409, один действующий ключ; конкурентная активация без дублирования audit; чужой ID и повтор старой ротации отклонены |
| Secret / session / CSRF | PASS: no-store, секрет не в cookie, GETDEL одноразов, CSRF и verified 2FA обязательны |
| Golden | **16/16 match**, 23 oracle tests passed; все 17 snapshot/index JSON побайтово неизменны |
| Freeze | **90 protected files / 18 areas PASS** без override после адресного принятия согласованных изменений |
| Freeze negative control | Искусственный неверный hash нового credential service корректно вызывает FAIL / FZ-15 |
| Preflight | **19/19 PASS** |
| HTTP smoke | **4/4 PASS**, повторено после финального перезапуска |
| Recovery/business | **14/14 PASS**, включая реальный локальный HTTP 500 → Celery retry → HTTP 204 и проверку HMAC |
| Aggregator callback retry | Существующий реальный локальный HTTP 500 → retry → 204 signature test PASS в full suite |
| Compile / diff whitespace | PASS |

10 предупреждений full suite — существующие типы deprecation FastAPI ORJSONResponse и Alembic path_separator, не провал проверок. Существующий Golden harness сохраняет прежнюю границу автоматизации (9 automated / 7 partially automated); пропуски его исторической coverage не скрыты и эталоны не перезаписывались.

## Текущее окружение и запрет реального Production

Локальный TradeSpace перезапущен на `http://127.0.0.1:8000`. База dev остаётся **Sandbox**, head `0027_aggregator_credentials`; Production-ключей Aggregator = **0**, AggregatorProductionAccess = **0**, MerchantProductionAccess = **0**, owner flag выключен. Production настройки/ключи в тестах существовали только внутри disposable test schemas/databases, а не в локальном рабочем продукте. Реальных платежей, активации, боевых ключей, commit, push или deploy не было.

Перед миграцией приложения остановлены и сохранён зашифрованный dump локальной БД; расшифровка в памяти проверена на полное совпадение, plaintext dump не записывался. Существовавшие до задачи изменения сохранены: исходные hashes/status/копии файлов лежат отдельно от проекта. Оригинальный recovery workspace не менялся: **225 hashes match**.

Freeze не ослаблен: владелец явно разрешил эту отдельную core-задачу. Адресно приняты ровно 8 проверенных protected paths (6 изменённых + 2 новых); 82 остальных protected files совпадают. Старый baseline, точный patch и negative-control результат сохранены. Новый service добавлен в FZ-15, новая миграция автоматически входит в FZ-01. Следующее обычное UI-изменение снова проверяется без override.

## Приёмка интерфейса

**Владелец сообщил, что вручную проверил интерфейс и принял его без замечаний.** Это ручная приёмка владельца. Она не выдаётся за автоматизированную браузерную проверку. В этой задаче выполнены автоматические HTTP/HTML/permission/security tests; визуальный браузерный осмотр не заявляется.

## Handoff для серверного этапа

Локальная реализация проверена. Сам серверный этап не выполнялся. Для будущего Production: отдельные чистые БД/Redis/секреты, миграция до 0027, bootstrap Superadmin/2FA, тарифы, затем отдельное разрешение владельца на флаг и выпуск/активацию ключей на соответствующем Production-сервере. Sandbox данные и balances не переносить. Rebuild и проверка release ZIP обязательны: старый `R:\TradeSpace-Release\tradespace-clean.zip` предшествует этим изменениям и не был обновлён этой задачей. Проверки реального партнёрского consumer/TLS/egress остаются приёмкой целевого окружения.

Доказательства этой задачи: `R:\TradeSpace-Archive\aggregator-modes-20260924` (`before.json`, encrypted dump, protected-task.diff, full-suite.log, golden.log, preflight.log, smoke.log, recovery.log, local-boundary.json, freeze-before.json, freeze-negative-control.log, task-changes.json`).

## Файлы этой задачи

- `alembic/versions/0027_aggregator_credentials.py`
- `app/api/v1/aggregator.py`
- `app/api/v1/integration.py`
- `app/models/__init__.py`
- `app/models/integration.py`
- `app/presentation/tradespace/staff/forms.py`
- `app/presentation/tradespace/staff/view_models.py`
- `app/services/aggregator_credentials.py`
- `app/services/aggregators.py`
- `app/templates/tradespace/auth/aggregator_secret.html`
- `app/web/routes.py`
- `docs/AGGREGATOR_MODES_REPORT.md`
- `docs/CLEAN_SERVER_START.md`
- `docs/INTEGRATION_MODES.md`
- `scripts/platform1_freeze_guard.py`
- `tests/golden/freeze_zone_baseline.json`
- `tests/test_aggregator_credentials_postgres.py`
- `tests/test_migrations_postgres.py`
