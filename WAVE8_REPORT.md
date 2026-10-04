# TradeSpace — Wave 8: функциональная регрессия и исправления интерфейса

Дата: 23 сентября 2026. Каталог: `R:\TradeSpace`.
Ветка: `codex/tradespace-wave1`. HEAD: `8b88337e55eb5d8b6d3a3d2ac78e7ed8943d0279`.

**Итог: исправления кода и автоматическая регрессия выполнены. Полная визуальная приёмка НЕ завершена. Серверный этап НЕ начат.**

## Исходное состояние и безопасность

Перед изменениями прочитаны `WAVE7_HANDOFF.md` и внешний `WAVE7_IDENTITY_REPORT.md`, проверены команды и Freeze. Применимых AGENTS.md в проекте и родительском каталоге не найдено. Начальное дерево содержало 70 строк short status: 53 tracked изменения и 17 untracked групп/путей. Это результаты предыдущих задач, а не изменения Wave 8.

Начальные status и SHA-256, а также копия git-visible файлов сохранены во внешнем архиве `R:\TradeSpace-Archive\wave8-20260923`: `target-status-before.txt`, `target-before.json`, `source-status-before.txt`, `source-before.json`, `before/`. Локальные секреты в копию не включались. Source workspace не редактировался: все 225 ранее зафиксированных файлов совпадают по SHA-256. Его pre-existing middleware/rate-limit/seed и документы сохранены.

Freeze при входе и после изменений: **81 защищённый файл / 18 зон, PASS**. Ни один protected file не изменён этой задачей. Не менялись модели, миграции, schema, финансовые services, API, persisted states, HMAC/headers, auth/CSRF/session/2FA или права ролей. В staff/access и staff/forms изменялись только подписи и пояснения; команды, поля, scopes и суммы прежние.

Wave 7 имела незавершённый визуальный остаток, включая operator, выключенный флаг и финальный просмотр wallet/Rolling/AI. Существенных незавершённых миграций контрактов или схемы при этой проверке не выявлено. Её network-dependent проверка курса теперь успешно повторена.

## Четыре замечания

| Замечание | Подтверждённая причина | Исправление | Доказательство / предел проверки |
|---|---|---|---|
| Значок вкладки | В HTML не было явного favicon link. Собственный SVG уже существовал, но браузер мог использовать ранее сохранённый implicit favicon | Общий `head_icons.html` с `/static/tradespace/brand-mark.svg?v=8`; подключён ко всем standalone документам, включая login, ошибки и одноразовый секрет. Layout наследуют все кабинеты | Четыре реальных localhost login и `/favicon.ico` отдают текущий SVG TradeSpace. MIME и байты совпадают; HTTP cache revalidation проверена. Фактическая вкладка браузера после очистки кэша НЕ просмотрена |
| Статусы и кнопки | `failure_reason` попадал в Trader/Merchant/Staff как машинный код; неизвестные статусы могли выводиться напрямую. Бейдж был nowrap, кнопки имели разные переносы и высоту строки | Display-only `reason_label`, полные словари состояний и безопасный fallback. `merchant_cancelled` → «Отменено мерчантом». Общие line-height, padding, vertical-align, ограничение ширины и перенос длинных надписей; отдельная строка причины под бейджем | HTTP/DB тест: Trader history/detail, Merchant detail и три staff роли; исходный код причины и балансы неизменны. Геометрия на реальных viewport пока НЕ подтверждена |
| Стрелка «Ещё» | Текстовый символ `⌄` зависел от метрик шрифта; summary не занимал высоту навигации | Общая SVG-стрелка, центрирование, высота summary и поворот при открытии. Аналогично стрелка аккаунта. Native details/summary сохранены | Все шесть ролей рендерят общий компонент; 4 JS unit tests проверяют Escape, outside click, возврат фокуса, mobile Tab trap и снятие scroll lock. Реальная мышь/клавиатура/touch НЕ проверены |
| Лишние надписи | Header narration, placeholder Wave/Foundation, английские realm/workspace и разработческие пояснения присутствовали в HTML | Удалены из разметки; не скрыты CSS. Локализованы обычные подписи и ошибки. Placeholder прямо сообщает о недоступном разделе, без имитации работы | 47 GET страниц шести ролей проверены на интерфейсную копию, общий icon/menu и отсутствие записей в БД |

**Проверка независимости бренда Wave 7 до этого исправления была незакрыта:** implicit favicon/cache и фактическое отображение вкладки не были надёжно проверены. Не утверждаем, что найден старый SVG в текущих assets: таких файлов сейчас нет; замечание пользователя касается отображения браузером.

Дополнительно исправлены подтверждённые по коду аналогичные дефекты:

- сырой status в служебном read-only просмотре трейдера;
- fallback сырых статусов в TeamLead и staff, включая before/after в истории;
- устаревшее упоминание «предыдущего кабинета» в описаниях выдачи/смены секрета агрегатора;
- технические подписи Merchant payout/settlement/Rolling/API и сообщение недоступного курса;
- постоянные декоративные индикаторы «live» и «непрочитано», не подкреплённые данными, удалены;
- перенос суммы и текста в operation cards, выравнивание причины отмены, наследование шрифта select/textarea;
- явный focus outline для кнопок/summary/select и размер мобильных touch targets.

Новые визуальные дефекты по скриншотам не заявляются: браузерного наблюдения в Wave 8 не было.

## Роли, права и страницы

Ниже PASS означает автоматические реальные HTTP/ASGI запросы с PostgreSQL, а не просмотр браузером.

| Роль | Вход и назначенный кабинет | Навигация и доступ | Функциональные доказательства |
|---|---|---|---|
| Trader | PASS, trader realm | 8 GET разделов; чужие записи/разделы закрыты | Рабочая очередь, история, реквизиты, деньги, споры, подтверждение и отсутствие GET мутаций; локализованная отмена |
| Merchant | PASS, merchant realm | 9 GET разделов, включая уведомления; ownership/foreign ID 404 | Операции/payout, балансы, Rolling, settlement, API и Webhook; идемпотентность и no-secret leakage |
| TeamLead | PASS, staff realm | 6 GET разделов; отдельного auth realm не добавлено; staff control запрещён | Назначения, два источника начислений, debt-first, сторно, расчёты и обязательная 2FA |
| Support | PASS, staff realm | 8 основных GET; дополнительные tabs проверены по allowlist | Нет финансовых/административных прав сверх существующих; запрещённые POST дают 403 |
| Admin | PASS, staff realm | 8 GET; проверены прямые tabs/details и недоступные root функции | Существующие команды и CSRF; financial visibility без расширения scope |
| Superadmin | PASS, staff realm | 8 GET и все разрешённые tabs | Детали средств/комиссий, расчёты, ключи/одноразовые секреты, wallet/Rolling/AI HTML tests |

Полная suite также покрывает operator и aggregator. `test_wave7_identity.py` повторно проверяет все восемь backend roles, оба значения флага и direct entry/role/legacy/security URLs. Прежние cabinet templates не рендерятся. Ссылка отката в AccountMenu отсутствует. `TRADESPACE_UI_ENABLED=false` остаётся экраном приостановки TradeSpace, как установлено Wave 7.

Security: действующие realm cookies, logout с CSRF, смена identity вкладки (409/no-store), отсутствие авторизации через скрытие кнопок, TOTP gates и одноразовое потребление секретов остаются прежними. Тесты контролируют masking/no-store/no secret in cookie. Существующие security-sensitive обработчики не менялись.

Пустые списки, role-forbidden, identity changed, недоступный курс, невыпущенные ключи и maintenance проверены существующими/добавленными HTML tests. Фальшивая надпись «Async state boundary готов» удалена. Долгая загрузка страницы и реальный focus/scroll в браузере остаются частью незавершённого visual QA.

## Результаты команд

| Проверка | Итог | Доказательство во внешнем архиве |
|---|---|---|
| Presentation PostgreSQL | **40 passed**, 1 warning | `presentation.log` |
| Full PostgreSQL | **247 passed**, 2 warnings | `full-postgres-final.log`, `.xml` |
| Menu event-handler unit tests | **4/4 PASS** | `menu-interactions.log` |
| Golden первый verify | **16/16 MATCH**, 17 executable oracles PASS | `golden-first.log` |
| Golden второй verify | **16/16 MATCH**, 17 executable oracles PASS | `golden-second.log` |
| Freeze | **81 files / 18 areas PASS**, без override | `freeze-final.log` |
| Deterministic preflight | **19/19 PASS**, локальный тестовый rate provider | `preflight-local.log` |
| External preflight | **19/19 PASS**, реальный rate provider | `preflight-external.log` |
| HTTP smoke после перезапуска API | **4/4 PASS** | `http-smoke.log` |
| Live recovery/business | **14/14 PASS**, реальные localhost API/worker/receiver, внешний курс | `recovery-external.log`, `.json` |
| Deterministic recovery/business | **14/14 PASS**, отдельный API 8003 и локальный курс | `recovery-local.log`, `.json` |
| Webhook | **500 → scheduled retry → 204**, HMAC и отдельный signing key, attempts записаны; проверено в обоих recovery runs | Те же recovery логи |
| Python compilation | **PASS** | `compilation-final.log` |
| git diff --check | **PASS** | `diff-check-final.log` |

Использованные команды: `.venv\Scripts\python.exe -m scripts.recovery_run test tests/tradespace --tb=short`, `... -m scripts.recovery_run test --tb=short`, `... -m scripts.platform1_golden --verify` (дважды), `... -m scripts.platform1_freeze_guard`, `... run/wave4_preflight.py`, `... -m scripts.recovery_run preflight`, `... -m scripts.recovery_run smoke`, `... -m scripts.recovery_smoke`, внешний `deterministic_recovery.py`, `node --test tests/tradespace/shell_interactions.test.cjs`, compileall, `git diff --check`.

Начальные запуски выявили только устаревшие UI assertions: server-permissions/TeamLead-workspace, Merchant finance/settlement headers, unavailable live quote. Они исправлены на актуальные пользовательские названия, без удаления проверок статусов HTTP, scope, сумм, idempotency и изменений БД. Итог 247 = прежние 234 + 13 новых Python test cases; четыре JS tests считаются отдельно. Предупреждения прежние: ORJSONResponse deprecation и Alembic path_separator.

## Финансовый baseline и Golden

В пределах исполняемых сценариев сохранены creation/routing, FeeSnapshot, trader hold/debit/release, merchant payable/fees, platform margin, TeamLead accrual/debt/reversal/settlement, Rolling FIFO/overflow/release, payout creation/reservation, settlement service completion/rejection, HMAC replay/idempotency, webhook headers/payload/signatures и reconciliation. Повторные запросы и защита от двойного финансового эффекта проверяются существующими финансовыми тестами. Реальных банковских/блокчейн переводов не выполнялось; evidence переводов синтетическое.

**Не заявляется, что все 16 сценариев полностью автоматизированы.** Прежняя полнота Golden сохранена: **9 AUTOMATED / 5 PARTIALLY AUTOMATED / 2 MANUAL**. GB-03, 06, 07, 13, 15 остаются частичными; GB-09 и 10 — ручными. Их automation_gap не менялся. В частности, нет полного golden terminal payout matrix и полного специализированного appeal accept/reject money/event snapshot. Проверка этих baseline gaps не подменялась придуманным результатом.

Golden provenance: в `tests/test_settlement_hardening_postgres.py` изменена ровно одна строка текста UI assertion: локализован заголовок unavailable-rate. Ни один financial assertion не изменён. Поскольку Golden хеширует весь файл теста, в GB-12/13/16 обновлён только `evidence.source_sha256` этого файла, а в index — соответствующие digest. Скрипт отдельно доказал, что после обратной подстановки старого source hash JSON идентичен прежнему. **business_result, automation, automation_gap, денежные значения и сам harness/normalization неизменны.** `--record` не запускался; Freeze baseline не менялся. Доказательство: `golden-provenance-review.json`.

## Найденная локальная проблема данных

Фоновый expiration многократно сообщает `invalid trader frozen amount` для существующей синтетической `VISUAL-W2-PENDING`, созданной **2026-09-22 14:25 UTC**, до Wave 8. Read-only диагностика: требуется hold **11 941,20 RUB**, у связанного трейдера хранится **0,00 RUB**. Guard отказывается списывать отсутствующий резерв; заявка остаётся pending. Ошибку нельзя выдавать за успешно завершённую expiration.

Это не операция новых recovery runs: оба новых сценария завершились 14/14 и прошли reconciliation. Запись, балансы и financial guard НЕ исправлялись. Подробности: `preexisting-local-expiry-data.json`. Причина первоначального появления несогласованной visual fixture полностью не установлена; требуется отдельное решение по локальным тестовым данным. Перенос текущей dev DB как готовых серверных данных не подтверждён.

Документированные ранее browser settlement rejection defect, callback mismatch и ограничения generic appeal не исправлялись и не объявляются устранёнными. Кнопка проблемного отказа в settlement остаётся gated; пользовательское предупреждение сохранено.

## Identity и visual QA

Проверены фактические current files и имена, включая source/templates/static/config/docs/tests, читаемые runtime logs и локальные config. Найдено **0 совпадений старого бренда**; никаких новых regex allowlist, обфускации или склеивания старого имени не добавлено. `app/static` содержит один пользовательский брендовый SVG TradeSpace и vendor Swagger assets. Manifest/apple/app icon файлов нет; не добавлялась вымышленная PWA. `/favicon.ico` — существующий HTTP маршрут, возвращающий тот же SVG с корректным MIME, не старый файл ICO.

Область сканирования и числа записаны в `identity-and-safety-final.json`: за пределами current product files остаются исторический `.git`, внешние runtime junctions/venv и bytecode/test caches. Эти исключения относятся к metadata/runtime, а не к найденным совпадениям продукта. Удалённое ранее старое изображение ещё видно как deletion в git diff к историческому HEAD; файл в текущем дереве отсутствует, Git history не переписывалась.

**Visual QA Wave 8: BLOCKED / NOT PERFORMED.** Для всех шести ролей desktop, tablet и mobile остаются непроверенными в текущем ходе. Нет ни одного нового screenshot «до/после» или скриншота исправленного экрана. Computer Use после открытия отдельного браузера остановил работу: не удалось достоверно определить текущий URL Windows для применения правил. Повторные UI попытки и альтернативный обход ограничения не выполнялись. Снимки Wave 7 не выдаются за новые результаты.

| Роль | Desktop >=1440 | Tablet 768–1023 | Mobile <768 |
|---|---|---|---|
| Trader | BLOCKED | BLOCKED | BLOCKED |
| Merchant | BLOCKED | BLOCKED | BLOCKED |
| TeamLead | BLOCKED | BLOCKED | BLOCKED |
| Support | BLOCKED | BLOCKED | BLOCKED |
| Admin | BLOCKED | BLOCKED | BLOCKED |
| Superadmin | BLOCKED | BLOCKED | BLOCKED |

Программные изменения затрагивают responsive CSS и проверены HTML/unit tests. Это не доказательство отсутствия overflow, качества выравнивания, tab focus, touch и кэша favicon в реальном браузере. До/после по коду сохранены в `before/` и `wave8-only.diff`.

## Файлы этой задачи

Ниже перечислены именно отличия от сохранённого worktree при входе Wave 8. Общий git diff к HEAD содержит предыдущие работы и не является diff этой задачи.

### Presentation projections и подписи

- `app/presentation/tradespace/labels.py`
- `app/presentation/tradespace/merchant/view_models.py`
- `app/presentation/tradespace/navigation.py`
- `app/presentation/tradespace/staff/access.py`
- `app/presentation/tradespace/staff/forms.py`
- `app/presentation/tradespace/staff/view_models.py`
- `app/presentation/tradespace/teamlead/view_models.py`
- `app/presentation/tradespace/trader/view_models.py`

### Общие стили и поведение меню

- `app/static/tradespace/css/app.css`
- `app/static/tradespace/css/polish.css`
- `app/static/tradespace/js/shell.js`

### Шаблоны

- `app/templates/tradespace/auth/login.html`
- `app/templates/tradespace/auth/one_time_secret.html`
- `app/templates/tradespace/components/head_icons.html`
- `app/templates/tradespace/components/ui.html`
- `app/templates/tradespace/layouts/app.html`
- `app/templates/tradespace/merchant/_components.html`
- `app/templates/tradespace/merchant/analytics.html`
- `app/templates/tradespace/merchant/disputes.html`
- `app/templates/tradespace/merchant/finance.html`
- `app/templates/tradespace/merchant/integration.html`
- `app/templates/tradespace/merchant/operation.html`
- `app/templates/tradespace/merchant/operations.html`
- `app/templates/tradespace/merchant/overview.html`
- `app/templates/tradespace/merchant/settlements.html`
- `app/templates/tradespace/pages/placeholder.html`
- `app/templates/tradespace/pages/security.html`
- `app/templates/tradespace/pages/shell.html`
- `app/templates/tradespace/staff/page.html`
- `app/templates/tradespace/staff/trader_preview.html`
- `app/templates/tradespace/states/forbidden.html`
- `app/templates/tradespace/states/session_changed.html`
- `app/templates/tradespace/teamlead/_components.html`
- `app/templates/tradespace/teamlead/accruals.html`
- `app/templates/tradespace/teamlead/overview.html`
- `app/templates/tradespace/teamlead/settlements.html`
- `app/templates/tradespace/trader/_components.html`
- `app/templates/tradespace/trader/_requisite_fields.html`
- `app/templates/tradespace/trader/analytics.html`
- `app/templates/tradespace/trader/disputes.html`
- `app/templates/tradespace/trader/finance.html`
- `app/templates/tradespace/trader/history.html`
- `app/templates/tradespace/trader/requisites.html`
- `app/templates/tradespace/trader/workbench.html`

### Presentation tests

- `tests/tradespace/shell_interactions.test.cjs`
- `tests/tradespace/test_wave1_presentation.py`
- `tests/tradespace/test_wave3_merchant.py`
- `tests/tradespace/test_wave5_staff.py`
- `tests/tradespace/test_wave8_regression.py`

### UI assertion в финансовом тесте

- `tests/test_settlement_hardening_postgres.py`

### Только provenance Golden

- `tests/golden/snapshots/gb-12.json`
- `tests/golden/snapshots/gb-13.json`
- `tests/golden/snapshots/gb-16.json`
- `tests/golden/snapshots/index.json`

### Отчёты и handoff

- `WAVE8_REPORT.md` — этот отчёт.
- `WAVE8_HANDOFF.md` — точное состояние и незакрытая приёмка.
- `WAVE7_HANDOFF.md` — добавлено уточнение favicon и ссылка на новый handoff.
- Внешний `WAVE7_IDENTITY_REPORT.md` — исторический результат не переписан; добавлено уточнение незакрытого favicon/browser acceptance.

Итого: **57 project files** изменены/созданы Wave 8; runtime logs/синтетические recovery records и внешние доказательства учитываются отдельно. Полные начальные и конечные `git status --short`, `git diff --stat` — во внешнем архиве (`target-status-before.txt`, `target-status-after.txt`, `git-diff-stat-after.txt`). Собственный перечень — `changes-from-wave8.json`; содержательный diff — `wave8-only.diff`. Commit, push, deploy и Wave 9 не выполнялись.

READY FOR WAVE 9 / SERVER MOVE: NO
Причина: не завершена обязательная визуальная приёмка шести ролей на desktop/tablet/mobile, включая favicon после очистки кэша и скриншоты «до/после»; текущая локальная dev DB содержит ранее созданную несогласованную visual fixture, и её перенос как готовых данных не принят.
