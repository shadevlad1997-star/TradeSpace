# QR‑1.2 — локальное принятие QR‑1 / QR‑1.1

Дата: 01.10.2026. Каталог: `R:\TradeSpace`. Ветка: `codex/qr1-static-nspk`.
Исходный HEAD: `8b88337e55eb5d8b6d3a3d2ac78e7ed8943d0279`.

Статус итоговых проверок: **ПРИНЯТО: native Golden 16/16 PASS; native Freeze 93/18 PASS; локальная фиксация разрешена**.

## Границы и сохранность

Это ревью и принятие существующей реализации. Исполняемый код, зависимости, миграции, формы, финансовые формулы и API в QR‑1.2 не менялись. QR‑2, банковские адаптеры и пересмотр Aggregator replay contract не начаты. Push, merge, deploy, активация Production и реальные переводы не выполнялись. Отдельный исходный проект не изменялся. Пользовательские dev-данные не сбрасывались; тесты использовали штатные изолированные fixture, recovery — новые synthetic записи.

Применимых AGENTS.md в `R:\` и дереве проекта не обнаружено. Прочитаны QR1_REPORT.md, QR1_1_REPORT.md, CI, pytest configuration, native guards, реальные services/routes/models/migration и изменённые тесты. Отчёт QR‑1.1 остаётся историческим: его FAIL Golden/Freeze заменяются результатами этого принятия, а не переписываются задним числом.

До изменений сохранены 336 файлов и сведения о 4 прежних удалениях, branch/HEAD/status, binary diff рабочего дерева и index:
`R:\TradeSpace-Archive\qr1_2-before-20261001-001312`.
Файлы `manifest.json`, `tracked.diff`, `index.diff`, `status-porcelain.txt` описывают именно входное состояние. Секретные ignored env, базы, uploads, logs и venv в исходный набор копий не включались. Базы не переносились в Git.

Разделение происхождения:

- До QR: архив `qr1-before-20260930/files`, status и freeze-pre-task; ранее принятые кабинеты, identity/auth, логотип, подписи/форматирование, Rapira и TeamLead estimate. Эталонные старые байты 22 изменённых native Freeze-файлов дополнительно сверены с `TradeSpace-Publish/tradespace-0027-20260924` по SHA256; все совпали со старым эталоном.
- QR‑1: сравнение pre-QR с `qr1_1-before-20260930/files`, сверка `task-files.json`: метод/0028, QR upload/snapshot/выдача, public checkout, API и guards.
- QR‑1.1: `manifest.json`, `task-only.diff`, `task-changed.json` того же архива: 33 файла; ручные споры, запрет decline, tariff return, формы и PyJWT. Итоговые hashes всех файлов текущего входного состояния совпали с восстановленным final manifest QR‑1.1, **0 неизвестных различий**.
- QR‑1.2: только `.gitattributes`, этот отчёт, Freeze JSON и три Golden JSON. Общий огромный diff к историческому HEAD не является diff этой задачи.

Машинные доказательства текущего ревью вне проекта: `reviewed-freeze-candidates.json` (старый/current/pre-QR/pre-QR1.1 hash и FZ каждого файла), `native-core-reviewed.diff` (полный нормализованный diff к проверенным старым байтам), `golden-exact-diff.json`, итоговые task-only diff/manifests и логи.

## Результат чтения кода

### Команды, споры и права

1. `app/web/routes.py::cabinet_decline_deposit` допускает только Admin/Superadmin **до чтения Deposit**. Исключения для QR/Trader/operator нет; проверены обычный и realm-prefixed POST для всех четырёх методов. UI без кнопки decline не является единственной защитой.
2. `refer_appeal_to_staff` блокирует Deposit → Appeal и перечитывает обе строки через `populate_existing`. Затем повторно проверяет активного actor, assignment и статус `opened`. Ответ «Платёж не найден» сохраняет причину/автора/время/доказательства, AppealMessage и audit, ставит `in_review`. Нет debit/credit, освобождения hold, финального payment outcome или webhook/callback.
3. `approve_expired_appeals` теперь только эскалирует: после lock повторяет проверку статуса/deadline, пишет audit/message, возвращает пустой список событий. `automatic=True` в сервисе подтверждения всегда отклоняется. Историческое имя worker и его период 60 секунд сохранены.
4. Trader/operator может подтвердить только свою `opened` апелляцию. После `in_review` — только Admin/Superadmin. Финальный reject/close/return для Deposit проходит административный guard внутри locked service. Support/TeamLead/Merchant не получили новых прав. API resolver дополнительно защищён `require_roles`; browser paths сохраняют session/CSRF/2FA.
5. Обычное подтверждение проверяет статус и наличие active Appeal. SMS запрещён для QR и для активного спора. Обычный decline/finalization запрещён при `appeal_opened`; TTL-worker выбирает только created/pending. Paid/terminal Deposit с активной апелляцией не превращается worker в новый финансовый результат. Исключительный repair-путь предварительно собирает блокирующие финансовые/appeal evidence и не является обычным decline.
6. API открытия апелляции блокирует operation, перечитывает статус, повторно проверяет duplicate active Appeal; browser открытия уже держит Deposit lock. Решения используют тот же порядок Deposit → Appeal. Финальный статус повторно проверяется после ожидания lock; проигравший запрос не проводит вторые деньги.

Источники: `app/services/appeals.py`, `deposit_confirmation.py`, `deposit_lifecycle.py`, `sms.py`; `app/api/v1/{admin,cabinet,merchant}.py`; `app/web/routes.py`; `app/workers/celery_app.py`.

### Финансовые состояния и события

- Известный previous paid либо уже paid Deposit: только решение апелляции/аудит. Не повторяются debit, merchant credit, комиссии, TeamLead, Rolling; нет ложного нового `deposit.paid` или callback. Погашенный Rolling не открывается повторно.
- Previous pending/created и освобождённые failed/cancelled/expired: при найденном платеже используется **сохранённый FeeSnapshot**, а не новый тариф. Проверяются сумма/валюта/метод/executor и история hold. Active hold потребляется; излишний исторический gross hold освобождается; released hold требует достаточных available funds. Неизвестное previous состояние/snapshot/reserve не угадываются: отказ и ручной разбор.
- Merchant-origin: debit gross минус сохранённая комиссия трейдера. Aggregator-origin: прежний gross debit трейдера, executor fee принадлежит агрегатору. Этот различающийся контракт не унифицирован механически.
- Ручное approve атомарно через nested transaction, затем прежние функции settlement, TeamLead, Rolling и partner outbox. Fault injection после финансовых записей обязан откатывать весь результат.
- Reject апелляции не равен отмене платежа: восстанавливается известный previous статус без повторного освобождения денег. Для unchanged terminal/paid события нет; реальный переход `appeal_opened → pending` даёт согласованное `deposit.pending`. Callback синхронизируется через прежний Aggregator mapping.
- Важное уточнение исторического отчёта: browser merchant-open сохраняет paid, но переводит и прочие terminal Deposit в `appeal_opened`; API open оставляет terminal статус. Оба сохраняют previous status. Это существующее различие представления, не новая математика. Reject в browser-варианте может дать событие фактического восстановления terminal-статуса; утверждение «нет события при неизменном terminal» относится к действительно неизменному состоянию, а не ко всем открытиям спора вообще.

### QR, резерв и тарифы

`lock_qr_routing` — общий transaction advisory lock для Merchant и Aggregator **до routing/idempotent create**, только для sbp_qr. Подбор под этим lock исключает занятый `identity + amount + RUB`, затем использует прежние ограничения/достаточность денег и ищет другой подходящий QR. Последний барьер — partial UNIQUE index в 0028, общий для обоих API. Отдельная запись Requisite с тем же QR не обходит ограничение; разные суммы допустимы по прежним лимитам.

Изображение локально декодируется/проверяется, не загружается по QR URL. Сохранённый snapshot, token, amount/currency/method защищены триггером 0028 от изменения. Изменение реквизита не переписывает выданную заявку. Terminal освобождает QR-slot; денежный hold обрабатывается существующим lifecycle. Public GET/token/image не подтверждает/не истекает операцию, использует no-store и 410 после окончания доступности.

QR добавлен в перечни поддерживаемых методов и тарифов, не в payout. Merchant/trader/aggregator fee rule обязателен; неоднозначный/отсутствующий rule или отрицательная margin блокируют выдачу. Формулы `create_operation_fee_snapshot` не менялись; финансовые поля защищены прежними ORM/DB immutable checks (0010/0011). Новые тарифы влияют только на будущие заявки.

### Проверка исправленных assertions

| Файл | Что действительно изменено | Что осталось обязательным |
|---|---|---|
| `test_acceptance_financial_postgres.py` | Reject исполняет созданный настоящий Admin; отдельно проверен запрет Trader | Два конкурентных решения: одно успешное; exact hold/balance/merchant/event; отсутствие дубля debit |
| `test_final_acceptance_combined_postgres.py` | При решении по unchanged terminal ожидается ноль новых событий | Полное `after == before`, кроме оправданного нового payment; money/journals/TeamLead/Rolling/audit/retry assertions сохранены |
| `test_appeal_confirmation_rule_postgres.py` | Недопустимый auto approval заменён manual fault injection; deadline ожидает in_review | Полный rollback после позднего сбоя, повторное ручное проведение, paid/Rolling без денег/событий, reconciliation |
| `test_qr1.py` | Новый label и manual-only ошибка; QR decline → 403/pending/reservation retained | CSRF, ownership, финансовые суммы, snapshots, replay, webhook/callback и конкурентность сохранены |
| `test_rolling_unit.py` | Убрана временная QR-поблажка; снова безусловный trader decline 403 даже с db=None | Проверка серверного отказа до доступа к БД |
| `test_qr1_1.py` | 30 исполняемых тестов нового контракта | Четыре метода × Trader/operator; escalation, Admin/Superadmin, ownership, stale actions, гонки, tariff snapshot, upload и JWT |

Новые правила не подтверждаются заранее записанными результатами Golden: выполнены реальные PostgreSQL/HTTP регрессии. Они уже обязательны для CI: `.github/workflows/release-gates.yml` запускает весь `pytest -q`, `pyproject.toml` содержит `testpaths = [tests]`, QR suites не исключены и не skipped. Дополнительный декларативный snapshot нового спора вместо теста не создавался; harness не ослаблен.

## Все 25 расхождений native Freeze

Для каждой строки решение — **принять**, после указанного чтения и связанных исполняемых проверок. Формулы и неизвестные изменения в rebaseline не добавлялись.

| Файл | FZ / происхождение | Назначение и результат ревью / защита тестом |
|---|---|---|
| `alembic/versions/0028_static_nspk_qr.py` | 01 / QR‑1 | Snapshot fields, active QR+amount+currency UNIQUE, immutable trigger; nondestructive downgrade. Real PG migration/constraint test PASS |
| `app/api/v1/admin.py` | 18 / QR‑1.1 | Сообщение блокирует Deposit→Appeal; terminal не открывается заново. Resolver guard/locked service; QR dispute tests PASS |
| `app/api/v1/aggregator.py` | 15 / QR‑1 | QR в существующем status-response; auth/replay неизменны. Both-origin/read/replay test PASS |
| `app/api/v1/cabinet.py` | 18 / QR‑1.1 | Operation lock/refresh перед duplicate active check и previous snapshot. Ownership guard сохранён; HTTP appeal tests PASS |
| `app/api/v1/merchant.py` | 04 / QR‑1 | Shared QR lock, snapshot, issued details в create/replay. Merchant HMAC/parallel create PASS |
| `app/core/enums.py` | 01 / QR‑1 | Только additive sbp_qr; прежние статусы не переименованы. Schema/payout exclusion PASS |
| `app/core/logging.py` | 03 / QR‑1 | Редактирование capability token в логируемом URL. QR redaction test PASS |
| `app/core/payment_methods.py` | 16 / QR‑1 + 1.1 | Код/option/label QR NSPK; старые aliases сохранены. Four-method/labels tests PASS |
| `app/core/requisite_providers.py` | 16 / QR‑1 | QR использует существующий bank directory; не новый банк/адаптер. Routing/upload PASS |
| `app/main.py` | 18 / QR‑1 | Подключение read-only public QR router. Import/OpenAPI и token GET PASS |
| `app/models/entities.py` | 01 / QR‑1 | Модель соответствует 0028, token/partial uniqueness/checks. Real migration и image parity PASS |
| `app/presentation/tradespace/routes.py` | 03 / до QR | Read-only TeamLead estimate: только staff+TeamLead+2FA, no-store, 503 при плохом курсе; не создаёт баланс. Scoped/read-only tests PASS |
| `app/schemas/common.py` | 18 / QR‑1 + 1.1 | Additive collection method/labels, payout validator запрещает QR. Both API + payout exclusion PASS |
| `app/services/aggregators.py` | 04,15 / QR‑1 | Shared lock, saved snapshot, QR response; прежняя экономика/executor. Cross-API race, saved-fee, retry PASS |
| `app/services/appeals.py` | 13 / QR‑1 + 1.1 | Manual-only resolution, refreshed locks, role/owner/state guards, nonfinancial escalation, no false paid event. Paid/unpaid/terminal/rollback/concurrency/insufficiency PASS |
| `app/services/deposit_confirmation.py` | 08 / QR‑1 + 1.1 | QR назначенному trader/operator; обычный confirm не обходит спор; release QR после paid. Race/HTTP/appeal PASS |
| `app/services/deposit_lifecycle.py` | 08 / QR‑1 + 1.1 | QR slot release и запрет ordinary finalization appeal_opened; hold/TTL formulas неизменны. Expiry/late appeal PASS |
| `app/services/fee_tiers.py` | 06 / QR‑1 | Только sbp_qr в разрешённых tier methods, расчёт/snapshot unchanged. Mandatory fee, tariff change/old snapshot PASS |
| `app/services/payment_qr.py` | 05 / QR‑1 + 1.1 | Новый offline validator, snapshot, advisory lock, shared availability и labels. Image negatives/identity/races PASS |
| `app/services/rapira.py` | 10 / до QR | Per-loop request lock, cooldown 5–30 s; прежние ask parser/freshness/cache и Decimal. 20 concurrent reads/9 failures/recovery PASS |
| `app/services/requisites.py` | 05 / QR‑1 | Отдельный QR method, shared lock, занятая identity проверяется до старых eligibility/hold. Cross-API duplicate/fallback/amount tests PASS |
| `app/services/settlements.py` | 12 / до QR | Только fetched_at/remaining validity в read quote; debit/hold/комиссия/rounding unchanged. Decimal metadata и native settlement oracles PASS |
| `app/services/sms.py` | 13 / QR‑1 + 1.1 | Запрет QR и active Appeal до денег; не внедрён новый SMS финансовый путь. QR rejection tests + code review |
| `app/web/payment_qr.py` | 18 / QR‑1 | Opaque-token readonly page/image, no-store/no-referrer/noindex/410. API/public GET tests PASS |
| `app/web/routes.py` | 18 / до QR + QR‑1 + 1.1 | Rate freshness/no-store; validated upload/edit under ownership/CSRF; decline only staff; trader response service; tariff return network/tab/id. Rate, upload, CSRF, role, dispute and tariff tests PASS |

Предшествующий TeamLead estimate отдельно прочитан: Decimal, fee берётся из `TEAMLEAD_SETTLEMENT_FEE_USDT`, отдельное ROUND_HALF_UP для RUB request/fee, поиск максимально оплачиваемых USDT cents; нет повторного вычитания frozen и нет записи в БД. Tests охватывают zero/fee-only/fractional/boundary, устаревший курс, отсутствие balance-row и чужие роли. UI/логотип/подписи не откатывались.

Native baseline принят штатным `python -m scripts.platform1_freeze_guard --record-baseline` после сравнения кандидатов с review manifest. Сохранены все **90** прежних файлов; добавлены 0028, payment_qr service и public route: **93 файла / 18 областей**. AREAS/guard code в этой задаче не менялись, `--allow` не использовался. `baseline_commit` указывает исторический HEAD до checkpoint, это provenance источника, а не самоссылочный SHA будущего коммита.

## Точный Golden diff

Штатный `--record` допускается только после машинной проверки: 16/16 `business_result` неизменны; меняются ровно следующие четыре JSON-значения:

| Файл / JSON path | До | После |
|---|---|---|
| gb‑09 `/evidence/source_sha256/tests/test_acceptance_financial_postgres.py` | `fc58421607d5567b99dccca3289f830e5d7479663d6257634c67ab36fbf0044b` | `60bca1ff07510f3d0d32dac6de30b2299c1a8debf0c7a0682eb867d176654658` |
| gb‑10 тот же path | `fc58421607d5567b99dccca3289f830e5d7479663d6257634c67ab36fbf0044b` | `60bca1ff07510f3d0d32dac6de30b2299c1a8debf0c7a0682eb867d176654658` |
| index `/scenario_sha256/GB-09` | `db5fc14ee396af45fe6319d17ac48f0528f1199c53e86dcec4683d6af1940686` | `e79fa283c5ce740375f530bca00418f281d2f23873e6bcdf8c14bf336a8dda73` |
| index `/scenario_sha256/GB-10` | `aa2e0e5e952ba0eac3623131f7dbf061d7251a71b4631ef3f4edcaab02132d5e` | `dd0fb11e1f4b4d55b13383d92cd05b2eca59c7cefb3323bd7afb18eba6f8aa25` |

Остальные 14 scenario JSON совпадают побайтно. Старые суммы, статусы, 9 AUTOMATED / 7 PARTIALLY AUTOMATED, gaps, normalization и набор сравниваемых сценариев не менялись. Record и последующий verify исполняют настоящие 23 pytest oracles; декларативная часть Golden не объявляется полноценным trace новых QR-споров.

## Проверки и привязка к состоянию

Все новые результаты ниже относятся к неизменённым app/tests/dependencies из final manifest QR‑1.1. После них менялись только baseline JSON, этот отчёт и Git attributes.

| Проверка | Фактический результат / evidence в архиве QR‑1.2 |
|---|---|
| Целевой PostgreSQL suite | **134 passed**, 1 warning, 393.86 s; `targeted.log`, exit 0; команды полностью в checks.json |
| Native Freeze, без override | **PASS, 93/18**, `freeze-native.log`; перед принятием сохранён FAIL25 в freeze-before.log |
| Native Golden | **PASS 16/16**, 23 oracles; golden-record.log + golden-native.log; 17 JSON files byte-deterministic |
| Preflight | **19/19 PASS**, live Rapira ask включён, current=head 0028; preflight.log |
| HTTP smoke | **4/4 PASS**, smoke.log |
| Recovery/business | **14/14 PASS**, recovery.log; реальные локальные HTTP500 → scheduled retry → 204, подпись, reconciliation, synthetic settlement |
| Compile Python + import/OpenAPI/Jinja | PASS: compileall app/scripts/tests; целевой import/template тест включён в 134 |
| Dependency consistency / package resources | pip check PASS; verify_package_install PASS |
| Docker/current bytes | **189/189 совпадают**, image-parity.json; app/alembic + pyproject/lock. Сеть контейнера отключена, БД не подключалась |
| Полный suite QR‑1.1 | **462 passed**, 10 warnings, 934.54 s — ранее выполнен, не называется новым прогоном. final manifest подтверждает тождественность всех исполняемых файлов/тестов |
| Linux build, clean migrations, dependency audit | Предыдущие evidence QR‑1.1 сохранены: 92 Linux cases + финальные 30 QR1.1, clean 0001→0028/no drift, точный lock 68 packages/0 advisories. Не выдаются за новый запуск или актуальную проверку изменившейся внешней vulnerability DB |
| Staged tree / secret scan / whitespace | 338/338 staged blobs совпадают с worktree; native Freeze на staged export PASS 93/18; gitleaks 8.30.1 (redacted, project config) — 0 findings; artifact policy PASS. Whitespace — см. честное разделение ниже. |

Текущий QR‑1.2 target запуск включает целиком test_qr1, test_qr1_1, test_qr1_migration, test_appeal_confirmation_rule_postgres, test_final_acceptance_combined_postgres, test_acceptance_financial_postgres, test_merchant_rate_widget, test_platform1_preparation; отдельно TeamLead estimate math/authorization/read-only и import/OpenAPI/Jinja. Новых тестов и ослабленных assertions в QR‑1.2 нет.

Первый `golden-before` исполнил 23/23 oracles, но вышел с ошибкой отсутствия `cancellation` recovery evidence: параллельный recovery ещё дописывал свой JSON. Этот запуск **не PASS**; после завершения recovery дальнейшие Golden запускались последовательно. Диагностика полного JSON подтвердила только четыре provenance значения выше. Ошибка не скрывалась обновлением business_result.

Полный тяжёлый набор повторно не запускался по условию задания: app/tests/dependencies не изменены, проверены относящиеся oracles/guards. Linux image не пересобирался по той же причине. Проверен существующий `tradespace-qr11:local`, ID `sha256:7d09ef1eda81e0d8e18b4072d185ead6182eaddb38e3d91fe4abcb312f060d48`; он содержит приложение QR‑1.1, а не новые acceptance docs/Git metadata.

## Локальный checkpoint и Git filters

Системный `core.autocrlf=true` мог изменить accepted bytes при staging/последующем checkout. Добавлен ровно существующий `GIT_ATTRIBUTES` из scripts/prepare_repository.py: `* -text whitespace=trailing-space,space-before-tab,cr-at-eol`, `*.sh text eol=lf`. Это уже принятая проектом политика хранения, не отключение Freeze и не правка core ради EOL. Она устраняет ложное восприятие CRLF как trailing whitespace; проверки настоящих trailing spaces/blank-at-EOF остаются.

Checkpoint включает **предшествующую принятую реализацию TradeSpace**, QR‑1, QR‑1.1 и шесть QR‑1.2 файлов; не приписывает всю рабочую копию QR‑1.2. Историческая Git history сохраняется. Identity автора берётся из прежней локальной publish-копии владельца, только для команды commit. Remote не добавляется. Никакой публикации.

Файлы QR‑1.2:

1. `.gitattributes` — сохранение принятых исходных байтов.
2. `tests/golden/freeze_zone_baseline.json` — 25 проверенных различий, покрытие 93/18.
3. `tests/golden/snapshots/gb-09.json` — source provenance.
4. `tests/golden/snapshots/gb-10.json` — source provenance.
5. `tests/golden/snapshots/index.json` — два производных hash.
6. `docs/QR1_2_ACCEPTANCE.md` — этот отчёт/передача.

## Сохранённые ограничения и handoff

- Это принятие локальной основы, **не разрешение production release** и не доказательство реального банковского поступления.
- Static QR валидируется offline в поддерживаемом long-URL/type01/free-amount профиле; CRC syntax не доказывает регистрацию, владельца или получателя. Сохранённое изображение не отзывается на стороне банка. Поздние/повторные переводы требуют ручного разбора.
- Aggregator byte-identical replay по прежнему может дать 409 в replay-window; business idempotency проверена отдельным допустимым подписанным запросом. Этот контракт не менялся; банковских адаптеров нет.
- Эскалация — scheduled batch до 200 opened записей, период 60 секунд; нет обещания синхронного перехода в точный deadline. Пока locked state ещё opened, назначенный Trader может успеть вручную подтвердить; после in_review запрещено. Запоздавший либо продлённый timer требует операционного контроля, не автоодобрения.
- Исторические имена approve_expired_appeals/trader_rejected и недостижимые automatic-ветви после unconditional refusal сохранены. Не делался косметический рефакторинг.
- Новая браузерная/банковская ручная приёмка в QR‑1.2 не выполнялась; ранее полученные screenshots и владелец UI approval не объявляются новым browser QA.
- Golden сохраняет семь явно частичных сценариев; новые финансовые правила проверены исполняемыми тестами вне декларативных business_result старого snapshot.
- До будущего push потребуется отдельное разрешение и свежие CI/security/release gates. Новая история/remote/сервер не создавались.

Блокирующих необъяснимых code/financial расхождений по результату данного ревью не обнаружено. Итоговая готовность локальной основы к отдельной разработке QR‑2: **YES**. QR‑2 автоматически не начинать.

## Whitespace и состав: дополнительные результаты перед checkpoint

Первый staging выявил 102 cached blobs с LF вместо фактически принятого CRLF. Все различия проверены как EOL-only. `git add --renormalize -- .` применил уже добавленную политику attributes к index; рабочие файлы не переписывались. После этого **338/338** staged blobs побайтно совпали с текущими файлами, **93/93** protected hashes совпали с Freeze. Guard отдельно запущен из экспортированного index (`freeze-staged.log`), не только из worktree.

`git diff --cached --check` к старому историческому HEAD: **exit 2, 16 унаследованных предупреждений**, не PASS. Это 15 прежних blank-at-EOF и trailing whitespace в `alembic/versions/0001_initial.py:3`. Для каждого файла доказано побайтное тождество pre-QR1.2 backup. Полный список — staged-whitespace.log. Они report-only как известное форматирование первого импорта принятой локальной основы, по ранее утверждённой политике; ради косметики историческая миграция/core не менялись. Эта оговорка не означает зелёный CI при публикации данной исторической ветки: её будущий parent-patch whitespace gate потребует отдельного учёта истории импорта. Публикации здесь нет. Новые QR‑1.2 изменения проверены строго; конфигурация CI/whitespace scanner не ослаблена.

Первый directory secret scan с абсолютными путями дал два срабатывания на существующий публичный ENCRYPTION_KEY fixture CI/Compose: точные path-правила конфигурации не совпали с абсолютным префиксом. Повтор из корня проверяемого дерева с относительными путями и неизменной `.gitleaks.toml` — 0 findings. Это не новые исключения или сокрытие находок. Финальный scan выполнен именно по выгруженным staged blobs; env, dumps, uploads, логи, venv, приватные ключи и архивы в них отсутствуют.

## Проверка checkpoint перед QR‑2.0 (2026-10-01)

Локальная фиксация QR‑1.2 фактически завершена: `1dd4ed8728c2526742dd94da91650019cb9d1368`, ветка `codex/qr1-static-nspk`, tree `ee80be8dca9222f87b1e4201e48972867e9817eb`. Перед QR‑2.0 `git status --short` пустой; незакоммиченных изменений нет. Повторный checkpoint или переписывание истории не требовались. QR‑2.0 начинается отдельной веткой `codex/qr2-bank-drafts` от этого SHA. Эта запись только документирует уже существующий коммит и не изменяет результаты приёмки QR‑1.2.
