# Merchant Rolling: подтверждённые переводы и FIFO-погашение

## Финансовая модель

Для каждого мерчанта базовый расчёт всегда один: сумма к выплате после
успешного Deposit накапливается в RUB settle balance. Rolling — не тип
мерчанта и не постоянный режим, а временный финансовый цикл, который
начинается только после подтверждения мерчантом конкретного USDT-перевода.

Терминология активного кода и UI:

```text
merchant_payable_rub = gross_rub - merchant_fee_rub
merchant_payable_usdt = merchant_payable_rub / saved_rapira_ask
```

Пока нет подтверждённого перевода с остатком, вся сумма
`merchant_payable_rub` зачисляется в settle. Pending, disputed и cancelled
переводы не меняют principal, recovered, outstanding или settle.

## Двухэтапный перевод

Superadmin в профиле мерчанта регистрирует отправку с amount, network,
destination, tx hash, sent_at, комментарием и idempotency key. Создаётся
`MerchantRollingTransfer(status=pending_confirmation)`, но Rolling account
и funding/topup ledger на этом шаге не меняются.

Мерчант видит неизменяемые реквизиты перевода и может:

- подтвердить получение;
- сообщить о проблеме с обязательной причиной.

Подтверждение выполняется в одной транзакции:

1. `SELECT ... FOR UPDATE` transfer и проверка ownership/status;
2. создание или блокировка merchant Rolling account;
3. однократное увеличение principal и outstanding;
4. перевод в `confirmed` с `confirmed_at` и `confirmed_by`;
5. immutable funding/topup ledger и audit.

Повторное подтверждение возвращает уже подтверждённый transfer и не меняет
агрегаты. Confirm конкурирует с dispute/cancel под блокировкой одной строки,
поэтому фиксируется ровно один terminal result. Подтверждённый перевод нельзя
отменить; исправление требует отдельной компенсирующей финансовой операции.

## Применимость к Deposit

Определяющий момент — неизменяемый `deposit.created_at`, а не paid_at.
При создании Deposit:

1. ищется confirmed transfer с remaining > 0 и
   `confirmed_at <= deposit.created_at`;
2. если его нет, strict Rapira quote не запрашивается и allocation не
   создаётся;
3. если он есть, до финансовых DB locks запрашивается свежий live ask Rapira;
4. allocation сохраняет rate evidence, `rolling_eligible_at` и максимальный
   `eligible_transfer_sequence`.

Таким образом, Deposit, созданный до подтверждения funding/top-up, не может
использовать этот перевод, даже если становится paid после подтверждения.
Новый top-up также не забирает ранее начисленный settle и не участвует в
старых allocations.

Eligibility хранится явно и проверяется DB constraint:

- `eligibility_status=eligible` требует непустые
  `eligible_transfer_sequence` и `rolling_eligible_at`, а также source
  `confirmed_transfer` или `legacy_migration`;
- `eligibility_status=ineligible` требует NULL в sequence/timestamp и source
  `legacy_no_confirmed_funding` либо
  `no_confirmed_funding_at_creation`.

Новые Deposits без confirmed transfer не создают allocation. Явное
`ineligible` состояние существует для безопасно мигрированной legacy-истории.
При paid такая allocation целиком зачисляет merchant payable в settle и
никогда не ищет текущий active Rolling или более поздний top-up.

## Strict Rapira

Для Rolling используется только успешный live HTTP response и `askPrice`.
Symbol определяет направление. Provider timestamp nullable: freshness
основана на нём, когда он валиден, иначе на фактическом UTC `fetched_at`.
Static fallback, last-success fallback, legacy cache, bid, last и close в
Rolling не используются. HTTP выполняется до участков с DB row locks.

Immutable allocation хранит rate, symbol, side=ask, source=rapira_live,
provider timestamp, fetched_at и freshness basis.

## FIFO и crossing в settle

При paid блокируются только transfers, допустимые snapshot этого Deposit:
confirmed, `sequence_no <= eligible_transfer_sequence` и
`confirmed_at <= rolling_eligible_at`. Погашение идёт по sequence FIFO.

Каждая часть погашения записывается отдельной immutable
`MerchantRollingTransferConsumption`:

- deposit и allocation;
- transfer и account;
- USDT, RUB и сохранённый rate;
- recovery/reversal type и idempotency key.

Для пересекающей операции:

```text
rolling_applied_usdt =
    min(merchant_payable_usdt, eligible_confirmed_remaining)

rolling_applied_rub + settle_credited_rub = merchant_payable_rub
```

Если Rolling покрывает всю операцию, весь RUB payable считается recovery.
Если remaining меньше payable, допустимый USDT остаток закрывается, а точный
RUB overflow зачисляется в settle. После outstanding=0 account становится
`exhausted`; новый трафик продолжает идти в settle без ручного переключателя.

Account агрегаты обязаны совпадать с confirmed transfers:

```text
account.principal   = sum(confirmed transfer.amount)
account.recovered   = sum(confirmed transfer.recovered)
account.outstanding = sum(confirmed transfer.remaining)
principal = recovered + outstanding
```

## Reversal и immutable history

История не редактируется и не удаляется. Reversal:

- блокирует Deposit → allocation → account → затронутые transfers;
- создаёт compensating consumption rows;
- возвращает recovery в transfer remaining/account outstanding;
- отдельно сторнирует settle overflow, если он доступен;
- добавляет immutable ledger и audit.

PostgreSQL trigger и ORM guards запрещают UPDATE/DELETE для Rolling ledger и
transfer consumption rows.

## Merchant settle

Settle создаётся только вручную мерчантом. Текущая реализация поддерживает
частичный запрос: requested USDT вместе с комиссией конвертируется в
`total_debit_rub`, который переносится available → frozen. Rejected возвращает
hold, completed окончательно списывает frozen. Частота не ограничивается, но
partial unique index разрешает только один pending request на merchant.

Rapira quote для settle получается до блокировки balance. Scheduler T+0/T+1,
Celery task или автоматическое создание settle отсутствуют.

## Lock order

Финансовый paid/reversal flow фиксирует порядок:

1. Deposit;
2. Rolling allocation;
3. Rolling account;
4. eligible confirmed transfers FIFO;
5. immutable transfer consumption rows;
6. merchant settle balance.

Confirmation сначала блокирует transfer, затем существующий account. Для
первого account строка merchant служит creation mutex с обязательным re-check.
Manual settle блокирует только settle balance и не берёт Rolling locks.

## Reconciliation

`reconcile_rolling_account()` только сообщает расхождения и ничего не
исправляет. Он сверяет:

- confirmed transfer totals с account aggregates;
- net recovery/reversal consumption с recovered каждого transfer;
- paid allocation RUB split;
- settle overflow allocations с immutable ledger;
- pending exposure;
- количество pending/disputed transfers, не включая их в principal.

## Миграция 0020

`0020_rolling_confirmation_flow` следует за `0019_ai_office_config`.

- Settle balances, trader balances и merchant balances не меняются.
- Пустой/нулевой Rolling account не создаёт transfer.
- Для account с principal > 0 создаётся один synthetic confirmed transfer:
  amount=principal, recovered=recovered, remaining=outstanding,
  source=`legacy_migration`, tx hash остаётся NULL.
- Legacy allocation с доказанным consumption (`rolling_applied_usdt > 0` или
  recovery ledger) связывается с synthetic transfer и получает
  `eligibility_status=eligible`.
- Allocation без consumption остаётся eligible только при наличии
  funding/topup ledger не позднее `deposit.created_at`.
- Allocation без такого evidence получает явный snapshot
  `eligibility_status=ineligible`,
  `eligibility_source=legacy_no_confirmed_funding`; её финансовые суммы и
  status не пересчитываются.
- Для paid/reversed eligible allocations создаются derived immutable
  consumption rows.
- Existing Rolling ledger rows не изменяются; новый nullable FK остаётся NULL.
- Сущность старого постоянного режима удаляется.
- Противоречивые account/funding totals, RUB split, доказанное consumption без
  funding либо duplicate pending settle останавливают upgrade с redacted
  invariant error.

Downgrade допускается только для legacy-only состояния. После регистрации
нового transfer он fail-closed, потому что удаление transfer уничтожило бы
audit/financial evidence.

Исторические миграции `0014`/`0015` и downgrade-часть `0020` сохраняют старое
слово `postpaid` только как неизбежную schema compatibility для отката до
старого приложения. В моделях, сервисах, routes, API, templates, UI, логах и
текущей документации бизнес-модели этого режима нет.

Физические DB-колонки `merchant_net_rub`/`merchant_net_usdt` и прежний
аддитивный response/metadata alias `merchant_net_amount` временно сохранены
ради совместимости внешних клиентов и immutable истории. ORM и новый
контракт отображают их как `merchant_payable_rub`,
`merchant_payable_usdt` и `merchant_payable_amount`; это документированный
технический долг, не пользовательский термин.

## Проверка

Автоматический PostgreSQL suite покрывает регистрацию/подтверждение,
ownership, dispute/cancel и их гонки, FIFO нескольких transfers, crossing,
eligibility по created_at, future top-up exclusion, exhausted routing,
idempotent/concurrent paid, reconciliation, manual partial settle, migration
legacy totals и downgrade guard.

Для ручной проверки localhost нельзя создавать реальный transfer без
отдельного решения оператора. Проверяются пустые состояния, существующая
история, формы/permissions/CSRF, health endpoints и отсутствие ошибок в
api/worker/beat logs.
