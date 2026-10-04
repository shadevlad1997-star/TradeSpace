# TradeSpace — release audit

Дата проверки: 2026-10-04
Проект: `R:\TradeSpace`
Рабочая ветка аудита: `codex/tradespace-release-audit`
Исходный checkpoint: `b50f15e8312db36272c7c2eb500092762473e8b5`

## Итог

**Статус:** обязательные локальные gates пройдены; P0/P1 не обнаружены. Публикация выполняется отдельной веткой из чистой истории удалённого репозитория после проверки состава и секретов. Деплой и Production-активация не входят в эту работу.

Проверенный processing core сохраняет денежные инварианты: создание и идемпотентное повторение заявки, маршрутизация, резерв, подтверждение, истечение, спор, immutable FeeSnapshot, комиссии Merchant/Trader/TeamLead, Rolling, расчёты, outbox и повторная доставка. Полный PostgreSQL suite и Golden не выявили расхождений бизнес-результатов.

## Исходное состояние и безопасность данных

- До аудита сохранены branch, HEAD, status, полный diff и история в `R:\TradeSpace-Archive\release-audit-20261004-160000`.
- Рабочая БД, аккаунты, пароли, балансы и история не сбрасывались и не использовались для destructive-проверок.
- Дамп локальной БД восстановлен в отдельный PostgreSQL-контейнер; ревизия миграций и контрольные количества сущностей совпали.
- Автоматические и браузерные сценарии выполнялись в отдельной БД на `127.0.0.1:55432` и отдельном Redis на `127.0.0.1:56379` с синтетическими данными.
- Production-сервер, реальные платежи и PlaySpace не изменялись.

## Карта системы

### Роли

| Роль | Реализованная поверхность | Результат аудита |
|---|---|---|
| Superadmin | staff realm, управление платформой, участниками, тарифами, интеграциями и расчётами | Подтверждено кодом, тестами и браузером |
| Admin | staff realm, операционный контроль и административные решения | Подтверждено кодом, тестами и браузером |
| Support | staff realm, read/operational support с ограниченными командами | Подтверждено кодом, тестами и браузером |
| TeamLead | staff realm, команда, начисления, долг и расчёты | Подтверждено кодом, Golden и браузером |
| Merchant | отдельный realm, операции, API/Webhook, финансы, расчёты и споры | Подтверждено кодом, Golden и браузером |
| Trader | trader realm, очередь, реквизиты, баланс и споры | Подтверждено кодом, Golden и браузером |
| Operator | trader realm с отдельным subject/ownership | Подтверждено кодом, тестами и браузером |
| Aggregator | отдельная API-аутентификация и browser realm | API/credentials реализованы; browser product pages пока заглушки |

Client-side скрытие не используется как источник полномочий. Реальные проверки роли, realm и ownership выполняются на сервере. TeamLead остаётся в staff realm.

### Способы оплаты и реквизиты

- `sbp_qr` — статический QR НСПК с ручным подтверждением; поддерживается загрузка/декодирование, snapshot и защита QR + сумма + валюта от конкурентной выдачи.
- `sbp` — СБП по банковскому реквизиту.
- `c2c` — перевод на карту.
- `mobile_commerce` — мобильная коммерция; legacy alias нормализуется внутри контракта.
- Динамический банковский QR не объявляется работающим: в коде есть protocol/disabled state и неактивные черновики подключений, но live-адаптеров Альфа-Банка и Модульбанка нет.

### API, jobs и интеграции

- API families: auth, Merchant, Aggregator, Admin, cabinet/integration и read-only presentation/public QR.
- Background jobs: webhook delivery, aggregator callback delivery, due-scanners для обоих типов доставки, expiration, appeal timeout и antiscam maintenance; Celery beat содержит пять периодических расписаний.
- Webhook delivery использует durable outbox, leases, retry/dead-letter и подписанный payload. Ошибка enqueue после commit восстанавливается due-scanner, не откатывая финансовую транзакцию.
- Rapira используется только как read-only источник askPrice. Финансовые команды повторно проверяют свежий курс на сервере.

## Находки и исправления

| ID | Приоритет | Находка и причина | Решение | Повторная проверка |
|---|---:|---|---|---|
| F-01 | P2 | Допустимая короткая ссылка НСПК без `type` оставалась «На проверке», хотя Trader явно выбрал статический QR. Причина — отсутствие bank metadata трактовалось как блокирующая неопределённость. | Принят уже подготовленный адресный diff: выбор Trader хранится как provenance, поддерживаемый short URL активируется; явный dynamic/invalid payload по-прежнему отклоняется. | `tests/test_qr_classification.py`, QR regressions и полный suite — PASS. |
| F-02 | P2 | Soft-deleted реквизит оставался в обычном списке Trader. Причина — list view не исключал terminal deleted status. | Обычный список, поиск, пагинация и счётчики фильтруют deleted на сервере; history/appeals/старые операции сохраняют ссылку. Routing уже исключает deleted. | `tests/test_requisite_soft_delete.py` и полный suite — PASS. |
| F-03 | P3 / config | В пользовательском локальном runtime Rapira выключена (`RAPIRA_RATES_ENABLED=false`). | Код не изменялся. Preflight честно сообщает disabled. В изолированном браузерном QA проверен валидный mock askPrice и TeamLead estimate. | User runtime preflight 18/19; QA widget показал RUB и приблизительный USDT после комиссии. |
| F-04 | P3 / deferred | Live dynamic QR через банки отсутствует. | Не маскировалось фиктивным успехом. Черновики подключений остаются неактивными; внешние ключи не запрашиваются и не сохраняются. | Код/IA/браузер подтверждают disabled state. |
| F-05 | P3 / incomplete UI | Aggregator browser realm существует, но product pages показывают «Раздел ещё не подключён». | Не объявляется завершённым. Aggregator API, отдельные Sandbox/Production keys и server-side gate проверяются тестами. | Авторизация и shell открыты в браузере; страницы явно обозначены как недоступные. |
| F-06 | verification gap | Локальный PlaySpace запущен, но изолированная TradeSpace QA-конфигурация не направлена в него; выполнение E2E изменило бы данные другого проекта. | PlaySpace не изменялся. Signed callback, идемпотентность и 500 → retry → 204 проверены PostgreSQL-тестами с фактическими HTTP semantics. | `tests/test_qr1.py` и `tests/test_webhook_hardening_postgres.py` входят в 491 PASS. |
| F-07 | P2 / packaging | В локальном дереве отсутствовали notices для распространяемых Swagger UI/normalize assets, хотя сами vendored assets присутствуют. | Восстановлены точные license/NOTICE файлы и сводный `THIRD_PARTY_NOTICES.md` из проверенного `origin/main`. | Release artifact policy и финальная publish-tree проверка — PASS. |

P0 и P1 не обнаружены. Теоретические forensic findings не изменялись без воспроизведения.

## Финансы, транзакции и конкуренция

Проверены исполняемыми PostgreSQL-сценариями:

- HMAC v2, nonce/replay и идемпотентное создание возвращают один business result;
- параллельные подтверждения не создают второй финансовый результат;
- merchant settlement резервируется под row lock и не превышает доступный баланс;
- Trader balance/hold и Merchant available/frozen согласованы с ledger;
- FeeSnapshot остаётся источником исторических ставок при подтверждении и споре;
- paid appeal не повторяет списания, TeamLead accrual или Rolling recovery;
- pending/expired/cancelled appeal применяет единственный разрешённый финансовый переход;
- QR advisory lock и DB unique constraint не позволяют одновременно выдать тот же QR на ту же сумму и валюту;
- webhook claim/lease, stale recovery и manual retry идемпотентны;
- TeamLead settlement и Rolling boundary защищены от конкурентного расходования.

Concurrency selection запускался три раза подряд: пять критических сценариев в каждом повторе, все прошли.

## Security review

- Realm cookies, session regeneration, logout, CSRF origin/token checks и обязательная 2FA staff-ролей покрыты тестами; staff browser-login дополнительно проверен синтетическим OTP.
- Direct URL и object ownership проверяются server-side; полная suite включает отрицательные cross-role/cross-owner сценарии.
- Merchant/Aggregator credentials разделены по Sandbox/Production; неактивный, suspended, revoked или key другого environment отклоняется до финансовых записей.
- One-time secrets не хранятся в session payload и показываются через одноразовую Redis-flash запись.
- QR upload проходит size/type/decode/host validation; неподдерживаемый или явно динамический payload не становится статическим реквизитом.
- Webhook URL проходит SSRF-проверки; secret/signature не логируются.
- По publishable tree не найдено `Ozark`, `OzarkPay` или ссылок на старый кабинет.
- Dependency audit по `requirements.lock`: **No known vulnerabilities found**.

## Автоматические проверки

| Gate | Результат |
|---|---|
| Full PostgreSQL pytest | **491 passed**, 10 warnings, 655.70 s |
| Native Golden | **PASS 16/16**, 23 oracle checks |
| Native Freeze | **PASS**, 97 protected files, 18 areas |
| Concurrency critical set | **5 passed × 3** |
| Clean DB migrations | `0001 → 0029`, current/heads/check — PASS |
| Migration round-trip | `0029 → 0023 → 0029` — PASS |
| Populated DB downgrade guard | ожидаемый отказ на 0025 при TeamLead finance activity |
| Compile | `compileall app scripts tests` — PASS |
| Release artifacts | PASS |
| `pip check` | PASS |
| `git diff --check` | PASS |
| User-runtime deterministic preflight | **18/19**; только Rapira disabled by config |
| HTTP smoke | **4/4**: health, ready, version, staff login |
| Docker build | API, worker, beat — PASS |
| Container runtime | health=ok, ready DB/Redis=ok; worker ready; beat started |
| Container restart | core row counts stable; demo users/financial entries не повторились |
| Dependency audit | PASS; известных CVE для lock-файла не найдено |

Первые два запуска Golden были отклонены до сравнения business result из-за неверной временной DB/Redis-конфигурации. После создания окружения по штатному `env.test`, применения миграций и изоляции сервисов native Golden прошёл 16/16 без изменения ожидаемых сумм, статусов или snapshot.

## Браузерный аудит

Фактически открыты локальные синтетические кабинеты, без финансовых POST-действий:

| Realm | Проверенные экраны/ширины |
|---|---|
| Trader | login, workbench/queue, balances, recent operations; 1440 и 390 px |
| Operator | login и пустой workbench с отдельным subject; 1024 px |
| Merchant | overview, operations/filter/table/cards; 1280, 768 и 390 px |
| TeamLead | login с 2FA, overview, RUB/USDT estimate, team/accrual summary; 1024 и 390 px |
| Support | control center; 1280 px |
| Admin | control center и role-specific identity; 1280 px |
| Superadmin | control center и role-specific identity; 390 px |
| Aggregator | login, navigation и explicit unavailable state; 1024 px |

Проверены horizontal navigation, mobile bottom navigation, «Ещё», таблица→mobile cards, фильтры, статусы, суммы, пустые состояния и отсутствие горизонтального растягивания. Browser console: 0 warning/error записей на проверенных страницах. В UI не обнаружены Ozark branding, raw backend status codes или переходы в старый кабинет.

Снимки были получены через локальный in-app browser на синтетических данных и не добавлялись в Git, чтобы исключить перенос QA-данных и артефактов.

## Реальные и имитированные интеграции

**Реально проверено локально:** PostgreSQL, Redis, API, worker, beat, health/readiness, restart, миграции, восстановление dump, browser sessions, CSRF, staff 2FA, локальный HTTP для Rapira widget.

**Проверено управляемым test transport:** Merchant/Aggregator HMAC, webhook/callback signatures, 500 → scheduled retry → 204, timeout/429, stale lease, enqueue failure recovery, SSRF blocks, QR image decoding и bank error mapping.

**Не проверялось как внешняя Production-интеграция:** Rapira public endpoint из user runtime, банковские dynamic QR API, реальные Telegram/email/monitoring endpoints, реальные переводы/выплаты, PlaySpace webhook на его пользовательской БД.

## Изменённые файлы проверенного состава

Принятые ранее локальные исправления:

- `app/services/payment_qr.py`
- `app/services/requisites.py`
- `app/web/routes.py`
- `app/web/ui_messages.py`
- `app/presentation/tradespace/trader/view_models.py`
- `app/templates/tradespace/trader/_requisite_fields.html`
- `tests/test_qr_classification.py`
- `tests/test_requisite_soft_delete.py`
- `tests/golden/freeze_zone_baseline.json`
- `docs/QR_STATIC_ACTIVATION.md`

Восстановленные обязательные notices:

- `app/static/normalize.LICENSE.txt`
- `app/static/swagger-ui-bundle.js.LICENSE.txt`
- `app/static/swagger-ui.LICENSE.txt`
- `app/static/swagger-ui.NOTICE.txt`
- `docs/THIRD_PARTY_NOTICES.md`

Добавлено аудитом:

- `docs/TRADESPACE_RELEASE_AUDIT.md`

## Ограничения перед обновлением сервера

1. На сервере потребуется Production-конфигурация и secrets management; локальные `.env`, QA credentials и архивы не публикуются.
2. Rapira нужно включить и проверить из фактической серверной сети перед разрешением зависимых расчётов.
3. Dynamic QR останется недоступным до реализации и sandbox-проверки конкретных банковских адаптеров.
4. Aggregator browser pages остаются незавершёнными; API и credential control работают независимо.
5. Нужны обычные server rollout steps: резервная копия, миграции до 0029, health/readiness, worker/beat и post-deploy smoke. Этот аудит deploy не выполняет.

## Публикация

- Private repository: `https://github.com/shadevlad1997-star/TradeSpace`
- Published branch: `codex/tradespace-release-audit`
- Audited release commit: `a9037dd88ae4adfed8e58e9dea90fa52b6832dba`
- Draft PR: `https://github.com/shadevlad1997-star/TradeSpace/pull/2`
- Push CI: `https://github.com/shadevlad1997-star/TradeSpace/actions/runs/37207541658` — SUCCESS
- Pull request CI: `https://github.com/shadevlad1997-star/TradeSpace/actions/runs/37207574142` — SUCCESS

Оба запуска прошли Docker build, security/repository hygiene, PostgreSQL concurrency, migrations и полный unit/integration job. Merge и deploy этой задачей не выполнялись.
