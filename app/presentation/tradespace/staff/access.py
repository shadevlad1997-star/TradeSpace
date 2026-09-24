"""Explicit presentation scopes, derived from web routes and the admin read guards.

No client-supplied role/model/table can widen these sets. TeamLead is intentionally absent.
"""
from dataclasses import dataclass
from app import models as m

STAFF_ROLES = frozenset({'superadmin', 'admin', 'support'})
MANAGERS = frozenset({'superadmin', 'admin'})
ROOT = frozenset({'superadmin'})

@dataclass(frozen=True)
class View:
    section: str
    slug: str
    title: str
    model: type
    fields: tuple
    roles: frozenset = STAFF_ROLES
    limit: int = 300
    money: tuple = ()
    currency: str = 'RUB'
    note: str = ''

# Field lists are allowlists, not serialization of ORM objects. Secrets/raw metadata are absent.
VIEWS = (
 View('operations','deposits','Пополнения',m.Deposit,('external_id','amount','currency','method','status','merchant_id','requisites_id','expires_at'),limit=500,money=('amount',)),
 View('operations','payouts','Выплаты',m.Payout,('external_id','amount','currency','status','method','merchant_id','destination'),limit=200,money=('amount',),note='Здесь доступен только просмотр выплат.'),
 View('operations','appeals','Обращения',m.Appeal,('status','operation_type','operation_id','decision'),limit=500),
 View('network','traders','Трейдеры',m.User,('email','is_active','is_locked','trader_traffic_status','trader_traffic_priority')),
 View('network','merchants','Мерчанты',m.Merchant,('name','owner_id','sandbox_mode','webhook_url','ip_whitelist')),
 View('network','requisites','Реквизиты',m.Requisite,('bank_name','method','status','trader_id','traffic_status','min_check','max_check','daily_limit','simultaneous_limit'),roles=MANAGERS,money=('min_check','max_check','daily_limit')),
 View('network','teamleads','TeamLead',m.User,('email','is_active','is_locked'),roles=MANAGERS),
 View('network','aggregators','Агрегаторы',m.AggregatorAccount,('name','status','min_payment_amount','max_payment_amount','daily_limit','monthly_limit'),money=('min_payment_amount','max_payment_amount','daily_limit','monthly_limit')),
 View('finance','settlements','Расчёты мерчантов',m.MerchantSettlement,('status','merchant_id','amount_usdt','fee_usdt','total_debit_rub','processed_at'),money=('amount_usdt','fee_usdt','total_debit_rub')),
 View('finance','merchants','Средства мерчантов',m.Balance,('merchant_id','available','frozen','currency'),roles=ROOT,money=('available','frozen'),note='Доступные и замороженные средства. Обязательства по расчётам показаны отдельно.'),
 View('finance','traders','Средства трейдеров',m.User,('email','trader_balance','trader_hold','trader_withdrawals_frozen'),money=('trader_balance','trader_hold'),note='Баланс и зарезервированные средства показаны отдельно.'),
 View('finance','rolling','Rolling',m.MerchantRollingTransfer,('status','merchant_id','sequence_no','amount_usdt','recovered_usdt','remaining_usdt','confirmed_at'),roles=ROOT,money=('amount_usdt','recovered_usdt','remaining_usdt'),currency='USDT'),
 View('finance','teamleads','Средства TeamLead',m.TeamLeadBalance,('teamlead_id','available_rub','frozen_rub','debt_rub','total_earned_rub','total_paid_rub'),roles=ROOT,money=('available_rub','frozen_rub','debt_rub','total_earned_rub','total_paid_rub')),
 View('finance','teamlead-settlements','Расчёты TeamLead',m.TeamLeadSettlement,('status','teamlead_id','requested_usdt','fee_usdt','total_debit_rub','requested_at'),roles=ROOT,limit=1000,money=('requested_usdt','fee_usdt','total_debit_rub')),
 View('finance','accruals','Начисления от трейдеров',m.TeamLeadAccrual,('status','teamlead_id','trader_id','deposit_id','commission_percent_snapshot','accrual_rub'),roles=ROOT,limit=1000,money=('accrual_rub',)),
 View('finance','merchant-accruals','Начисления от мерчантов',m.TeamLeadMerchantAccrual,('status','teamlead_id','merchant_id','deposit_id','commission_percent_snapshot','accrual_rub'),roles=ROOT,limit=1000,money=('accrual_rub',)),
 View('finance','aggregators','Средства агрегаторов',m.AggregatorAccount,('name','balance','hold_balance','total_turnover'),money=('balance','hold_balance','total_turnover')),
 View('finance','income','Доход платформы',m.OperationFeeSnapshot,('deposit_id','settlement_status','merchant_fee_amount','executor_fee_amount','platform_income_amount','currency'),roles=MANAGERS,limit=500,money=('merchant_fee_amount','executor_fee_amount','platform_income_amount')),
 View('finance','fees','Тарифы',m.FeeRule,('entity_type','entity_id','payment_method','min_amount','max_amount','rate_percent','version','is_active'),roles=MANAGERS,limit=500,money=('min_amount','max_amount'),note='Редактирование диапазонов — в карточке мерчанта, трейдера или агрегатора. Условия уже созданных операций не меняются.'),
 View('finance','wallet','Адрес пополнения',m.PlatformCryptoWallet,('asset','network','address','label','is_active','version','change_reason'),roles=MANAGERS),
 View('integrations','webhooks','Доставка Webhook',m.WebhookEvent,('event_type','status','merchant_id','attempts','max_attempts','last_status_code','next_attempt_at')),
 View('integrations','payments','Платежи агрегаторов',m.AggregatorPayment,('aggregator_order_id','merchant_order_id','aggregator_id','status','amount','currency','callback_status','callback_attempts'),money=('amount',)),
 View('integrations','callbacks','Callbacks',m.AggregatorCallbackLog,('direction','related_payment_id','status','attempt','response_status_code','next_retry_at'),note='История доставки. Повторная отправка из этого раздела недоступна.'),
 View('integrations','credentials','API и настройки',m.Merchant,('name','sandbox_mode','webhook_url','ip_whitelist'),roles=MANAGERS),
 View('control','risk','События риска',m.RiskEvent,('severity','decision','target_type','target_id','trader_id','requisite_id','old_status','new_status','resolution_status'),limit=200),
 View('control','audit','Журнал действий',m.AuditLog,('action','actor_id','target_type','target_id','ip'),roles=ROOT,limit=800),
 View('control','users','Сотрудники',m.User,('email','role','is_active','is_locked','twofa_enabled'),roles=ROOT),
 View('control','access','Добавить сотрудника',m.User,(),roles=frozenset({'admin'}),note='Можно создать учётную запись поддержки. Пароли и блокировки сотрудников доступны только суперадминистратору.'),
 View('control','antiscam','Настройки риска',m.AntiscamGlobalSettings,('antiscam_enabled','auto_disable_traffic_enabled','default_reinstate_mode'),roles=ROOT,limit=1),
 View('control','ai','AI-интеграция',m.AIIntegrationConfig,('enabled','environment','auth_type','last_connection_test_at','last_connection_test_status','last_success_at','last_error_code'),roles=ROOT,limit=1),
)

def tabs_for(role, section):
    return tuple(v for v in VIEWS if v.section == section and role in v.roles)

def resolve_view(role, section, tab):
    tabs = tabs_for(role, section)
    return next((v for v in tabs if v.slug == tab), None) if tab else next(iter(tabs), None)
