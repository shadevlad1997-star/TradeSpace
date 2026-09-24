# TeamLead

## Назначение роли

Каноническое имя роли — `teamlead`, интерфейсное название — `TeamLead`.
Это отдельный staff-realm кабинет с существующими password/session/CSRF/2FA
механизмами. TeamLead видит только собственный баланс, назначения, начисления,
ledger и settle. Реквизиты трейдеров, карты, телефоны, secrets, другие
TeamLead и полная финансовая модель мерчанта в кабинет не передаются.

TeamLead получает настроенный процент от gross успешного Deposit привлечённого
трейдера:

```text
accrual_rub = deposit_gross_rub × commission_percent / 100
```

**Правило атрибуции:** Deposit, созданный до назначения TeamLead и оплаченный
после назначения, не создаёт accrual. Назначение выбирается исключительно на
`deposit.created_at`; назначение задним числом не применяется.

Вознаграждение является расходом платформы. Оно не уменьшает доход или баланс
трейдера, сумму к выплате мерчанту/settle и Rolling recovery.

## Исторические назначения

`teamlead_trader_assignments` хранит TeamLead, трейдера, процент
`Numeric(10, 6)`, период действия, акторов и причины создания/закрытия.
PostgreSQL partial unique index разрешает только одно назначение с
`effective_to IS NULL` на трейдера.

Назначение, переназначение и изменение процента никогда не обновляют
финансовую историю. Текущая запись закрывается, затем создаётся новая. Для
Deposit выбирается запись, действовавшая в `deposit.created_at`, а не на момент
подтверждения. Pending Deposit поэтому нельзя перевести к другому TeamLead
задним числом.

## Начисление и platform expense

Начисление выполняется в той же транзакции paid-flow, что и settlement из
immutable `OperationFeeSnapshot`. На Deposit действует unique constraint.
Сохраняются gross и snapshot процента. Повторная финализация возвращает
существующее начисление.

Одновременно создаётся отдельная запись
`platform_ledger_entries.entry_type = teamlead_expense`. Reversal создаёт
`teamlead_expense_reversal`. Доход платформы в отчёте показывает TeamLead
expense и net после него. Если expense больше platform income операции,
начисление не обрезается и paid не блокируется; событие
`teamlead_loss_making_operation` пишется в finance log.

## Баланс, ledger, reversal и debt

`teamlead_balances` содержит:

- `available_rub`;
- `frozen_rub`;
- `debt_rub`;
- `total_earned_rub`;
- `total_paid_rub`.

Все значения — неотрицательные `Numeric`, денежные вычисления выполняются
через `Decimal` с явным округлением до копеек.

`teamlead_ledger_entries` — immutable ledger. UPDATE/DELETE запрещены ORM
listener и PostgreSQL trigger. Движения имеют unique idempotency key и
after-balances.

Reversal не удаляет accrual. Он создаёт `accrual_reversal`, списывает доступный
остаток и переносит нехватку в `debt_rub`. Будущие начисления сначала создают
`debt_offset`, затем кредитуют остаток в available. При debt больше нуля settle
запрещён. Completed settle не переписываются.

## Settle USDT TRC20

TeamLead вводит сумму, которую хочет получить. Комиссия всегда списывается
сверх неё:

```text
requested_usdt = 100
fee_usdt = 5
total_debit_usdt = 105
total_debit_rub = 105 × Rapira live ask RUB/USDT
```

`requested_rub` — фактическая выплата TeamLead, `fee_rub` — отдельная комиссия
платформы, `total_debit_rub = requested_rub + fee_rub`. `total_paid_rub`
увеличивается только на `requested_rub`, без комиссии.

TRC20-адрес проходит полную Base58Check-проверку: декодирование 25 байт,
network/version byte `0x41` и первые 4 байта двойного SHA-256 checksum.
Проверки только regex, длины и префикса недостаточно.

Адрес проверяется сервером как TRC20 Base58-адрес длиной 34 символа,
начинающийся с `T`.

HTTP-запрос Rapira выполняется до DB locks. Используется только текущая strict
политика:

- успешный live HTTP response;
- canonical `USDT/RUB` symbol;
- только `askPrice`;
- `source=rapira_live`, `side=ask`;
- nullable provider timestamp;
- обязательные `fetched_at` и `freshness_basis`;
- без static, stale, last-success и legacy fallback.

После получения quote баланс блокируется `SELECT ... FOR UPDATE`. Эквивалент
`requested + 5 USDT` переводится available → frozen, создаются pending
settlement и `settlement_freeze`. Недоступная Rapira возвращает стабильную
ошибку и не меняет баланс.

Допускается один pending:

- `rejected` требует причину, создаёт `settlement_release`, возвращает frozen
  в available и не запускает cooldown;
- `completed` требует `tx_hash`, создаёт `settlement_complete`, окончательно
  списывает frozen и запускает ровно 168 часов cooldown от `completed_at`;
- повтор того же финального действия идемпотентен, противоположное финальное
  действие отклоняется.

## Права и audit

- Superadmin создаёт/блокирует TeamLead, управляет назначениями и процентами,
  видит финансы, выполняет reversal/correction и обрабатывает settle.
- Admin создаёт TeamLead и видит только базовый список и текущих назначенных
  трейдеров. Он не меняет процент, не делает reassign/correction и не
  обрабатывает settle.
- TeamLead работает только со своим кабинетом и создаёт только собственный
  settle.
- Support, Merchant и Trader новых TeamLead-прав не получают.

Все superadmin-финансовые маршруты используют существующую проверку
подтверждённой 2FA-сессии, CSRF и audit. TeamLead также является обязательной
2FA-ролью; settle требует подтверждённой 2FA-сессии.

## Блокировки и идемпотентность

Финансовый порядок:

1. Deposit;
2. историческое TeamLead assignment;
3. TeamLead balance;
4. TeamLead accrual;
5. TeamLead settlement.

Rapira I/O не входит в этот порядок и всегда завершается заранее. Unique
constraints защищают Deposit accrual, активное назначение, pending settlement
и idempotency keys. Конкурентные запросы дополнительно сериализуются
`SELECT ... FOR UPDATE`.

## Reconciliation

`reconcile_teamlead_account()` возвращает структурированный, read-only отчёт:
total/reversed accrual, debt offsets, completed/pending/rejected settlement
суммы, balance cache, количество ledger entries и набор проверок. Сервис не
исправляет расхождения молча.

Platform income reconciliation независимо сравнивает TeamLead accrual/reversal
с `teamlead_expense`/`teamlead_expense_reversal` в platform ledger. Чистая
прибыль платформы равна platform income минус net TeamLead expense; расхождение
в любой из двух частей делает общий отчёт unreconciled.

## Миграция

`0017_teamlead` следует за `0016_rolling_rapira_freshness`. Она не изменяет
существующих пользователей, не назначает TeamLead и не создаёт начисления
задним числом. Clean downgrade до `0016` поддерживается. Downgrade блокируется,
если появились accrual, settlement, ledger или ненулевой TeamLead balance.

`users.role` физически хранится как `VARCHAR(32)` (`String(32)`), а не как
PostgreSQL ENUM. Поэтому миграция 0017 не добавляет и downgrade не удаляет ENUM
value; строка `teamlead` в существующей записи `users` физически остаётся после
clean downgrade.

Миграцию нельзя запускать на staging/production в рамках локального спринта.

## Manual QA перед merge/deploy

Автоматические тесты не заменяют обязательный вечерний browser walkthrough:

1. Создать TeamLead как Superadmin и как Admin, подключить 2FA.
2. Назначить трейдера, изменить процент и проверить исторические интервалы.
3. Создать Deposit до reassign, подтвердить paid после reassign и проверить
   attribution по `deposit.created_at`.
4. Проверить gross accrual, balance, platform expense и неизменность
   Trader/Merchant/Rolling.
5. Создать settle 100 USDT и увидеть freeze эквивалента 105 USDT по live ask.
6. Reject с причиной, проверить release и немедленный повторный запрос.
7. Complete с tx hash, проверить списание frozen и точный cooldown 168 часов.
8. Убедиться, что TeamLead не видит реквизиты и данные другого TeamLead.
9. Проверить superadmin audit и reconciliation.
