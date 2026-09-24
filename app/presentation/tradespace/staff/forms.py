"""HTML form descriptions for the existing cabinet POST routes. No commands execute here."""
from uuid import uuid4
from types import SimpleNamespace
from app.services.deposit_confirmation import CONFIRMABLE_DEPOSIT_STATUSES
from app.core.payment_methods import payment_method_label
from app.core.russian_banks import get_enabled_banks
from app.core.mobile_operators import get_enabled_mobile_operators
from app.services.fee_tiers import COMMISSION_TIER_RANGES, COMMISSION_TIER_METHODS, build_commission_tier_context


def field(name, label, value='', *, kind='text', options=(), required=True, multiple=False, hint=''):
    return dict(name=name,label=label,value='' if value is None else value,kind=kind,options=options,required=required,multiple=multiple,hint=hint)

def choice(name,label,options,value='',**kwargs):
    return field(name,label,value,kind='select',options=options,**kwargs)

def hidden(name,value):
    return field(name,'',value,kind='hidden')

def reason():
    return field('reason','Причина',kind='textarea')

def action(path,title,fields=(),*,effect='Изменение сохранится после проверки прав и условий на сервере.',danger=False):
    return dict(path=path,title=title,fields=list(fields),effect=effect,danger=danger,dialog='staff-action-'+uuid4().hex)

METHODS=tuple((x,payment_method_label(x)) for x in COMMISSION_TIER_METHODS)
ROLE_LABELS={'admin':'Администратор','support':'Поддержка','teamlead':'TeamLead','merchant':'Мерчант','trader':'Трейдер','operator':'Трейдер (operator)'}


def create_user(role, *, staff=False, selected='trader'):
    roles = (['admin','support'] if role=='superadmin' else ['support']) if staff else ['trader','operator','merchant','teamlead']
    return action('users/create','Создать staff-аккаунт' if staff else 'Создать участника',[
        field('email','Email',kind='email'),field('password','Начальный пароль',kind='password'),
        choice('role','Роль',[(v,ROLE_LABELS[v]) for v in roles],selected if selected in roles else roles[0]),
        *([] if staff else [field('merchant_name','Название мерчанта',required=False)]),
    ],effect='Создаётся новый аккаунт выбранной роли. Для staff обязательна настройка 2FA. API-данные мерчанта показываются один раз.')


def aggregator_fields(row=None):
    defaults={'min_payment_amount':'100','max_payment_amount':'150000'}
    return [field(n,l,getattr(row,n,defaults.get(n,'')),required=n in {'name','min_payment_amount','max_payment_amount'}) for n,l in (
        ('name','Название'),('callback_url','Callback URL'),('success_url','Success URL'),('fail_url','Fail URL'),
        ('min_payment_amount','Минимальный платёж'),('max_payment_amount','Максимальный платёж'),
        ('daily_limit','Лимит за день'),('monthly_limit','Лимит за месяц'))]


def requisite_fields(row=None, traders=()):
    from app.web.routes import display_requisite_value
    def val(n,d=''):return getattr(row,n,d)
    result=[choice('method','Способ оплаты',METHODS,val('method','sbp')),
        field('value','Платёжный реквизит',display_requisite_value(row) if row else ''),
        choice('bank_code','Банк',[('','Не выбран')]+[(b.code,b.display_name) for b in get_enabled_banks()],val('bank_code'),required=False),
        choice('operator_code','Оператор связи',[('','Не выбран')]+[(b.code,b.display_name) for b in get_enabled_mobile_operators()],val('operator_code'),required=False),
        field('full_name','Получатель',val('full_name')),field('automation_id','ID автоматизации',val('automation_id'),required=False),
        field('last4','Последние 4 символа',val('last4'),required=False)]
    for n,l,d in [('daily_limit','Дневной лимит',500000),('operation_limit','Лимит операций',50),('request_count','Запросов за период',10),('success_delay_minutes','Пауза после успеха, мин',0),('simultaneous_limit','Параллельных операций',1),('min_check','Минимальная сумма',100),('max_check','Максимальная сумма',150000)]:
        result.append(field(n,l,val(n,d),kind='number'))
    result += [choice('timeframe','Период',[('час','Час'),('день','День')],val('timeframe','час')),
        choice('status','Состояние',[('active','Активен'),('disabled','Отключён'),('review','На проверке')],val('status','active'))]
    if row is None: result.append(choice('trader_id','Трейдер',traders))
    else: result.append(hidden('bank_name',row.bank_name or ''))
    return result


def fee_forms(row, entity, rules):
    current=build_commission_tier_context(rules).get(f'{entity}:{row.id}',{})
    forms=[]
    for method in COMMISSION_TIER_METHODS:
        values=current.get(method,{})
        forms.append(action(f'{entity}s/{row.id}/commission-tiers',f'Тарифы · {payment_method_label(method)}',[
            hidden('payment_method',method),*[field(t['field'],t['label']+' · %',values.get(t['key'],{}).get('rate_percent','')) for t in COMMISSION_TIER_RANGES]
        ],effect='Новые ставки действуют для будущих операций. Сохранённые снимки комиссий не изменяются.'))
    return forms


def account_actions(row,actor):
    if actor.role!='superadmin' or row.id==actor.id or row.role not in ROLE_LABELS:return []
    return [action(f'users/{row.id}/password','Сбросить пароль',[field('new_password','Новый пароль (от 10 символов)',kind='password')],effect='Пароль изменится; refresh tokens будут отозваны.',danger=True),
        action(f'users/{row.id}/lock','Разблокировать аккаунт' if row.is_locked else 'Заблокировать аккаунт',[hidden('locked','false' if row.is_locked else 'true')],effect='Блокировка закрывает вход в аккаунт и отзывает refresh tokens.',danger=not row.is_locked)]


def risk_actions(row,kind,settings):
    path=f'antiscam/{kind}/{row.id}'
    fields=[field('decision_reason','Причина решения',kind='textarea')]
    forms=[action(path+'/pause','Остановить трафик',fields,effect='Приём новых операций будет остановлен.',danger=True),
        action(path+'/reinstate','Возобновить трафик',fields+[field('proofs_checked','Подтверждения проверены',False,kind='checkbox'),field('proof_reference','Ссылка на проверку',required=False),choice('reinstate_mode','Режим',[(x,l) for x,l in [('limited','Ограниченный'),('full','Полный'),('low_amount_only','Малые суммы'),('no_high_amount','Без крупных сумм')]],'limited')],effect='Сервер проверит доказательства и применит существующие ограничения.')]
    if kind=='traders':
        forms.append(action(path+'/withdrawals','Настроить ограничение вывода',[field('frozen','Вывод заморожен',row.trader_withdrawals_frozen,kind='checkbox')]))
    if settings is None:
        settings=SimpleNamespace(antiscam_enabled=False,failed_payments_in_row_limit=10 if kind=='traders' else 5,min_conversion_percent='45' if kind=='traders' else '35',conversion_check_window_minutes=60,max_confirmation_delay_minutes=10,allow_high_amount_traffic=False,use_global_antiscam_settings=False,conversion_drop_percent_limit='40',max_active_payments_when_risky=3,risk_level='strict',auto_disable_requisites_enabled=False,auto_disable_trader_enabled=False,freeze_withdrawals_on_auto_pause=False,limited_max_active_payments=3,limited_max_amount='30000')
    if settings is not None:
        # This list is explicitly limited to fields accepted by the respective existing command.
        names=['antiscam_enabled','failed_payments_in_row_limit','min_conversion_percent','conversion_check_window_minutes','max_confirmation_delay_minutes','allow_high_amount_traffic']
        names += ['use_global_antiscam_settings','conversion_drop_percent_limit','max_active_payments_when_risky','risk_level','auto_disable_requisites_enabled','auto_disable_trader_enabled','freeze_withdrawals_on_auto_pause'] if kind=='traders' else ['limited_max_active_payments','limited_max_amount']
        forms.append(action(path+'/settings','Настройки риска',[setting_field(n,getattr(settings,n)) for n in names],effect='Будут изменены действующие условия контроля трафика.'))
    return forms

SETTING_LABELS={
'antiscam_enabled':'Антискам включён','auto_disable_traffic_enabled':'Автоматическая остановка трафика','use_global_antiscam_settings':'Использовать общие настройки',
'failed_payments_in_row_limit':'Неуспешных операций подряд','min_conversion_percent':'Минимальная конверсия, %','conversion_check_window_minutes':'Окно конверсии, мин','conversion_drop_percent_limit':'Порог падения конверсии, %','max_confirmation_delay_minutes':'Предел подтверждения, мин','allow_high_amount_traffic':'Крупные платежи разрешены','risk_level':'Уровень риска','max_active_payments_when_risky':'Активных платежей при риске','auto_disable_requisites_enabled':'Автоостановка реквизитов','auto_disable_trader_enabled':'Автоостановка трейдера','freeze_withdrawals_on_auto_pause':'Заморозить вывод при автоостановке','limited_max_active_payments':'Лимит активных платежей','limited_max_amount':'Лимит суммы',
'failed_payments_in_row_limit_requisite':'Неуспешных подряд на реквизите','failed_payments_in_row_limit_trader':'Неуспешных подряд у трейдера','min_payments_for_conversion_check':'Минимум операций для проверки','min_requisite_conversion_percent':'Минимальная конверсия реквизита, %','min_trader_conversion_percent':'Минимальная конверсия трейдера, %','merchant_complaints_limit_requisite':'Жалоб на реквизит','merchant_complaints_limit_trader':'Жалоб на трейдера','high_amount_extra_risk_enabled':'Дополнительная проверка крупных платежей','high_amount_threshold':'Порог крупного платежа','freeze_withdrawals_on_trader_auto_pause':'Заморозка вывода при автоостановке трейдера','default_reinstate_mode':'Режим возобновления','limited_reinstate_duration_minutes':'Ограниченный режим, мин','limited_reinstate_max_active_payments':'Лимит активных платежей после возобновления','limited_reinstate_max_amount':'Лимит суммы после возобновления'}

def setting_field(n,v):
    if n in {'risk_level','default_reinstate_mode'}:
        options=[('strict','Строгий'),('normal','Обычный'),('trusted','Доверенный')] if n=='risk_level' else [('limited','Ограниченный'),('full','Полный'),('low_amount_only','Малые суммы'),('no_high_amount','Без крупных сумм')]
        return choice(n,SETTING_LABELS[n],options,v)
    return field(n,SETTING_LABELS[n],v,kind='checkbox' if isinstance(v,bool) else 'number')


def teamlead_adjust_action(teamlead_id):
    return action(f'teamlead/{teamlead_id}/adjust','Корректировка TeamLead',[
        choice('adjustment_type','Вид',[(x,l) for x,l in [('available_credit','Увеличить доступный баланс'),('available_debit','Уменьшить доступный баланс'),('debt_increase','Увеличить долг'),('debt_write_off','Списать долг')]]),
        field('amount_rub','Сумма, RUB'),reason(),hidden('idempotency_key',uuid4().hex)
    ],effect='Будет создана финансовая корректировка с причиной.',danger=True)


def rolling_registration(merchant_id):
    """Describe the existing registration command; never execute a transfer."""
    return action(f'rolling/merchants/{merchant_id}/transfers','Зарегистрировать перевод Rolling',[
        field('amount_usdt','Сумма, USDT'),field('network','Сеть','TRC20'),
        field('destination_address','Адрес получателя'),field('tx_hash','Хеш перевода'),
        field('sent_at','Время отправки (UTC)',kind='datetime-local'),
        field('comment','Комментарий',required=False),hidden('idempotency_key',uuid4().hex)
    ],effect='Регистрируется уже отправленный перевод. Мерчант должен подтвердить его получение.')


def detail_actions(view,row,actor,*,choices=None,rules=(),api_keys=(),signing_keys=(),risk_settings=None):
    from app.web import routes as legacy
    choices=choices or {}; forms=[]; manage=actor.role in {'admin','superadmin'}; root=actor.role=='superadmin'; kind=view.slug
    if view.section=='operations':
        if kind=='deposits' and manage and row.status in CONFIRMABLE_DEPOSIT_STATUSES:
            forms += [action(f'deposits/{row.id}/confirm','Подтвердить поступление',effect='Подтверждайте только фактически полученный платёж. Операция будет учтена в балансах.'),action(f'deposits/{row.id}/decline','Отказать по операции',[reason()],effect='Операция завершится неуспешно; резерв будет освобождён по действующим правилам.',danger=True)]
        if kind=='appeals' and row.status in {'opened','in_review'}:
            forms.append(action(f'appeals/{row.id}/extend','Продлить рассмотрение',[field('minutes','Добавить минут',30,kind='number')]))
            if manage and row.operation_type=='deposit':
                for a,l in [('approve','Одобрить обращение'),('reject','Отклонить обращение')]:
                    forms.append(action(f'appeals/{row.id}/staff/resolve',l,[hidden('action',a)],effect='Проверьте платёж и приложенные подтверждения перед вынесением окончательного решения.',danger=True))
    if view.section=='network':
        if kind=='merchants' and root:
            forms.append(rolling_registration(row.id))
        if kind in {'traders','teamleads'}: forms += account_actions(row,actor)
        if kind=='traders' and root:
            merchant_options=list(choices.get('merchants',()))
            known={str(k) for k,_ in merchant_options}
            assigned=[str(k) for k in (row.trader_assigned_merchants or [])]
            merchant_options += [(k,'Существующее закрепление · '+k) for k in assigned if k not in known]
            forms.append(action(f'traders/{row.id}/finance','Финансы и распределение',[
                field('trader_balance','Целевой баланс, RUB',row.trader_balance),field('trader_hold','Целевой резерв, RUB',row.trader_hold),field('trader_traffic_priority','Приоритет трафика',row.trader_traffic_priority,kind='number'),
                choice('assigned_merchants','Закреплённые мерчанты',merchant_options,assigned,multiple=True,required=False,hint='Пустой выбор снимает закрепления. Ctrl / Cmd позволяет выбрать несколько.')],effect='Это финансовая корректировка, а не пополнение. Также изменятся приоритет и закрепления.',danger=True))
        if kind in {'traders','requisites'} and root: forms += risk_actions(row,kind,risk_settings)
        if kind=='requisites' and manage:
            forms += [action(f'requisites/{row.id}/edit','Изменить реквизит',requisite_fields(row),effect='Изменятся платёжные данные и ограничения новых назначений.'),action(f'requisites/{row.id}/toggle','Выключить реквизит' if row.enabled else 'Включить реквизит',effect='Доступность новых назначений изменится.',danger=row.enabled)] if row.status!='deleted' else []
        if kind=='aggregators' and manage:
            forms += [action(f'aggregators/{row.id}/update','Настроить агрегатора',aggregator_fields(row)),action(f'aggregators/{row.id}/status','Изменить состояние',[choice('status','Состояние',[('active','Активен'),('blocked','Заблокирован'),('archived','В архиве')],row.status)],danger=True)]
        if kind=='teamleads' and root:
            forms.append(teamlead_adjust_action(row.id))
            for entity,label in [('trader','Трейдер'),('merchant','Мерчант')]:
                forms.append(action('teamlead/'+('assignments' if entity=='trader' else 'merchant-assignments'),'Назначить · '+label,[hidden('teamlead_id',row.id),choice(entity+'_id',label,choices.get(entity+'s',())),field('commission_percent','Комиссия, %'),reason()],effect='Создаётся новое историческое назначение. Предыдущие начисления не пересчитываются.'))
    if kind in {'merchants','credentials'} and view.section in {'network','integrations'} and manage:
        forms.append(action(f'merchants/{row.id}/integration','Настроить интеграцию',[field('webhook_url','Webhook URL',row.webhook_url,required=False),field('ip_whitelist','Разрешённые IP через запятую',','.join(row.ip_whitelist or []),required=False)]))
        forms.append(action(f'merchants/{row.id}/api-keys/issue','Выпустить API-ключ',[choice('mode','Режим',[('sandbox','Sandbox'),('production','Production')],'sandbox')],effect='Новые учётные данные будут показаны один раз после создания.'))
        for key in api_keys:
            if key.is_active:
                for cmd,label in [('rotate','Заменить'),('revoke','Отозвать')]:
                    forms.append(action(f'merchants/{row.id}/api-keys/{key.id}/{cmd}',label+' API-ключ · '+legacy.api_key_fingerprint(key.api_key),effect='Текущий ключ перестанет работать. При замене новые учётные данные показываются один раз.',danger=True))
        if root:
            cmd='rotate' if any(k.status=='active' for k in signing_keys) else 'issue'
            forms.append(action(f'merchants/{row.id}/webhook-signing-keys/{cmd}','Заменить ключ Webhook' if cmd=='rotate' else 'Выпустить ключ Webhook',effect='Секрет будет показан один раз. Сохраните его и обновите настройки принимающей стороны.',danger=True))
            forms.append(action(f'teamlead/merchant-assignments/{row.id}/close','Закрыть назначение TeamLead',[reason()],effect='Будет закрыта текущая связь. Исторические начисления сохраняются.'))
    if view.section=='network' and kind in {'merchants','traders','aggregators'} and manage and not row.is_archived:
        forms += fee_forms(row,{'merchants':'merchant','traders':'trader','aggregators':'aggregator'}[kind],rules)
    if view.section=='finance' and root:
        if kind=='settlements' and row.status=='pending':
            forms.append(action(f'settlements/{row.id}/approve','Подтвердить расчёт',[field('tx_hash','Хеш фактической транзакции')],effect='Подтвердите фактически выполненный перевод. Зарезервированная сумма будет списана.',danger=True))
            forms.append(action(f'settlements/{row.id}/reject','Отклонить расчёт',[field('reject_reason','Причина отказа')],effect='Резерв будет возвращён на доступный баланс мерчанта.',danger=True))
        if kind=='teamlead-settlements' and row.status=='pending':
            forms += [action(f'teamlead/settlements/{row.id}/complete','Подтвердить расчёт TeamLead',[field('tx_hash','Хеш фактической транзакции')],effect='Зарезервированные средства будут списаны. Начнётся период ожидания до следующего запроса.',danger=True),action(f'teamlead/settlements/{row.id}/reject','Отклонить расчёт TeamLead',[reason()],effect='Резерв вернётся на доступный баланс.',danger=True)]
        if kind in {'accruals','merchant-accruals'} and row.status=='credited':
            forms.append(action(f'teamlead/accruals/{row.deposit_id}/reverse','Сторнировать начисления операции',[reason()],effect='Сторно затрагивает обе referral-стороны этой операции по действующим правилам.',danger=True))
        if kind=='teamleads':
            forms.append(teamlead_adjust_action(row.teamlead_id))
        if kind=='rolling' and row.status in {'pending_confirmation','disputed'}:
            forms.append(action(f'rolling/transfers/{row.id}/cancel','Отменить перевод Rolling',[reason()],effect='Сервер проверит допустимость отмены и использование principal.',danger=True))
        if kind=='merchants':
            forms += [action(f'rolling/merchants/{row.merchant_id}/transfers','Зарегистрировать перевод Rolling',[field('amount_usdt','Сумма, USDT'),field('network','Сеть','TRC20'),field('destination_address','Адрес получателя'),field('tx_hash','Хеш перевода'),field('sent_at','Время отправки (UTC)',kind='datetime-local'),field('comment','Комментарий',required=False),hidden('idempotency_key',uuid4().hex)],effect='Регистрируется уже отправленный перевод. Мерчант должен подтвердить его получение.'),action(f'rolling/merchants/{row.merchant_id}/suspension','Приостановка Rolling',[field('suspended','Приостановить',False,kind='checkbox'),reason()],danger=True)]
    if view.section=='integrations' and kind=='webhooks' and root and row.status!='delivered':
        forms.append(action(f'webhooks/{row.id}/retry','Повторить доставку',effect='Будет выполнена повторная попытка доставки. Денежный результат операции не изменится.'))
    if view.section=='control' and kind=='users':forms += account_actions(row,actor)
    return forms
