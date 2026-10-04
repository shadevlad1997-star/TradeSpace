# QR-1.1 — ручное подтверждение, административный разбор и завершение проверки QR

Дата: 30.09.2026. Локальная рабочая копия: `R:\TradeSpace`. Без commit, push, deploy, реальных платежей и QR-2.

## Исходное состояние и границы

Ветка `codex/qr1-static-nspk`, HEAD `8b88337e55eb5d8b6d3a3d2ac78e7ed8943d0279`. HEAD не описывает весь текущий продукт: до задачи уже существовали многочисленные tracked/untracked изменения, включая QR-1, миграцию 0028, кабинеты и предыдущие исправления. Они сохранены. Состояние перед QR-1.1, копии файлов, SHA256 и status находятся в `R:\TradeSpace-Archive\qr1_1-before-20260930`. Список ниже сравнивается именно с этим состоянием, а не выдаёт весь diff к старому HEAD за эту задачу.

Исходный отдельный проект не редактировался. В QR-1.1 нет миграции, изменения enum/схемы, финансовых формул, HMAC/replay, тарифных алгоритмов, округления, ledger, TeamLead или Rolling. Изменены правила допуска к командам споров и значение таймера: автоматическое одобрение заменено передачей администрации для всех методов. Это намеренное изменение поведения по заданию, а не только UI. Таймер выполняется существующим worker раз в 60 секунд (и существующим кабинетным вызовом), а не синхронно в точный момент deadline. Ручной ответ разрешён только пока после захвата lock состояние ещё `opened`; если эскалация уже выиграла блокировку, Trader не может завершить `in_review`. Нового SLA или wall-clock запрета до выполнения задачи не вводилось.

## Реализовано

- Обычный trader/operator decline запрещён до чтения заявки; QR-исключение удалено. Удалены кнопка «Не оплачено» и её диалог. Admin/Superadmin не могут обойти активный спор обычным decline; используется решение спора.
- «Платёж не найден» теперь сохраняет ответ, автора, время, причину и доказательства и переводит `opened → in_review`. Никаких денежных записей или окончательных partner events на этом шаге.
- Срок ответа передаёт открытый спор администрации. Автоматическое одобрение запрещено и в сервисе. Сохранены совместимые имя worker `approve_expired_appeals` и расписание; возвращаемый счётчик событий равен нулю, поскольку финансовых событий нет.
- Закрытие/возврат/отклонение через общий resolver остаётся finance-aware и требует Admin/Superadmin. Обычное подтверждение, SMS и decline не обходят активный спор. Создание спора, сообщение администратора и решения используют порядок блокировок Deposit → Appeal; состояние перечитывается после lock.
- В интерфейсе и public schema label — **QR NSPK**, код `sbp_qr` неизменен; payout не расширен этим методом.
- Форма реквизита включает QR-upload только для QR NSPK. Для остальных методов upload скрыт и disabled; требуемые поля переключаются по методу. При редактировании отсутствие нового файла сохраняет расшифрованный payload и identity; ciphertext может меняться из-за обычного шифрования.
- Суперадмин использует существующие тарифы через «Тарифы · QR NSPK». Исправлен return URL после сохранения: фактический раздел `network` с нужными `tab/id`, вместо русского названия раздела, дававшего 403. Тарифы и FeeSnapshot не переписаны.
- PyJWT обновлён 2.13.0 → 2.15.0 синхронно в pyproject и lock. Другие зависимости не обновлялись попутно.

Дополнительные подтверждённые UI-дефекты: CSS `display:grid` показывал поля с `hidden` — исправлено узко для форм реквизитов; новый успешный ответ спора не входил в allowlist сообщений и приводил к `ui_error` — добавлены точные сообщения; после передачи администрации таймер ошибочно показывал «Истёк» — теперь «Ответ передан». В staff история `trader_rejected` отображается как позиция трейдера и проверка администрации, а не окончательное решение.

## Роли и команды

| Действие | Назначенный Trader / operator | Чужой Trader | Merchant | Support / TeamLead | Admin / Superadmin |
|---|---|---|---|---|---|
| Обычное подтверждение своей заявки | Существующая команда; QR вручную | Нет | Нет | Нет | Существующие ограничения; обычный QR запрещён |
| Обычный decline | Нет для всех 4 методов | Нет | Не этот endpoint; свой cancel сохраняется | Нет | Да, вне активного спора |
| Открыть спор | Существующие права не расширены | Нет | Своя операция, существующий путь | По прежним read/write правам | По прежним правам |
| Подтвердить открытый спор | Да, пока `opened` и своя заявка | Нет | Нет | Нет | Да |
| Сообщить «Платёж не найден» | Да, `opened`; с прежними требованиями к причине/файлу | Нет | Нет | Нет | Рассмотрение через staff-команды |
| Решить `in_review` | Нет | Нет | Нет | Нет | Подтвердить / окончательно отклонить |
| Финальный reject/close/return | Нет | Нет | Нет | Нет | Да |
| Изменить merchant QR tariff | Нет | Нет | Нет | Нет | Только Superadmin по прежнему guard |

Проверка роли выполняется сервером, в том числе внутри locked resolution service, а не только скрытием кнопок. Требуются действующая, не заблокированная и не архивная учётная запись и соответствующая ownership. Обязательные CSRF/session/2FA не изменялись.

## Переходы и финансовые эффекты

Клиентская QR-страница не сообщает серверу «я оплатил». Merchant открывает обращение существующим `POST /api/v1/cabinet/appeals` с проверкой принадлежности операции. Есть прежние browser пути открытия спора; обычный TTL не создаёт Appeal. У Aggregator не добавлялся отдельный endpoint открытия спора: его Deposit разбирается существующим ручным административным процессом. Финансовые тесты проверяют оба происхождения Deposit.

| До | Команда | После | Деньги и события |
|---|---|---|---|
| Обычная created/pending | Системный срок истёк | Deposit `failed` по существующему timeout | Прежнее однократное освобождение hold/QR; Appeal не создаётся |
| Обычная заявка | Trader decline | 403; без изменения | Нет |
| Известное прежнее состояние Deposit | Создание допустимого спора | Appeal `opened`; незавершённый Deposit → `appeal_opened`, terminal Deposit сохраняет статус; прежнее состояние записано | По прежнему пути открытия; не новый платёж |
| Appeal opened | Trader: платёж найден | Appeal approved; Deposit paid | Если ещё не был paid — прежний saved-snapshot финансовый путь ровно один раз |
| Appeal opened | Trader: платёж не найден | Appeal in_review; Deposit не меняется | Нет нового ledger, освобождения hold, webhook/callback |
| Appeal opened | Истёк срок ответа | Appeal in_review; audit + message | Нет нового финансового результата |
| Appeal in_review | Старое действие trader/обычное confirm/SMS | Отказ; проверка остаётся | Нет |
| Appeal opened/in_review | Admin/Superadmin approve | Appeal approved, Deposit paid | Один результат; либо финансовый no-op для известного paid |
| Appeal opened/in_review | Admin/Superadmin reject | Appeal rejected; восстанавливается известное прежнее состояние Deposit | Само отклонение не является cancel и не освобождает hold второй раз |
| Завершённый спор | Повтор или проигравшее конкурентное решение | Конфликт | Нет повторных денег/событий |
| Прежнее состояние неизвестно | Финальное решение | Отказ для ручного разбора | Не трактуется как unpaid |

Отказ по спору, открытому из pending, возвращает pending. Обычный timeout после этого может завершить заявку отдельно по своим условиям. Paid остаётся paid; отклонение жалобы не отменяет платёж. Для отменённой/истёкшей заявки поздний платёж требует явного ручного подтверждения, достаточного остатка и существующего восстановления Rolling. Не сопоставляется автоматически с новой заявкой только по сумме.

Merchant-origin использует сохранённую комиссию трейдера: net debit = gross − executor fee; active hold потребляется без двойного списания, лишний старый gross hold освобождается существующим путём. Если hold released, применяется debit available с проверкой остатка. Aggregator-origin сохраняет прежний gross debit трейдера: executor fee относится к агрегатору, не ошибочно к трейдеру. Merchant credits, platform margin, TeamLead, Rolling и reconciliation идут через прежние функции.

Если до спора было paid (включая погашенный Rolling), только решение/аудит спора: нет второго debit, fee, TeamLead, Rolling recovery, merchant credit или ложного `deposit.paid`. Для нового подтверждённого платежа — прежний durable webhook и синхронизация Aggregator payment/callback в транзакции. HTTP delivery отдельно. При отказе нет финального события от позиции трейдера; событие фактического restored Deposit status создаётся только при реальном изменении статуса, без нового paid event. Подписи/headers неизменны. Merchant status отражает Deposit. Сохранённый Aggregator mapping: created → created, pending/appeal_opened → waiting_payment, paid/failed/expired/cancelled → одноимённый статус. `in_review` принадлежит Appeal и не подменяет payment outcome. Существующий GET Aggregator status синхронизирует payment перед ответом; его прежние внутренние side effects не перепроектировались. В create-response состояние денег — `payment_status` (а `status=success` служебный), в GET — `status`.

## QR-1: сохранённые защиты

Тестами охвачены конкуренция Merchant/Aggregator за QR identity + amount + RUB, дубликаты одного QR в разных реквизитах, fallback на другой QR, разные суммы, идентичность независимо от PNG/JPEG, immutable snapshot/trigger, CSRF/ownership, отсутствие server fetch QR URL, image validation, public token/no-store/410 и запрет QR SMS/automatic approval. Mandatory fee по-прежнему блокирует выдачу, не подставляется 0. Новый тариф меняет только будущие операции (пример теста: старая fee 100; новая 120.12 при 12% от 1001; SBP 7% сохраняется).

Aggregator replay contract не изменён: byte-identical body в окне replay получает 409 даже с новым request ID; отдельно бизнес-идемпотентность возвращает тот же payment при допустимом заново подписанном семантически идентичном запросе. Использован GET status для чтения. Nonce/replay records не очищались ради тестов.

## Dependency audit — подтверждение первоисточниками

`pip-audit 2.10.1 --strict --format json` воспроизвёл 12 advisories PyJWT 2.13.0. Каждая сверена с API security advisory репозитория разработчика. Итоговый аудит PyJWT 2.15.0: **No known vulnerabilities found**. `pip check` — PASS. Машинные файлы `dependency-before.json`, `dependency-after.json`, `dependency-primary-sources.json` сохранены в архиве задачи; найденные пакеты не исключались. Дополнительно проверен именно полный lock: `pip-audit --strict --no-deps --disable-pip -r requirements.lock --format json --output dependency-lock-final.json` — 68/68 точных версий, 0 advisories. Здесь no-deps запрещает повторное разрешение в более новые версии, а не исключает зависимости: все 68 строк closure перечислены явно. Первый аудит проекта разрешил 67 пакетов (несколько транзитивных версий отличались от lock), поэтому он не подменяет отдельную lock-проверку. Предупреждение инструмента о желательности package hashes записано в `dependency-lock-final.log`; оно не является найденной уязвимостью.

| Advisory (первоисточник) | Уязвимые версии по maintainer | Исправлено |
|---|---|---|
| [CVE-2026-102274](https://github.com/jpadilla/pyjwt/security/advisories/GHSA-w6j9-cwv2-h6wq) | >= 2.9.0, <= 2.13.0 | API: null; исправление 2.14.0 подтверждено changelog |
| [CVE-2026-101917](https://github.com/jpadilla/pyjwt/security/advisories/GHSA-2gx3-rcp4-g85q) | <= 2.13.0 | 2.14.0 |
| [CVE-2026-102273](https://github.com/jpadilla/pyjwt/security/advisories/GHSA-w2cx-738m-mc7w) | = 2.13.0 | 2.14.0 |
| [CVE-2026-102269](https://github.com/jpadilla/pyjwt/security/advisories/GHSA-hxm8-2xgr-2p9m) | <= 2.13.0 | 2.14.0 |
| [CVE-2026-102267](https://github.com/jpadilla/pyjwt/security/advisories/GHSA-9v7f-9g4p-ffgj) | <= 2.13.0 | 2.14.0 |
| [CVE-2026-102268](https://github.com/jpadilla/pyjwt/security/advisories/GHSA-ffc3-869f-jxw9) | <= 2.13.0 | 2.14.0 |
| [CVE-2026-102272](https://github.com/jpadilla/pyjwt/security/advisories/GHSA-r6x4-923q-g947) | 2.13.0 | 2.14.0 |
| [CVE-2026-102271](https://github.com/jpadilla/pyjwt/security/advisories/GHSA-p4g4-x82p-q773) | 2.13.0 | 2.14.0 |
| [CVE-2026-102265](https://github.com/jpadilla/pyjwt/security/advisories/GHSA-8wjv-2p76-3863) | ==2.13.0 | 2.14.0 |
| [CVE-2026-102266](https://github.com/jpadilla/pyjwt/security/advisories/GHSA-9j54-fg26-wv3r) | 2.13.0 | 2.14.0 |
| [CVE-2026-102270](https://github.com/jpadilla/pyjwt/security/advisories/GHSA-jwrc-g2q2-pq5p) | <= 2.13.0 | >=2.14.0 |
| [CVE-2026-101918](https://github.com/jpadilla/pyjwt/security/advisories/GHSA-42vr-xj54-vc7v) | >= 2.0.0a1, <= 2.14.0 | 2.15.0 |

Для первой записи поле patched_versions в API не заполнено: это не скрыто, исправление сверено по [официальному changelog](https://pyjwt.readthedocs.io/en/stable/changelog.html). Для всех 12 нужен минимум 2.15.0 из-за последней записи. Приложение использует HS256, не JWKS; отсутствие вызова JWK не выдается за исправление пакета. Добавлены valid/expired/unsigned/wrong-algorithm/tampered/malformed/deeply-nested JSON JWT проверки. Проверки не принимают RecursionError/500 вместо invalid token.

## Проверки итогового состояния

| Проверка | Итог / доказательство |
|---|---|
| Полный Windows PostgreSQL suite | **462 passed**, 10 warnings, 934.54 s; `full-accepted.log`, `test-results/recovery-pytest.xml`. Финальные production и test files; без skipped/failed |
| QR / споры в Linux | 92 passed на финальном production-коде; после расширения operator coverage отдельно 30/30 `test_qr1_1.py` |
| Новый QR-1.1 suite | 30 сценариев: 8 trader/operator decline; 8 method × admin decision; 4 timeout; ownership/race; tariff; edit-no-file; 7 JWT |
| HTTP smoke | 4/4 PASS после запуска текущего кода |
| Preflight | 19/19 PASS, включая actual live Rapira ask; 0028 current=head |
| Recovery/business | 14/14 PASS; local HTTP 500 → реальный Celery scheduled retry → 204; подпись проверена |
| Linux build | PASS, штатный Dockerfile/requirements.lock, Python 3.12; `zxingcpp`, `app.main`, JWT 2.15.0 import |
| Clean Linux migrations | 0001 → 0028 PASS в отдельной пустой `qr11_clean_final`; `alembic check`: No new upgrade operations detected |
| Linux process/HTTP | Uvicorn на loopback 18002; health/ready/version и Trader/Merchant/Staff login — 6/6 HTTP 200 |
| Image/source parity | 187 app/alembic files SHA256 identical, 0 differences |
| Dependency audit | 0 known vulnerabilities; pip check PASS |
| Compile | `python -m compileall -q app scripts tests` PASS |
| Whitespace | `git -c core.whitespace=cr-at-eol diff --check` PASS; обычный `git diff --check` — **FAIL (exit 2)** на CRLF строках старого root-import; `cr-at-eol` — **PASS (exit 0)** без изменения Git config. Отдельно все добавленные/изменённые строки task-only diff проверены на trailing spaces/tabs — 0 |

Итоговый image: `tradespace-qr11:local`, SHA256 `7d09ef1eda81e0d8e18b4072d185ead6182eaddb38e3d91fe4abcb312f060d48`. QA compose-project `tradespace-qr11-qa` имеет изолированную БД/Redis, production secrets не используются. После проверки только его контейнеры остановлены. Основное приложение на 127.0.0.1:8000 оставлено работающим.

Команды (из `R:\TradeSpace`, Python `.venv\Scripts\python.exe`):

```text
python -m scripts.recovery_run test -vv --tb=short
python -m scripts.recovery_run preflight
python -m scripts.recovery_run smoke
python -m scripts.recovery_smoke
python -m scripts.platform1_golden --verify
python -m scripts.platform1_freeze_guard
python -m scripts.platform1_freeze_guard --baseline R:\TradeSpace-Archive\qr1_1-before-20260930\freeze-pre-task.json --allow FZ-05 --allow FZ-08 --allow FZ-13 --allow FZ-16 --allow FZ-18 --reason "Owner QR-1.1: manual dispute review, no trader decline, QR NSPK labels and tariff form return. No financial formula change."
```

Docker команды и весь вывод — `docker-verified-final.log`, `docker-qr11-final-expanded.log`, `docker-clean-migrations-final.log`, `docker-http-final.json`, `docker-source-parity-final.json` в архиве задачи. Штатный compose дополнен только внешним QA override с read-only mount tests и loopback HTTP-port. Dockerfile не изменялся в QR-1.1.

Ранее полный прогон: 456 passed / 2 failed за 907.05 s. Причина обоих падений — старое ожидание повторного partner event для неизменного terminal Deposit; **не финансовый diff**. После исправления assertions выполнен отдельный полный прогон на финальном наборе тестов: **462 passed** (добавлены 4 operator cases). Другие assertions не ослаблялись. Предупреждения FastAPI ORJSON/Alembic config — существующие deprecation; предупреждение SHA512 возникает только в отрицательном тесте запрещённого алгоритма.

Неудачные или прерванные более ранние запуски не засчитываются. В частности, запуск после остановки локальных служб не дошёл до pytest из-за connection timeout. PostgreSQL завершила штатный crash recovery; сброс БД или отключение reconciliation не применялись. Windows test-DB suites выполнялись последовательно, Docker — в отдельной БД без опубликованного DB-порта.

## Фактическая браузерная проверка

Реальное локальное приложение, синтетический изолированный fixture, desktop 1440×900 и mobile 390×844. Фикстура создавала opening ledger обычной командой, reconciliation не отключался. Выполнены реальные действия только с тестовыми записями:

- Workbench: QR NSPK, только подтверждение, нет кнопки/диалога decline; desktop/mobile.
- Список и форма реквизитов: sbp, c2c, mobile_commerce, sbp_qr; корректные hidden/disabled/required поля. Существующий QR без файла сохраняется. Staff QR-upload также показан.
- Спор: «Платёж не найден», отправка причины, статус «Проверка администрации»/«Ответ передан», история ответа; без ложного ui_error.
- Staff: очередь in_review, детальная история и фактическое финальное отклонение синтетического спора.
- Superadmin: «Тарифы · QR NSPK», сохранение 12% по диапазонам, возврат 200 и повторное открытие значений, desktop/mobile.
- Mobile: горизонтальное переполнение на проверенных экранах отсутствует; вывод ширины контента 375 при viewport 390. Проверка не является заявлением о полном browser QA всех кабинетов.

Скриншоты (локальные, не credentials): `C:\Users\aimwa\.codex\visualizations\2026\09\20\01a0c02e-efc5-7152-a4cc-0bb2f6a34791`:
`qr11-workbench-desktop.png`, `qr11-workbench-mobile.png`, `qr11-form-desktop.png`, `qr11-form-mobile.png`, `qr11-dispute-desktop.png`, `qr11-not-found-mobile.png`, `qr11-review-mobile.png`, `qr11-staff-review-desktop.png`, `qr11-staff-decision-mobile.png`, `qr11-staff-form-desktop.png`, `qr11-tariff-final-desktop.png`, `qr11-tariff-final-mobile.png`.

[Workbench desktop](C:/Users/aimwa/.codex/visualizations/2026/09/20/01a0c02e-efc5-7152-a4cc-0bb2f6a34791/qr11-workbench-desktop.png) · [Передача спора mobile](C:/Users/aimwa/.codex/visualizations/2026/09/20/01a0c02e-efc5-7152-a4cc-0bb2f6a34791/qr11-review-mobile.png) · [Тариф QR NSPK desktop](C:/Users/aimwa/.codex/visualizations/2026/09/20/01a0c02e-efc5-7152-a4cc-0bb2f6a34791/qr11-tariff-final-desktop.png) · [Тариф mobile](C:/Users/aimwa/.codex/visualizations/2026/09/20/01a0c02e-efc5-7152-a4cc-0bb2f6a34791/qr11-tariff-final-mobile.png).

## Golden / Freeze: без подмены результата

Native Freeze: **FAIL**, 25 protected-file differences к штатному принятому baseline (включают предшествующий QR-1 и прежние ещё не принятые изменения). Полный список: `freeze-native-final.log`. Машинный old/new hash и признак изменения именно QR-1.1 — `freeze-differences-final.json`.

| Файл для отдельного принятия native Freeze | Области | Изменён в QR-1.1? |
|---|---|---|
| `alembic/versions/0028_static_nspk_qr.py` | FZ-01 | Нет, предшествовал задаче |
| `app/api/v1/admin.py` | FZ-18 | Да |
| `app/api/v1/aggregator.py` | FZ-15 | Нет, предшествовал задаче |
| `app/api/v1/cabinet.py` | FZ-18 | Да |
| `app/api/v1/merchant.py` | FZ-04 | Нет, предшествовал задаче |
| `app/core/enums.py` | FZ-01 | Нет, предшествовал задаче |
| `app/core/logging.py` | FZ-03 | Нет, предшествовал задаче |
| `app/core/payment_methods.py` | FZ-16 | Да |
| `app/core/requisite_providers.py` | FZ-16 | Нет, предшествовал задаче |
| `app/main.py` | FZ-18 | Нет, предшествовал задаче |
| `app/models/entities.py` | FZ-01 | Нет, предшествовал задаче |
| `app/presentation/tradespace/routes.py` | FZ-03 | Нет, предшествовал задаче |
| `app/schemas/common.py` | FZ-18 | Да |
| `app/services/aggregators.py` | FZ-04, FZ-15 | Нет, предшествовал задаче |
| `app/services/appeals.py` | FZ-13 | Да |
| `app/services/deposit_confirmation.py` | FZ-08 | Да |
| `app/services/deposit_lifecycle.py` | FZ-08 | Да |
| `app/services/fee_tiers.py` | FZ-06 | Нет, предшествовал задаче |
| `app/services/payment_qr.py` | FZ-05 | Да |
| `app/services/rapira.py` | FZ-10 | Нет, предшествовал задаче |
| `app/services/requisites.py` | FZ-05 | Нет, предшествовал задаче |
| `app/services/settlements.py` | FZ-12 | Нет, предшествовал задаче |
| `app/services/sms.py` | FZ-13 | Да |
| `app/web/payment_qr.py` | FZ-18 | Нет, предшествовал задаче |
| `app/web/routes.py` | FZ-18 | Да |

К pre-task QR-1.1 snapshot: **PASS WITH EXPLICIT OVERRIDE**, 93 protected files / 18 areas. Только 10 изменённых protected files в разрешённых областях:

| FZ | Файлы QR-1.1 | Причина |
|---|---|---|
| FZ-05 | `app/services/payment_qr.py` | Только пользовательское QR NSPK название |
| FZ-08 | `app/services/deposit_confirmation.py`, `app/services/deposit_lifecycle.py` | Активный спор не обходится обычным подтверждением/финализацией |
| FZ-13 | `app/services/appeals.py`, `app/services/sms.py` | Ручной разбор, права, эскалация; запрет автоматического обхода |
| FZ-16 | `app/core/payment_methods.py` | Название метода |
| FZ-18 | `app/api/v1/admin.py`, `app/api/v1/cabinet.py`, `app/schemas/common.py`, `app/web/routes.py` | Locked dispute commands, QR label, запрет decline, корректный tariff return |

Native Golden: **FAIL (exit 1)**. Его независимые 23/23 pytest oracles прошли за 37.87 s; сравнение сообщает 3 изменённых JSON-файла: `gb-09.json`, `gb-10.json`, `index.json`. Остальные 14 scenario JSON совпадают полностью. Лог — `golden-final.log`, XML — `test-results/platform1-golden-pytest.xml`.

Декларативный `business_result` одинаков для 16/16. Точное сравнение файлов: 14/16 scenario snapshots совпадают; GB-09 и GB-10 отличаются **только** `/evidence/source_sha256/tests/test_acceptance_financial_postgres.py`; их hashes в `index.json` также меняются. `golden-differences-final.json` содержит старые/новые hash и JSON paths. Это provenance mismatch, но он не замалчивается и не переименован в native PASS.

Golden фиксирует декларативный нормализованный business contract и hashes файлов тестов; совпадение `business_result` само по себе не заменяет выполнение тестов. Изменение правила таймера и прав на финальный отказ проверено отдельными QR-1.1 regression, но старые 16 сценариев не моделируют весь этот новый contract. Существующие automation gaps остаются честно описанными baseline; они не устранялись переписыванием snapshot.

Guard/harness/snapshots не редактировались. Для будущего отдельного утверждения нужны: ревью QR-1 diff и QR-1.1 protected-file diff, принятие нового ручного lifecycle и тестов, обновление только соответствующих provenance hashes/index и расширение покрытия новых сценариев, затем повторный native прогон. В этой задаче rebaseline не выполнен.

## Файлы именно QR-1.1

Всего 33 файла: 31 изменён относительно сохранённого pre-task состояния; 2 новых.

- `app/presentation/tradespace/staff/forms.py` — изменён.
- `app/presentation/tradespace/staff/view_models.py` — изменён.
- `app/presentation/tradespace/trader/view_models.py` — изменён.
- `app/services/payment_qr.py` — изменён.
- `app/static/tradespace/css/app.css` — изменён.
- `app/static/tradespace/js/shell.js` — изменён.
- `app/templates/tradespace/layouts/app.html` — изменён.
- `app/templates/tradespace/payment_qr.html` — изменён.
- `app/templates/tradespace/staff/page.html` — изменён.
- `app/templates/tradespace/trader/_requisite_fields.html` — изменён.
- `app/templates/tradespace/trader/disputes.html` — изменён.
- `app/templates/tradespace/trader/workbench.html` — изменён.
- `docs/QR1_API.md` — изменён.
- `requirements.lock` — изменён.
- `tests/test_acceptance_financial_postgres.py` — изменён.
- `tests/test_appeal_confirmation_rule_postgres.py` — изменён.
- `tests/test_final_acceptance_combined_postgres.py` — изменён.
- `tests/test_qr1.py` — изменён.
- `app/api/v1/admin.py` — изменён.
- `app/api/v1/cabinet.py` — изменён.
- `app/core/payment_methods.py` — изменён.
- `app/schemas/common.py` — изменён.
- `app/services/appeals.py` — изменён.
- `app/services/deposit_confirmation.py` — изменён.
- `app/services/deposit_lifecycle.py` — изменён.
- `app/services/sms.py` — изменён.
- `app/templates/cabinet.html` — изменён.
- `app/web/routes.py` — изменён.
- `app/web/ui_messages.py` — изменён.
- `pyproject.toml` — изменён.
- `tests/test_rolling_unit.py` — изменён.
- `tests/test_qr1_1.py` — новый.
- `docs/QR1_1_REPORT.md` — новый.

`task-only.diff` и `task-changed.json` в архиве отделяют эту работу от ранее существовавшего общего Git diff. В тестах `test_acceptance_financial_postgres.py` final reject теперь выполняет настоящий Admin, дополнительно проверяется отказ Trader. `test_final_acceptance_combined_postgres.py` требует нулевых новых событий при неизменном terminal payment, а все денежные и исторические сравнения сохранены. Запрет trader decline в `test_rolling_unit.py` восстановлен в пред-QR1 виде, без расслабления assertions.

## Ограничения и передача

- Native Golden/Freeze остаются отдельными release gates: override не означает обычный PASS. Пока они не приняты/обновлены отдельным решением, **production-ready: NO**.
- QR проверяется локально по формату и изображению. CRC syntax не подтверждает регистрацию/владение банковским QR; клиент проверяет получателя в банковском приложении. Сохранённый внешний статический QR технически нельзя отозвать на стороне банка.
- Aggregator replay semantics выше сохранены; endpoint открытия спора агрегатору не добавлялся. Реальный банк, QR-2, внешняя доставка и реальные платежи не проверялись и не запускались.
- В сервисе сохранены исторические имена `approve_expired_appeals` и `trader_rejected`. Их новые допустимые эффекты описаны выше. Недостижимые старые ветви automatic после явного отказа не удалялись попутным refactor.
- Ручная приёмка старого интерфейса владельцем не объявляется повторной приёмкой изменённых QR-1.1 экранов. Для владельца к проверке: новая форма QR, передача спора, решение администрации, тарифы и отсутствие decline.

Локальный запуск оставлен для проверки пользователя: http://127.0.0.1:8000/trader/login. Финальный повторный smoke 4/4 PASS. Git/сервер/Production не публиковались. QR-2 не начат.

Итоговые `git status --short` и `git diff --stat` сохранены как `git-status-final.txt` и `git-diff-stat-final.txt` в архиве. Общий tracked diff к старому HEAD: 84 файла, +7441/−7435; он включает ранее существовавшие работы и не является размером QR-1.1. Untracked файлы перечислены в status; точный состав этой задачи — 33 файла выше. Ветка и HEAD не изменились.

После перезапуска локального приложения повторное открытие уже существующей error-вкладки было отклонено URL-policy браузерного инструмента. Обход не выполнялся. Приведённые выше screenshots/действия получены ранее в этой же задаче на финальном production-коде; после них менялись только тестовые ожидания и документация. Доступность перезапущенного приложения подтверждена HTTP smoke, а не повторным browser QA.
