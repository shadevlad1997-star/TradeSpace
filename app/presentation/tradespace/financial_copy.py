"""Human-readable system ledger descriptions; never rewrite persisted records."""
import re
from app.presentation.tradespace.labels import reason_label

SYSTEM_DESCRIPTIONS = {
    'manual deposit confirmation': 'Оплата подтверждена администратором',
    'cabinet deposit confirmation': 'Оплата подтверждена в кабинете',
    'deposit requisite reserved': 'Резерв для назначения операции',
    'deposit hold released': 'Резерв операции освобождён',
    'successful deposit settlement debit': 'Списание по подтверждённой оплате',
    'successful deposit trader commission release': 'Освобождена комиссия трейдера',
    'appeal approved deposit settlement debit': 'Списание по одобренному спору',
    'appeal approved trader commission release': 'Освобождена комиссия по спору',
    'appeal approved deposit confirmation': 'Зачисление по одобренному спору',
    'manual trader finance adjustment by superadmin': 'Корректировка финансов администратором',
    'settlement request hold': 'Резерв по запросу расчёта',
    'settlement completed': 'Расчёт завершён',
    'settlement rejected': 'Резерв отклонённого расчёта возвращён',
    'payout requested': 'Резерв по запросу выплаты',
    'payout created': 'Резерв по запросу выплаты',
    'payout completed': 'Выплата завершена',
    'payout cancelled': 'Выплата отменена, резерв возвращён',
    'payout rejected': 'Выплата отклонена, резерв возвращён',
    'payout failed': 'Выплата не выполнена, резерв возвращён',
    'TeamLead accrual applied to outstanding debt': 'Начисление направлено на погашение долга',
    'TeamLead commission from paid deposit gross': 'Комиссия за операцию привлечённого трейдера',
    'Merchant referral accrual applied to outstanding debt': 'Комиссия за мерчанта направлена на погашение долга',
    'TeamLead merchant referral commission from paid gross': 'Комиссия за операцию привлечённого мерчанта',
    'TeamLead settlement requested': 'Средства зарезервированы для расчёта',
    'merchant confirmed Rolling transfer receipt': 'Подтверждено получение Rolling',
    'Rolling pending exposure created': 'Обязательство по Rolling создано',
    'successful deposit Rolling recovery': 'Погашение Rolling успешной операцией',
    'Rolling pending exposure finalized as paid': 'Обязательство Rolling учтено после оплаты',
    'successful deposit settle overflow': 'Остаток после погашения Rolling зачислен на баланс',
    'QA opening funding': 'Тестовое пополнение',
    'Legacy ineligible Rolling exposure finalized to settle': 'Обязательство без участия в Rolling завершено',
    'Legacy ineligible Rolling payable credited to settle': 'Зачисление без участия в Rolling',
}


ENTRY_LABELS = {
 'credit':'Зачисление','debit':'Списание','fee':'Комиссия','hold':'Резервирование',
 'release':'Возврат резерва','release_hold':'Освобождение резерва',
 'balance_adjustment':'Корректировка баланса','insurance_deposit_set':'Изменение страхового резерва',
 'deposit_success_debit':'Списание по оплате','accrual_credit':'Зачисление комиссии',
 'debt_offset':'Погашение долга','accrual_reversal':'Сторно начисления',
 'settlement_freeze':'Резервирование расчёта','settlement_release':'Возврат резерва расчёта',
 'settlement_complete':'Завершение расчёта','manual_adjustment':'Корректировка',
 'write_off':'Списание долга','funding':'Финансирование','topup':'Пополнение Rolling',
 'pending_added':'Обязательство создано','pending_released':'Обязательство освобождено',
 'recovery':'Погашение Rolling','settle_overflow':'Зачисление остатка','reversal':'Возврат',
 'platform_income':'Доход платформы',
}
DESCRIPTION_PREFIXES = {
 'Rolling pending exposure released: ':('Обязательство Rolling освобождено',False),
 'Rolling pending exposure reopened: ':('Обязательство Rolling возобновлено',False),
 'Rolling suspended: ':('Rolling приостановлен',True),
 'Rolling resumed: ':('Rolling возобновлён',True),
 'available_credit: ':('Зачисление',True),'available_debit: ':('Списание',True),
 'debt_increase: ':('Увеличение долга',True),'debt_write_off: ':('Списание долга',True),
}

def financial_description(value, *, user_comment=False):
    text = str(value or '').strip()
    if not text:
        return ''
    if text in SYSTEM_DESCRIPTIONS:
        return SYSTEM_DESCRIPTIONS[text]
    if text.startswith('TRC20 settlement completed; tx='):
        return 'Расчёт TRC20 завершён'
    for prefix,(label,human) in DESCRIPTION_PREFIXES.items():
        if text.startswith(prefix):
            tail=text[len(prefix):]
            return label + (': '+(tail if human else reason_label(tail)) if tail else '')
    # Free-form human comments remain intact, including English comments.
    if user_comment or re.search('[А-Яа-яЁё]', text):
        return text
    return 'Финансовое движение'
