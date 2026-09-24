# Подтверждение споров: принятое правило и проверка релиза

Дата: 24.09.2026. Изменение выполнено по прямому решению владельца.

## Реализованное поведение

- Предыдущее состояние `paid`: спор становится `approved`, заявка остаётся `paid`.
  Повторного списания/кредита, комиссий, TeamLead, восстановления или погашения Rolling нет.
  Не создаётся повторный `deposit.paid` или новый callback агрегатору. Решение оставляет
  сообщение спора и аудит `deposit_appeal_approved` с `financial_resolution=already_paid`,
  `finance_applied=false`. Уже погашенный Rolling не открывается повторно и не вызывает 409.
- Известное неоплаченное состояние (`created`, `pending`, `failed`, `expired`, `cancelled`):
  подтверждение проводит платёж один раз. Активный резерв расходуется, излишек освобождается.
  После освобождения резерва используется существующий `trader_debit_available`, учитывающий
  другие обязательства. Недостаточность средств или несовместимая история дают 409 без частичного результата.
- Для комиссии используется immutable OperationFeeSnapshot операции. Текущий изменённый тариф
  не подставляется. Отсутствующий/несовместимый snapshot или история резерва требуют ручного разбора.
- Merchant: сохранённая комиссия Trader уменьшает его списание. Aggregator: комиссия исполнителя
  в snapshot принадлежит агрегатору; Trader ранее резервирует полную сумму, его комиссия в этом пути
  равна нулю. Полная сумма списывается с Trader, а комиссия зачисляется AggregatorAccount существующим
  `sync_payment_from_deposit`. Это не новая формула обычного платежа.
- При новом найденном платеже AggregatorPayment синхронизируется с paid и создаёт обычные durable callbacks
  обоих направлений (platform_to_aggregator, aggregator_to_merchant). Их доставка остаётся за существующим worker.
- Предыдущее неизвестное состояние никогда не трактуется как неоплаченное. Если обычное подтверждение
  успело завершиться во время спора, текущее paid тоже не обрабатывается финансово второй раз.
- Блокировки Deposit → Appeal, существующие ключи журналов и состояние спора ограничивают результат одним
  применением. Повтор завершённого решения получает 409. Каждое одобрение имеет savepoint: worker, который
  ловит бизнес-ошибку и продолжает batch, не сможет сохранить частично проведённые деньги.

Изменены только `app/services/appeals.py` и одна фраза в `app/web/routes.py` среди protected paths.
Обычные confirmation/routing/ledger/fee/TeamLead/Rolling/payout/settlement/auth сервисы и миграции не изменены.

## Сценарии на реальном изолированном PostgreSQL

Тесты: `tests/test_appeal_confirmation_rule_postgres.py` (27 новых случаев) и существующий
`tests/test_final_acceptance_combined_postgres.py` (7 combined cases, обновлено утверждённое ожидание paid).
HMAC/JWT/Redis и финансовые команды реальные, участники и суммы синтетические. Внешняя FX-котировка
фиксирована только внутри тестов; переводов в банк/blockchain и HTTP реальным партнёрам нет.

| Сценарий | Проверяемый результат |
|---|---|
| Merchant и Aggregator, ранее paid, Rolling exhausted | approved/paid, финансовый snapshot и partner records побайтово/структурно прежние; никаких новых paid events |
| Merchant и Aggregator, pending | однократный debit существующего hold, без второго списания; полная финансовая сверка |
| Merchant и Aggregator, cancelled/expired/failed | released hold не списывается повторно; available debit, восстановление allocation и одно погашение Rolling |
| Изменение тарифов после создания | ставки 40%/99% не меняют сохранённые комиссии операции |
| Конкурентное подтверждение одного спора | ответы 200/409, один финансовый результат и один аудит одобрения |
| Повтор и второй спор по тому же платежу | повтор 409; второй обоснованный спор approved без второго финансового результата |
| Обычное подтверждение одновременно со спором | одна оплата, один debit, один deposit.paid |
| Недостаточный доступный остаток после cancelled/expired | 409, нет частичного изменения состояния/денег/начислений/событий |
| Неконсистентный активный hold | 409, чужие/несуществующие резервы не используются |
| Неизвестное предыдущее состояние: 4 варианта | 409, финансовое состояние неизменно |
| Нет snapshot или сведений о hold | 409, деньги/журналы неизменны |
| Исторический gross hold 1000 при комиссии Trader 50 | debit 950 и release 50; итоговый hold 0 |
| Автоматическое одобрение ранее paid с Rolling | approved_auto; повтор batch не создаёт денег/событий |
| Ошибка worker после настоящих финансовых записей | savepoint откатывает все записи; последующая попытка успешно проводит один платёж |

Числовая проверка для синтетического платежа 1000 RUB: merchant fee 100, executor fee 50,
platform gross 50. Merchant-путь: Trader debit 950, TeamLead trader 10 + merchant 5, net platform 35.
Aggregator-путь: Trader debit 1000, aggregator fee 50, только merchant TeamLead 5, net platform 45.
В обоих случаях 600 RUB погашают два Rolling-транша FIFO (2 + 4 USDT при fixture-курсе 100),
300 RUB идут Merchant. Начальные merchant 1000 становятся 1300. Все четыре сверки (Trader,
Merchant, Rolling, TeamLead) сходятся. При исходном paid все эти значения остаются прежними.

## Результаты обязательных проверок

| Проверка | Фактический результат |
|---|---|
| Полный PostgreSQL suite | **375 passed**, 0 failed/errors/skipped; 670.08 s |
| Новые споры | **27/27 PASS** в full suite; combined Appeal/TeamLead/Rolling **7/7 PASS** |
| Golden | **16/16 match**, 23 oracle tests PASS; ни один из 17 snapshot/index файлов не изменён |
| Freeze | **90 protected / 18 areas PASS** без override после адресного принятия двух разрешённых файлов |
| Freeze negative control | Подмена hash appeals.py вызывает FAIL FZ-13; защита не ослаблена |
| Preflight | **19/19 PASS**, включая реальную read-only Rapira quote |
| HTTP smoke | **4/4 PASS** после перезапуска текущего приложения |
| Recovery/business | **14/14 PASS**, подпись и реальный локальный HTTP 500 → scheduled retry → 204 |
| Aggregator callback retry | PASS существующего 500 → retry → 204 теста в полном suite |
| JavaScript menu | **4/4 PASS** |
| Packaging | **6/6 PASS**, повторено после окончательной корректировки подготовки source handoff |
| Python compile / pip check / YAML parse / git diff --check текущего patch | PASS |

10 предупреждений full suite — существующие deprecation FastAPI ORJSONResponse и Alembic path_separator.
Ничего не замаскировано xfail/skip. Исторический Golden остаётся 9 automated / 7 partially automated;
новые 27 PostgreSQL регрессий дополнительно проверяют деньги, состояния, journals и события споров.
Первые запуски нового теста выявили ошибку имени импортируемого helper; исправлен тест, не финансовый код.
Окончательный full suite зелёный.

Ветвь исходной рабочей копии: `codex/tradespace-wave1`, HEAD
`8b88337e55eb5d8b6d3a3d2ac78e7ed8943d0279`, без новых коммитов.
До задачи было 114 строк git status с существующими изменениями. Их состояние и 314 hashes/копии
сохранены в `R:\TradeSpace-Archive\appeal-rule-20260924\before.json` и `files/`.
Оригинальный recovery workspace не изменён: 225 hashes совпадают.
Подробные логи и полный JUnit хранятся там же вне нового репозитория.

Freeze baseline обновлён только для `app/services/appeals.py` (FZ-13) и `app/web/routes.py` (FZ-18),
после проверки точного diff и full suite. 88 остальных protected files совпадают с началом задачи.
Миграции и API-контракты не изменены; head по-прежнему 0027.

## Изменения интерфейса и ручная приёмка

Владелец ранее вручную проверил интерфейс и принял его без замечаний. Эта приёмка сохранена;
она не выдаётся за автоматизированный визуальный тест. Layout, меню, формы и стили этой задачей не менялись.
Изменена одна успешная фраза после решения: «Апелляция подтверждена» вместо обещания пересчёта баланса.
Для ручной перепроверки достаточно одобрить спор по уже paid и по найденному ранее неоплаченному платежу,
проверить этот текст, paid/approved и отсутствие повторных начислений в первом случае.

## Чистый релиз и отдельный репозиторий

Deployment ZIP исключает tests/QA-генераторы, generated accounts, uploads, runtime, caches, dumps,
credentials и вложенные архивы; содержит актуальное приложение, 27 миграций до 0027,
Compose, nginx/monitoring и manifest SHA-256 каждого файла.

Отдельный source handoff сохраняет тесты, синтетические fixture generators, Golden/Freeze и CI,
но не реальные dev/test-записи, секреты, вложения, старый .git или архивы. Новая Git-история
готовится отдельно; исходная папка и её история не перезаписываются. Commit/push/deploy не выполнялись.

При упаковке устранены подтверждённые проблемы: общий Docker ignore по слову credentials исключал
серверный модуль и миграцию 0027; теперь исключаются credential data JSON. ZIP прежде не включал
production Compose и nginx/monitoring. CI теперь использует безопасный тестовый endpoint,
поддерживает первый коммит без родителя и запуск по main/codex branches/вручную.

## Handoff

Реальный Production не активирован, live keys не выпущены. Для будущего серверного запуска нужны
отдельные чистые PostgreSQL/Redis и новые секреты; не импортировать локальные данные/балансы.
Доставка реальным партнёрам, Linux/container image build, TLS/egress и серверная резервная копия
проверяются на целевом окружении отдельно. Локальная регрессия не является доказательством этих условий.


## Состав изменений этой задачи

- Правило и сообщение: `app/services/appeals.py`, `app/web/routes.py`.
- Регрессии: `tests/test_appeal_confirmation_rule_postgres.py`,
  `tests/test_final_acceptance_combined_postgres.py`, `tests/test_remediation_packaging.py`.
- Принятие проверенного core diff: `tests/golden/freeze_zone_baseline.json` (только два hash и acceptance_history).
- Упаковка: `scripts/build_release.py`, новый `scripts/prepare_repository.py`, `.dockerignore`, `.gitignore`.
- Подготовка CI: `.github/workflows/release-gates.yml`.
- Только CRLF → LF для Linux bash, без изменения команд: `scripts/backup_encrypted.sh`,
  `scripts/verify_encrypted_backup_restore.sh`, `scripts/run_tests_workflow.sh`.
- Документация: `README.md` (в том числе актуальный head 0027), этот report/handoff.

В отдельную подготовленную папку дополнительно записывается `.gitattributes`: сохраняет принятые
байты защищённых файлов между Windows/Linux, задаёт LF для shell. Она не устанавливается
в старый working tree, не переписывает его Git history и не меняет protected source.

Финальные артефакты:
- Release: `R:\TradeSpace-Release\tradespace-0027-appeal-rule-20260924.zip`.
- Состав нового репозитория: `R:\TradeSpace-Publish\tradespace-0027-20260924`.
- Внешний delivery report и JSON verification: `R:\TradeSpace-Release\appeal-rule-20260924-delivery.md`
  и `R:\TradeSpace-Release\appeal-rule-20260924-verification.json`.

SHA-256 ZIP записывается в sidecar `.sha256` и внешний delivery report после финальной сборки;
он не встраивается внутрь самого архивируемого документа. Файловый manifest проверяется после чтения ZIP.

## Блокеры перед публикацией и граница следующего этапа

Для публикации требуется выбрать владельца/URL отдельного **private** GitHub repository и дать отдельную
команду на первый commit/push. В этой задаче нет ни commit, ни push, ни remote URL, ни deploy.
Локальная функциональная регрессия не оставила blocker по принятому правилу споров.
GitHub Actions/Linux Docker build и dependency audit должны пройти в целевой CI после разрешённого импорта;
здесь они не выдаются за выполненные. Они, TLS/egress/consumer acceptance и новая чистая БД с секретами
остаются условиями последующего серверного допуска, а не разрешением включить реальные платежи.


### Первый импорт и унаследованное форматирование

Проверка полного нового дерева (от пустого каталога) выявила старые пробелы/пустые строки,
в том числе в migration 0001 и защищённых security/russian_banks. Они не меняются ради стиля.
В первом root commit CI обязательно проверяет Freeze и публикует whitespace findings как report-only;
для всех последующих push/PR проверка изменённого patch остаётся строгой. Это явная граница
первоначального импорта, не отключение Golden, Freeze, secret scan или функциональных тестов.
Подробный исходный список: внешний `initial-tree-whitespace.log` в evidence directory.
