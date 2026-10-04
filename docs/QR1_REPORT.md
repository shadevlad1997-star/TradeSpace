# QR-1 — реализация и проверки

Дата: 30.09.2026. Работа только в `R:\TradeSpace`. Ветка `codex/qr1-static-nspk`, исходный HEAD `8b88337e55eb5d8b6d3a3d2ac78e7ed8943d0279`. Новый коммит, push и deploy не выполнялись. Старый исходный workspace не изменялся. Реальных переводов и production-активации не было.

## Сохранность исходного состояния

Работа начата от текущего dirty состояния `codex/tradespace-wave1`, а не от чистого исторического HEAD. Полный список исходных tracked/untracked изменений и копии файлов сохранены вне проекта: `R:\TradeSpace-Archive\qr1-before-20260930\status.txt`, `head.txt`, `files/`. Эти предыдущие изменения не объявляются изменениями QR-1. В частности, прежние правки Merchant, TeamLead, Rapira, логотипа и подписей сохранены.

Перед локальной миграцией создан и проверен через pg_restore list backup `tradespace-dev-before-0028.dump` (в том же archive-каталоге). Миграция 0028 применена к локальным test/dev БД. Локальные финансовые данные не сбрасывались; существующий recovery создаёт собственные synthetic записи.

Применимых AGENTS.md в R:\ и R:\TradeSpace при первоначальном обходе не найдено. Пользователь отдельно подтвердил изменения QR-1 в защищённом ядре, API и БД после отклонения первоначального патча автоматической проверкой. Запись выполнена после этого согласования.

## Реализовано

- Отдельный `sbp_qr` / «QR НСПК» для приёма RUB, с явным запретом этого метода в payout. Прежний СБП по телефону сохранён.
- Upload PNG/JPEG, проверка фактического формата/размера/разрешения, локальное декодирование одного QR через zxing-cpp 3.1.1. Поддерживается статический свободносуммовой C2B-профиль НСПК. SSRF по содержимому картинки отсутствует: URL не запрашивается.
- Понятные allowlisted причины отклонения. Исправлено подавление этих сообщений прежним общим mapper: невалидный файл теперь даёт причину, а не generic action_failed.
- QR-метод включён в прежние настройки тарифов; отсутствующий обязательный тариф блокирует выдачу, нулевая ставка не подставляется.
- Оба API используют один QR reservation lock и неизменяемый snapshot. QR_ID сравнивается независимо от кодировки картинки. PostgreSQL unique index не позволяет двум активным заявкам занять один QR и сумму.
- Выданный QR, сумма, валюта, метод и capability token защищены от подмены триггером 0028. Редактирование/архивирование реквизита не заменяет данные уже созданной операции.
- Merchant и Aggregator возвращают общий QR-объект в своих совместимых оболочках; обычные методы сохраняют прежние ответы и финансовые пути.
- Клиентская страница с QR, точной суммой RUB, получателем, банком и deadline. Доступ по случайному 256-bit token, не перебираемому operation UUID; no-store/no-referrer/noindex. Просмотр не меняет состояния/балансы.
- Рабочее место трейдера: выданный QR, ручное подтверждение и QR-only «Не оплачено» с причиной. Закрытие диалога/«Продолжить проверку» оставляет ожидание. По истёкшему QR ручное обычное подтверждение недоступно; требуется существующий ручной разбор.
- SMS и automatic expired-appeal approval не могут оплачивать QR. Штатный staff confirm не заменяет назначенного трейдера; поздние платежи решаются вручную через существующую апелляцию.
- Сохранены действующие комиссии, collateral, Merchant/Aggregator, TeamLead, Rolling, immutable FeeSnapshot, ledger, reconciliation и durable события. Worker retry не повторяет финансовый результат.
- QR-2: только `BankQrProvider` interface, без работающих банковских адаптеров и ключей.

Подробности, запросы, ответы, ошибки, ограничения и ссылки на официальную документацию: [QR1_API.md](QR1_API.md).

## Миграция

`0028_static_nspk_qr` после 0027: Requisite.qr_identity; Deposit.qr_identity/qr_snapshot/qr_token/qr_reserved; unique token; partial unique активного QR+amount+currency; check QR/RUB; immutable snapshot trigger. Триггер снимает QR-slot при terminal state; денежный hold освобождает существующий lifecycle. Downgrade после QR-активности отказоустойчиво запрещён, данные не удаляются.

`alembic check` после применения: PASS, новых необъяснимых операций autogenerate нет. Полная миграция с нуля, уникальность, immutability и запрет destructive downgrade дополнительно проверены на реальном PostgreSQL в `tests/test_qr1_migration.py`.

## Приёмочные сценарии

| Проверка | Фактический результат |
|---|---|
| PNG разных размеров и JPEG одного QR | один QR_ID, 3 cases PASS |
| Неподдерживаемые URL/type/sum/параметры | 13 cases PASS |
| Неизображение, oversize, tiny/huge, GIF, пустая картинка, два QR | 7 cases PASS |
| Collection-only / log redaction | PASS |
| Merchant HMAC create, snapshot, retry, manual paid | PASS |
| Aggregator HMAC create, snapshot, business retry, manual paid | PASS с сохранением прежнего replay-window; см. ограничения |
| Parallel Merchant+Aggregator, одинаковый QR и сумма | один 200 и один 409; без второго резерва |
| Дублирующие записи одного QR | не обходят уникальность |
| Другой трейдер с другим QR | fallback PASS; без кандидатов 409 |
| Разные суммы одного QR | допустимы при лимитах |
| Одновременные идемпотентные Merchant create | одна операция, один hold |
| Отсутствие trader fee | отказ fee_rule_missing |
| Двойное/конкурентное подтверждение | один денежный результат, повтор conflict |
| QR snapshot после редактирования/архивирования реквизита | неизменен в create replay/API/image |
| failed/cancelled/expired и поздняя manual appeal, оба API | 6 cases PASS, hold освобождён, повтор не начисляет |
| Истечение одновременно с confirm | старый terminal failed/trader_timeout, нулевой hold, нет deposit.paid |
| GET просроченного QR | 410 для image; чтение не изменяет pending/QR reservation |
| SMS и автоматический appeal timeout | отклонены/исключены для QR |
| Browser upload + CSRF + чужой owner + QR confirm/decline | оба cases PASS; невалидный файл не создаёт реквизит |
| Rolling и TeamLead, оба API, тариф изменён после create | сохранён FeeSnapshot, ожидаемые начисления и reconciliation PASS |
| Merchant webhook 500 → retry → 204 | PASS, денежные остатки неизменны при retry |
| Aggregator callback 500 → retry → 204 | PASS, HMAC и постоянный idempotency key проверены, третий deliver не отправляет повтор |

39 QR test cases плюс 1 PostgreSQL migration case. Первоначальный полный набор дал 430 PASS и два устаревших ожидания: запрет любого trader decline и список GET routes без ранее добавленного TeamLead estimate. Исправлены только ожидания: non-QR decline всё ещё обязан вернуть 403; presentation остаётся GET/HEAD-only. Целевой повтор после этого — 41 PASS. После сообщений QR, expiry UI и расширения guard — 45 PASS (39 QR + 5 preparation + import/OpenAPI/Jinja). Финальный additive QR status endpoint и повторы без очистки nonce проверены набором QR + migration + Aggregator credentials: 73 PASS.

## Общие проверки

- Полный итоговый pytest: **432 PASS, 10 предупреждений, 881.26 s** (до финального additive поля QR в Aggregator status; после него целевой повтор QR/Aggregator: **73 PASS, 4 предупреждения, 264.76 s**). Один повтор был прерван остановкой локальных процессов и не считается PASS; PostgreSQL восстановился штатно, тесты запущены заново.
- Preflight: **19/19 PASS**, включая live Rapira ask. В логе есть прежнее диагностическое `rolling_rapira_pair_metadata_conflict`, но строгий валидатор выбрал допустимую пару и прошёл.
- HTTP smoke: **4/4 PASS**, повторён при локальных перезапусках.
- Recovery/business: **14/14 PASS**, включая настоящий локальный HTTP 500 → scheduled retry → 204 через Celery, HMAC, отмену, settlement и reconciliation. Settlement здесь — только локальный учёт и synthetic evidence, без blockchain transfer.
- `pip check`: PASS.
- `compileall -q app scripts` и import/OpenAPI/Jinja: PASS.
- `git diff --check`: PASS; Git предупреждает о преобразовании LF/CRLF, без whitespace errors.
- Linux/container build в этой локальной задаче не запускался.

### Golden — точное состояние, без перезаписи эталонов

До поправки устаревшего unit-теста: 23 oracles PASS и 16/16 native snapshots MATCH. На итоговом коде 23 oracles также PASS, но native verify: **15/16**, FAIL для gb-07 и производного index.json. Единственная содержательная разница gb-07 — `/evidence/source_sha256/tests/test_rolling_unit.py` (обновлён stub теста non-QR отказа). Сам TTL oracle `test_ttl_uses_immutable_expires_at` не менялся. Все 16 `business_result`, их статусы и суммы совпадают; это отдельная диагностика, не подмена native verify. Snapshot files и Golden harness не менялись.

Перед публикацией требуется отдельная проверка/утверждение обновления provenance hash GB-07 и его индекса. Финансовые ожидания обновлять не требуется. Не объявлять текущий native Golden полностью зелёным.

### Freeze — разрешённый core change, без обхода

До QR-1 default Freeze уже показывал изменения 4 файлов: presentation/routes, rapira, settlements, web/routes. Для этой задачи сохранён отдельный pre-task hash manifest вне репозитория на 90 защищённых файлов. Default Freeze по прежнему эталону остаётся FAIL (старые + разрешённые QR изменения).

Добавлено покрытие новых `app/services/payment_qr.py` (FZ-05) и `app/web/payment_qr.py` (FZ-18); защита расширена, не ослаблена. Эталон Freeze не перезаписан. Сравнение с pre-task backup и явным override FZ-01,03,04,05,06,08,13,15,16,18: **PASS с OVERRIDE**, 20 добавленных/изменённых protected файлов; не plain baseline match. Rapira, settlement engine и прежний TeamLead estimate этой задачей не изменены.

### Dependency audit — незакрытая проверка

CI-команда `pip_audit --strict --format json .` выполнена отдельным инструментом pip-audit 2.10.1 вне runtime проекта. **FAIL: 12 advisories в существующем PyJWT 2.13.0**; новый zxing-cpp 3.1.1 — 0 findings. PyJWT не обновлялся попутно с QR: отдельный security/dependency review и регрессия authentication необходимы до публикации. Ничего не исключалось из сканирования.

Advisories: CVE-2026-102274, 101917, 102273, 102269, 102267, 102268, 102272, 102271, 102265, 102266, 102270, 101918. В выводе fix_versions — 2.14.0 для первых 11 и 2.15.0 для 101918. Применимость каждого к используемому HS256/JWT потоку отдельно не исследовалась; это findings dependency scanner, а не доказанные exploit-сценарии TradeSpace.

## Браузерная проверка

Открыты фактически: Trader workbench с QR, диалог «Не оплачено» и закрытие без решения, requisites/create form, клиентская QR-страница. Desktop 1280×720, mobile 390×844. QR загружается, точная сумма/банк/получатель/срок видны, horizontal overflow не обнаружен; кнопки достигаются прокруткой/клавиатурой и не требуют сжатой таблицы. Оплата через браузер не выполнялась; финансовые команды проверялись автоматическими тестами на synthetic данных.

Скриншоты вне проекта: `C:\Users\aimwa\.codex\visualizations\2026\09\20\01a0c02e-efc5-7152-a4cc-0bb2f6a34791\qr1-*.jpg`: trader desktop/mobile, upload desktop/mobile, checkout desktop/mobile и mobile actions. Это синтетический неподключённый банковский QR: не оплачивать. Проверены штатный TradeSpace favicon и загрузка картинки на мобильной ширине. При финальном просмотре выявлена существующая тесная колонка средств на 1024–1439px: три суммы помещались в 200px и перекрывали соседние показатели. Колонка заменена строкой на доступную ширину, финансовые значения не менялись. Повторная браузерная проверка: при 1280px ширина панели средств 1198px, суммы не накладываются; при 390px scrollWidth=390 и QR загружен. Скриншоты trader desktop/mobile перезаписаны после исправления. Временный preview работал на 8002 в отдельной test schema, не в пользовательских dev записях. При первой версии fixture возникала правильная reconciliation warning из-за прямого начального баланса без ledger; финальные снимки используют opening ledger, предупреждение отсутствует, product check не отключался.

## Ограничения и QR-2

1. Реальная регистрация QR, владение счётом, bank/receiver identity и получение денег в банке не проверяются. Валидируется поддерживаемый offline формат. CRC — только syntax; bank authenticity не заявляется.
2. Поддерживается строгий long URL-профиль; короткие ссылки без type/bank/crc, типы 02/03, QR с любой встроенной суммой, прочие форматы не поддержаны. Это ограничение первой версии, не утверждение, что все другие банковские QR невалидны.
3. Статический QR нельзя отозвать в банковском приложении. Сохранённая картинка остаётся потенциально оплачиваемой; поздний/повторный перевод — ручная апелляция.
4. Aggregator сохраняет прежний HMAC replay guard: байтово одинаковый запрос в replay-window может получить 409 ещё до business idempotency. Финальный тест сохраняет replay records: проверяет 409 для byte-identical запроса, затем заново подписанный семантически идентичный JSON с другой сериализацией и тот же payment/QR/hold. Отдельно проверен QR в GET status. Это не доказательство immediate byte-for-byte 200 retry. Merchant fresh nonce + idempotency key проверен конкурентно. Для изменения Aggregator replay semantics нужно отдельное решение; в QR-1 защита не обходилась.
5. QR uploads доступны в browser multipart форме; JSON Requisite API не получил binary upload.
6. Общий QR routing lock сериализует QR create в пределах БД. Надёжность проверена конкурентными тестами; production throughput не измерен. Прежние методы этот lock не используют.
7. Банковских интеграций QR-2 нет. Производственная готовность, реальные платежи и банковская доставка не заявляются.

## Локальная ручная проверка

```powershell
cd R:\TradeSpace
.\scripts\recovery_windows.ps1 -Action Start
.\.venv\Scripts\python.exe -m scripts.recovery_run test tests/test_qr1.py tests/test_qr1_migration.py
.\.venv\Scripts\python.exe -m scripts.recovery_run preflight
.\.venv\Scripts\python.exe -m scripts.recovery_run smoke
```

Вход: http://127.0.0.1:8000/trader/login. Используйте существующие локальные credentials, не production. Настройте отдельный QR тариф у участников через staff UI; добавьте PNG/JPEG в «Реквизиты»; создайте sandbox запрос по QR1_API.md; откройте checkout_url. Ручное банковское подтверждение в продукте выполняет трейдер только после фактического поступления; для проверки кода используйте приложенные isolated tests, которые не требуют реального перевода. Отрицательные/поздние кейсы не проверять реальными платежами.

## Изменённые именно этой задачей файлы

- `alembic/versions/0028_static_nspk_qr.py` — новый
- `app/api/v1/aggregator.py`
- `app/api/v1/merchant.py`
- `app/core/enums.py`
- `app/core/logging.py`
- `app/core/payment_methods.py`
- `app/core/requisite_providers.py`
- `app/main.py`
- `app/models/entities.py`
- `app/presentation/tradespace/trader/view_models.py`
- `app/schemas/common.py`
- `app/services/aggregators.py`
- `app/services/appeals.py`
- `app/services/deposit_confirmation.py`
- `app/services/deposit_lifecycle.py`
- `app/services/fee_tiers.py`
- `app/services/payment_qr.py` — новый
- `app/services/requisites.py`
- `app/services/sms.py`
- `app/static/tradespace/css/app.css`
- `app/static/tradespace/payment-qr.css` — новый
- `app/templates/tradespace/payment_qr.html` — новый
- `app/templates/tradespace/trader/_requisite_fields.html`
- `app/templates/tradespace/trader/requisites.html`
- `app/templates/tradespace/trader/workbench.html`
- `app/web/payment_qr.py` — новый
- `app/web/routes.py`
- `app/web/ui_messages.py`
- `docs/QR1_API.md` — новый
- `pyproject.toml`
- `requirements.lock`
- `scripts/platform1_freeze_guard.py`
- `tests/test_migrations_postgres.py`
- `tests/test_platform1_preparation.py`
- `tests/test_qr1.py` — новый
- `tests/test_qr1_migration.py` — новый
- `tests/test_rolling_unit.py`
- `tests/tradespace/test_wave1_presentation.py`
- `docs/QR1_REPORT.md` — новый отчёт.

Чужие pre-existing изменения перечислены в исходном status.txt отдельно. Собственные временные preview-схемы удалены, основной local runtime оставлен работающим на 8000. Полный diff относительно старого Git HEAD включает их и поэтому не является diff QR-1. Task-specific file hashes/changes находятся вне проекта в task-files.json.

## Итог и следующий шаг

Локальный QR-1 flow реализован; приёмка ограничена перечисленными доказательствами. Публикация/production не разрешалась и не выполнялась. Перед последующей публикацией остаются native Golden provenance review, утверждение нового Freeze baseline по разрешённым изменениям и устранение/оценка dependency advisories. Immediate identical Aggregator replay имеет прежнее ограничение, поэтому строгую приёмку требования «любой повтор немедленно вернёт 200 с тем же QR» нельзя объявить закрытой.
