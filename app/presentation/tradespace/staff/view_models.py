from app.presentation.tradespace.financial_copy import financial_description, ENTRY_LABELS
"""SELECT-only staff queries and allowlisted display projections.

Queries preserve legacy collection ceilings before filtering/pagination. No expiration,
get-or-create balance, reconciliation repair, network call or secret consumption occurs here.
"""
from dataclasses import dataclass, field as datafield
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode
from uuid import UUID
from sqlalchemy import String, cast, func, or_, select
from fastapi import HTTPException
from app import models as m
from app.core.payment_methods import payment_method_label
from app.services.appeals import appeal_deadline
from app.services.deposit_ttl import deposit_deadline
from app.services.platform_income import platform_income_dashboard
from app.web.view_models import sanitize_response_snippet
from app.presentation.tradespace.merchant.view_models import DEPOSIT_STATUS,PAYOUT_STATUS,SETTLEMENT_STATUS,APPEAL_STATUS,WEBHOOK_STATUS,ROLLING_STATUS
from .access import STAFF_ROLES, MANAGERS, VIEWS, resolve_view, tabs_for
from . import forms as f
from app.presentation.tradespace.labels import reason_label, ROLE_LABELS

PAGE_SIZE=25
LABELS=dict(
 id='ID',created_at='Создано · UTC',updated_at='Изменено · UTC',external_id='Внешний ID',amount='Сумма',currency='Валюта',method='Способ оплаты',status='Состояние',merchant_id='Мерчант',requisites_id='Реквизит',requisite_id='Реквизит',expires_at='Срок оплаты · UTC',destination='Получатель',operation_type='Тип операции',operation_id='Операция',decision='Решение',email='Email',is_active='Активен',is_locked='Заблокирован',trader_traffic_status='Трафик',trader_traffic_priority='Приоритет',name='Название',owner_id='Владелец',sandbox_mode='Sandbox',webhook_url='Webhook URL',ip_whitelist='Разрешённые IP',bank_name='Банк / оператор',trader_id='Трейдер',traffic_status='Трафик',min_check='Минимальная сумма',max_check='Максимальная сумма',daily_limit='Дневной лимит',simultaneous_limit='Параллельных операций',min_payment_amount='Минимальный платёж',max_payment_amount='Максимальный платёж',monthly_limit='Месячный лимит',amount_usdt='К выплате',fee_usdt='Комиссия',total_debit_rub='Списание',processed_at='Обработано · UTC',available='Доступно',frozen='Заморожено',trader_balance='Баланс',trader_hold='Зарезервировано',trader_withdrawals_frozen='Вывод заморожен',sequence_no='Последовательность',recovered_usdt='Погашено',remaining_usdt='Остаток',confirmed_at='Подтверждено · UTC',teamlead_id='TeamLead',available_rub='Доступно',frozen_rub='Заморожено',debt_rub='Долг',total_earned_rub='Начислено всего',total_paid_rub='Выплачено всего',requested_usdt='К выплате',requested_at='Запрошено · UTC',deposit_id='Пополнение',commission_percent_snapshot='Ставка, %',accrual_rub='Начисление',balance='Баланс',hold_balance='Зарезервировано',total_turnover='Оборот',settlement_status='Учёт',merchant_fee_amount='Комиссия мерчанта',executor_fee_amount='Комиссия исполнителя',platform_income_amount='Маржа платформы',entity_type='Тип участника',entity_id='Участник',payment_method='Метод',min_amount='От суммы',max_amount='До суммы',rate_percent='Ставка, %',version='Версия',asset='Актив',network='Сеть',address='Адрес',label='Метка',change_reason='Причина изменения',event_type='Событие',attempts='Попыток',max_attempts='Лимит попыток',last_status_code='Последний HTTP',next_attempt_at='Следующая попытка · UTC',aggregator_order_id='ID агрегатора',merchant_order_id='ID заказа',aggregator_id='Агрегатор',callback_status='Callback',callback_attempts='Попыток callback',direction='Направление',related_payment_id='Платёж',attempt='Попытка',response_status_code='HTTP',next_retry_at='Повтор · UTC',severity='Важность',target_type='Объект',target_id='ID объекта',old_status='До',new_status='После',resolution_status='Рассмотрение',action='Действие',actor_id='Кто',ip='IP',role='Роль',twofa_enabled='2FA',antiscam_enabled='Антискам',auto_disable_traffic_enabled='Автоостановка',default_reinstate_mode='Возобновление',enabled='Включено',environment='Среда',auth_type='Авторизация',last_connection_test_at='Проверено · UTC',last_connection_test_status='Результат проверки',last_success_at='Последний успех · UTC',last_error_code='Код ошибки',description='Описание',entry_type='Запись',balance_after='Баланс после',hold_after='Резерв после',available_after='Доступно после',frozen_after='Заморожено после',debt_after='Долг после',amount_rub='Сумма, RUB',reason='Причина',tx_hash='Хеш перевода',trc20_address='Адрес TRC20',wallet_address='Адрес выплаты',rate_rub='Курс RUB',fee_rub='Комиссия RUB',reject_reason='Причина отказа',effective_from='Начало · UTC',effective_to='Конец · UTC',valid_from='Начало · UTC',valid_to='Конец · UTC',commission_percent='Ставка, %',executor_type='Исполнитель',executor_id='ID исполнителя',merchant_rate_percent='Ставка мерчанта, %',executor_rate_percent='Ставка исполнителя, %',platform_margin_percent='Маржа, %',calculation_base_amount='База расчёта',rate_snapshot_at='Зафиксировано · UTC',settled_at='Учтено · UTC',merchant_payable_amount='Обязательство мерчанту',failure_reason='Причина неуспеха',source_type='Источник',reversal_reason='Причина сторно',reversed_at='Сторно · UTC',principal_usdt='Сумма финансирования',outstanding_usdt='К погашению',rolling_applied_usdt='В погашение Rolling',settle_credited_rub='Зачислено в settle',merchant_payable_rub='К зачислению',merchant_fee_rub='Комиссия мерчанта',merchant_payable_usdt='К зачислению, USDT',rapira_rate_rub='Курс RUB',eligibility_status='Доступность Rolling',release_reason='Причина освобождения',last_error='Ошибка доставки',error='Ошибка',status_code='HTTP',attempt_no='Попытка',message='Сообщение',author_id='Автор',sent_at='Отправлено · UTC',cancel_reason='Причина отмены',dispute_reason='Причина спора',comment='Комментарий',destination_address='Адрес',rate_source='Источник курса',rate_side='Сторона курса',rate_symbol='Пара',gross_rub='База начисления',credited_to_available_rub='В доступные средства',applied_to_debt_rub='Погашение долга')
LABELS.update(key_id='ID ключа',retire_at='Окончание действия · UTC',owner_name='Получатель',full_name='Полное имя',request_count='Запросов за период',timeframe='Период',success_delay_minutes='Пауза после успеха, мин',operation_limit='Лимит операций',usage_count='Использований',last_success_at='Последний успех · UTC',risk_score='Оценка риска',callback_url='Callback URL',success_url='Success URL',fail_url='Fail URL')
STATES={**DEPOSIT_STATUS,**PAYOUT_STATUS,**SETTLEMENT_STATUS,**APPEAL_STATUS,**WEBHOOK_STATUS,**ROLLING_STATUS,'active':('Активен','positive'),'disabled':('Отключён','neutral'),'review':('На проверке','attention'),'settled':('Учтено','positive'),'credited':('Начислено','positive'),'reversed':('Сторнировано','neutral'),'blocked':('Заблокирован','critical'),'manual_paused':('Ручная пауза','attention'),'auto_paused':('Автопауза','critical')}


def number(v):
    return format(v,',f').replace(',',' ') if isinstance(v,Decimal) else str(v)

def display(value):
    if value is None or value=='':return '—'
    if isinstance(value,bool):return 'Да' if value else 'Нет'
    if isinstance(value,datetime):return value.astimezone(timezone.utc).strftime('%d.%m.%Y %H:%M:%S')
    if isinstance(value,Decimal):return number(value)
    if isinstance(value,list):return ', '.join(map(str,value)) or '—'
    return str(value)

def uuid_value(value):
    if not value:return None
    try:return UUID(str(value))
    except ValueError:raise HTTPException(400,'Некорректный ID фильтра')

def date_value(value):
    if not value:return None
    try:return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)
    except ValueError:raise HTTPException(400,'Некорректная дата')

def decimal_value(value):
    if not value:return None
    try:
        d=Decimal(value.replace(',','.'))
        if not d.is_finite():raise ValueError()
        return d
    except (InvalidOperation,ValueError):raise HTTPException(400,'Некорректная сумма')

def page_value(value):
    try:return min(10000,max(1,int(value or 1)))
    except ValueError:raise HTTPException(400,'Некорректная страница')

def link(base,section,tab='',**params):
    args={k:str(v) for k,v in {'tab':tab,**params}.items() if v is not None and v!=''}
    return base+'/tradespace/'+section+('?' + urlencode(args) if args else '')

def scope(view):
    if view.model is m.User:
        roles={'traders':('operator','trader'),'teamleads':('teamlead',),'users':('admin','superadmin','support')}.get(view.slug,())
        return [m.User.role.in_(roles),m.User.is_archived.is_(False)]
    return [view.model.is_archived.is_(False)] if hasattr(view.model,'is_archived') else []

def bounded(view):
    cls=view.model
    return select(cls.id).where(*scope(view)).order_by(cls.created_at.desc(),cls.id.desc()).limit(view.limit)

@dataclass
class Page:
    section: str
    tab: str=''
    title: str=''
    tabs: tuple=()
    columns: list=datafield(default_factory=list)
    rows: list=datafield(default_factory=list)
    selected: dict|None=None
    panels: list=datafield(default_factory=list)
    actions: list=datafield(default_factory=list)
    cards: list=datafield(default_factory=list)
    links: list=datafield(default_factory=list)
    filters: dict=datafield(default_factory=dict)
    filter_fields: tuple=()
    statuses: list=datafield(default_factory=list)
    page: int=1
    pages: int=1
    count: int=0
    ceiling: int=0
    previous: str=''
    next: str=''
    note: str=''
    notice: str=''


def cell(key,value,*,money=(),currency='RUB',user_comment=False):
    rendered=display(value)
    result=dict(key=key,label=LABELS.get(key,key),value=rendered,tone='',money=False,currency='')
    if key in money and value is not None:
        result.update(money=True,currency='USDT' if key.endswith('_usdt') else ('RUB' if key.endswith('_rub') else currency))
    elif key in {'status','callback_status','traffic_status','trader_traffic_status','settlement_status','resolution_status','old_status','new_status','eligibility_status','last_connection_test_status'}:
        label,tone=STATES.get(str(value),('Статус не определён' if value else '—','neutral'));result.update(value=label,tone=tone,raw=display(value))
    elif key in {'reason','failure_reason','reject_reason','release_reason','cancel_reason'}:result['value']=reason_label(value) or '—'
    elif key=='description':result['value']=financial_description(value,user_comment=user_comment) or '—'
    elif key=='entry_type':result['value']=ENTRY_LABELS.get(str(value),'Финансовое движение')
    elif key=='source_type':result['value']={'trader_referral':'За трейдера','merchant_referral':'За мерчанта'}.get(str(value),'Источник не указан')
    elif key=='role':result['value']=ROLE_LABELS.get(str(value),'Роль не определена')
    elif key in {'method','payment_method'}:result['value']=payment_method_label(value)
    return result

def status_map(tab):
    specific={'deposits':DEPOSIT_STATUS,'payouts':PAYOUT_STATUS,'settlements':SETTLEMENT_STATUS,'teamlead-settlements':SETTLEMENT_STATUS,'appeals':APPEAL_STATUS,'webhooks':WEBHOOK_STATUS,'rolling':ROLLING_STATUS}.get(tab,{})
    return {**STATES,**specific}

def table_fields(view):
    identity=next((k for k in ('name','email','external_id','aggregator_order_id') if k in view.fields),None)
    fields=[k for k in view.fields if k!=identity and not (k=='currency' and view.money)]
    if 'status' in fields:fields=['status']+[k for k in fields if k!='status']
    return fields

def row_view(row,view,base,*,detail=False):
    fields=view.fields if detail else table_fields(view)
    cells=[cell(k,getattr(row,k),money=view.money,currency=getattr(row,'currency',view.currency)) for k in fields]
    for c in cells:
        if c['key']=='status':
            label,tone=status_map(view.slug).get(str(row.status),('Статус не определён','neutral'))
            c.update(value=label,tone=tone)
    return dict(id=str(row.id),title=display(next((getattr(row,k) for k in ('name','email','external_id','aggregator_order_id') if hasattr(row,k)),row.id)),created=display(row.created_at),url=link(base,view.section,view.slug,id=row.id),cells=cells)

async def name_cells(db,actor,page):
    cells=[c for r in page.rows for c in r['cells']]+(page.selected['fields'] if page.selected else [])
    def ids(keys):
        result=set()
        for c in cells:
            if c['key'] in keys:
                try:result.add(UUID(c['value']))
                except (ValueError,TypeError):pass
        return result
    labels={}
    merchant_ids=ids({'merchant_id'})
    if merchant_ids:labels.update({str(i):name for i,name in (await db.execute(select(m.Merchant.id,m.Merchant.name).where(m.Merchant.id.in_(merchant_ids)))).all()})
    user_ids=ids({'trader_id','teamlead_id','owner_id','actor_id'})
    roles=('trader','operator') if actor.role=='support' else ('trader','operator','teamlead','merchant')
    if actor.role=='superadmin':roles=(*roles,'admin','superadmin','support')
    if user_ids:labels.update({str(i):email for i,email in (await db.execute(select(m.User.id,m.User.email).where(m.User.id.in_(user_ids),m.User.role.in_(roles)))).all()})
    aggregator_ids=ids({'aggregator_id'})
    if aggregator_ids:labels.update({str(i):name for i,name in (await db.execute(select(m.AggregatorAccount.id,m.AggregatorAccount.name).where(m.AggregatorAccount.id.in_(aggregator_ids)))).all()})
    for c in cells:
        if c['value'] in labels:c.update(reference=c['value'],value=labels[c['value']])


def panel(title,rows,fields,*,money=(),currency='RUB'):
    rendered=[]
    for row in rows:
        manual = getattr(row,'entry_type','') in {'balance_adjustment','insurance_deposit_set','manual_adjustment','write_off','settlement_release','accrual_reversal'} or str(getattr(row,'idempotency_key','') or '').startswith('manual')
        cells=[]
        for key in fields:
            value=getattr(row,key,None)
            item=cell(key,value,money=money,currency=getattr(row,'currency',currency),user_comment=manual)
            if key=='reason' and isinstance(row,(m.TeamLeadLedgerEntry,m.MerchantRollingLedgerEntry)):
                item['value']=financial_description(value,user_comment=manual) or '—'
            cells.append(item)
        rendered.append(cells)
    return dict(title=title,columns=[LABELS.get(k,k) for k in fields],rows=rendered)

async def related(db,cls,condition,limit=100):
    return (await db.scalars(select(cls).where(condition).order_by(cls.created_at.desc(),cls.id.desc()).limit(limit))).all()

async def choices(db,actor):
    if actor.role not in MANAGERS:return {}
    result={}
    for slug,cls,cond,label in [('traders',m.User,m.User.role.in_(('operator','trader')),m.User.email),('merchants',m.Merchant,m.Merchant.is_archived.is_(False),m.Merchant.name)]:
        rows=(await db.execute(select(cls.id,label).where(cond,cls.is_archived.is_(False)).order_by(label).limit(300))).all()
        result[slug]=[(str(i),name) for i,name in rows]
    return result

async def center(db,actor,base):
    p=Page('center',title='Центр управления')
    specs=[('Ожидают оплаты','operations','deposits',m.Deposit.status.in_(('created','pending','processing')),'Открыть очередь'),('Открытые обращения','operations','appeals',m.Appeal.status.in_(('opened','in_review')),'Рассмотреть'),('Расчёты ожидают обработки','finance','settlements',m.MerchantSettlement.status=='pending','Открыть расчёты'),('Доставка требует внимания','integrations','webhooks',m.WebhookEvent.status.in_(('failed','dead','retry','configuration_required')),'Проверить доставку')]
    for title,section,tab,condition,cta in specs:
        view=resolve_view(actor.role,section,tab)
        count=await db.scalar(select(func.count()).select_from(view.model).where(view.model.id.in_(bounded(view)),condition))
        p.cards.append(dict(title=title,value=count,href=link(base,section,tab,attention='1'),cta=cta,caption=f'В пределах последних {view.limit} записей'))
    p.links=[dict(title='Пополнения и выплаты',text='Рабочая очередь, детали и обращения',href=link(base,'operations','deposits')),dict(title='Участники сети',text='Трейдеры, мерчанты и агрегаторы',href=link(base,'network','traders')),dict(title='Расчёты и учёт',text='Балансы, начисления и расчёты',href=link(base,'finance','settlements'))]
    p.note='Состояние на момент обновления. Выберите очередь для обработки.'
    return p

async def load_staff_page(db,actor,*,cabinet_base,query_params,section,selected_operation_id=None):
    if actor.role not in STAFF_ROLES:raise HTTPException(403,'Недостаточно прав')
    if section in {'center','notifications'}:return await center(db,actor,cabinet_base)
    tab=str(query_params.get('tab',''))
    if selected_operation_id:tab='deposits'
    view=resolve_view(actor.role,section,tab)
    if view is None:raise HTTPException(403,'Раздел недоступен этой роли')
    # Validate even irrelevant ID filters; they never change the selected scope/model.
    filters={k:str(query_params.get(k,'')).strip() for k in ('q','status','method','date_from','date_to','amount_min','amount_max','merchant_id','trader_id','requisite_id','sort','attention')}
    filters['q']=filters['q'][:160]
    for key in ('merchant_id','trader_id','requisite_id'):uuid_value(filters[key])
    for key in ('date_from','date_to'):date_value(filters[key])
    for key in ('amount_min','amount_max'):decimal_value(filters[key])
    p=Page(section,view.slug,view.title,tabs_for(actor.role,section),filters=filters,page=page_value(query_params.get('page')),ceiling=view.limit,note=view.note)
    cls=view.model
    p.columns=[LABELS.get(k,k) for k in table_fields(view)]
    p.filter_fields=tuple(k for k in ('status','method','amount','merchant_id','trader_id','requisites_id') if hasattr(cls,k))
    conditions=[cls.id.in_(bounded(view))]
    if filters['q']:
        term='%'+filters['q'].replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%'
        columns=[cls.id]+[getattr(cls,k) for k in ('email','name','external_id','merchant_order_id','aggregator_order_id','operation_id','event_type','action') if hasattr(cls,k)]
        conditions.append(or_(*(cast(c,String).ilike(term,escape='\\') for c in columns)))
    for key in ('status','method','merchant_id','trader_id'):
        if filters[key] and hasattr(cls,key):conditions.append(getattr(cls,key)==(uuid_value(filters[key]) if key.endswith('_id') else filters[key]))
    if filters['requisite_id'] and cls is m.Deposit:conditions.append(cls.requisites_id==uuid_value(filters['requisite_id']))
    if filters['trader_id'] and cls is m.Deposit:conditions.append(cls.requisites_id.in_(select(m.Requisite.id).where(m.Requisite.trader_id==uuid_value(filters['trader_id']))))
    if filters['date_from']:conditions.append(cls.created_at>=date_value(filters['date_from']))
    if filters['date_to']:conditions.append(cls.created_at<date_value(filters['date_to'])+timedelta(days=1))
    if hasattr(cls,'amount'):
        if filters['amount_min']:conditions.append(cls.amount>=decimal_value(filters['amount_min']))
        if filters['amount_max']:conditions.append(cls.amount<=decimal_value(filters['amount_max']))
    if filters['attention']=='1':
        states={'deposits':('created','pending','processing'),'appeals':('opened','in_review'),'settlements':('pending',),'webhooks':('failed','dead','retry','configuration_required')}.get(view.slug)
        if states:conditions.append(cls.status.in_(states))
    if hasattr(cls,'status'):p.statuses=[str(v) for v in (await db.scalars(select(cls.status).where(cls.id.in_(bounded(view))).distinct().order_by(cls.status))).all()]
    if view.slug!='access':
        p.count=await db.scalar(select(func.count()).select_from(cls).where(*conditions))
        p.pages=max(1,(p.count+PAGE_SIZE-1)//PAGE_SIZE);p.page=min(p.page,p.pages)
        sort=filters['sort']; order=cls.created_at.asc() if sort=='oldest' else cls.created_at.desc()
        if sort in {'amount_asc','amount_desc'} and hasattr(cls,'amount'):order=cls.amount.asc() if sort=='amount_asc' else cls.amount.desc()
        rows=(await db.scalars(select(cls).where(*conditions).order_by(order,cls.id.desc()).offset((p.page-1)*PAGE_SIZE).limit(PAGE_SIZE))).all()
        p.rows=[row_view(r,view,cabinet_base) for r in rows]
    p.previous=link(cabinet_base,section,view.slug,**filters,page=p.page-1) if p.page>1 else ''
    p.next=link(cabinet_base,section,view.slug,**filters,page=p.page+1) if p.page<p.pages else ''
    opts=await choices(db,actor) if section in {'network','control'} else {}
    if section=='network' and actor.role in MANAGERS and not query_params.get('id'):
        if view.slug in {'traders','merchants','teamleads'}:p.actions.append(f.create_user(actor.role,selected={'traders':'trader','merchants':'merchant','teamleads':'teamlead'}[view.slug]))
        if view.slug=='aggregators':p.actions.append(f.action('aggregators/create','Создать агрегатора',f.aggregator_fields(),effect='Учётные данные будут показаны один раз. Сохраните их сразу.'))
        if view.slug=='requisites':p.actions.append(f.action('requisites/create','Добавить реквизит',f.requisite_fields(traders=opts.get('traders',()))))
    if section=='control' and view.slug in {'users','access'} and not query_params.get('id'):p.actions.append(f.create_user(actor.role,staff=True))
    raw_id=query_params.get('id')
    if cls is m.AntiscamGlobalSettings and raw_id:
        if str(raw_id)!='1':raise HTTPException(404,'Запись не найдена')
        selected=1
    else:selected=selected_operation_id or uuid_value(raw_id)
    if selected:
        row=await db.scalar(select(cls).where(cls.id==selected,*scope(view)))
        if row is None:raise HTTPException(404,'Запись не найдена')
        p.selected=row_view(row,view,cabinet_base,detail=True)
        detail_cells=sorted(p.selected['cells'],key=lambda c:0 if c['key']=='status' else (1 if c['money'] else 2))
        p.selected['fields']=[*detail_cells,cell('id',row.id),cell('created_at',row.created_at),cell('updated_at',getattr(row,'updated_at',None))]
        p.selected['back']=link(cabinet_base,section,view.slug,**filters,page=p.page)
        await detail(db,actor,view,row,p,cabinet_base,opts)
    if section=='finance' and view.slug=='income':
        # Currency is fixed to RUB: the existing service's monetary totals are not cross-currency.
        income=await platform_income_dashboard(db,currency='RUB',page=page_value(query_params.get('page')),page_size=25,date_from=date_value(filters['date_from']),date_to=date_value(filters['date_to'])+timedelta(days=1) if filters['date_to'] else None,merchant_id=uuid_value(filters['merchant_id']),trader_id=uuid_value(filters['trader_id']),payment_method=filters['method'] or None,operation_status=filters['status'] or None,amount_min=decimal_value(filters['amount_min']),amount_max=decimal_value(filters['amount_max']))
        p.cards=[dict(title=label,value=number(income['totals'][key]),caption='RUB · существующий финансовый отчёт',href='',cta='') for key,label in [('platform_income','Gross margin'),('net_teamlead_expense','Расход TeamLead с учётом сторно'),('net_platform_income','Net margin'),('reconciliation_delta','Расхождение учёта')]]
        p.rows=[row_view(r['snapshot'],view,cabinet_base) for r in income['rows']]
        p.count=income['totals']['operations_count'];p.page=income['page'];p.pages=income['total_pages'];p.ceiling=0
        p.filter_fields=('method','amount','merchant_id','trader_id')
        p.previous=link(cabinet_base,section,view.slug,**filters,page=p.page-1) if p.page>1 else ''
        p.next=link(cabinet_base,section,view.slug,**filters,page=p.page+1) if p.page<p.pages else ''
        p.note='Отчёт в RUB за выбранный период. Доход и расходы TeamLead показаны отдельно.'
    if section=='finance' and view.slug=='wallet' and actor.role=='superadmin':p.actions.append(f.action('platform-wallet','Изменить адрес пополнения',[f.field('address','Адрес USDT TRC20'),f.field('label','Метка',required=False),f.field('change_reason','Причина',kind='textarea')],effect='Изменится активный адрес, который видят пользователи при пополнении.',danger=True))
    if section=='control' and view.slug in {'ai','antiscam'}:await control_forms(db,view,p)
    await name_cells(db,actor,p)
    return p

async def detail(db,actor,view,row,p,base,opts):
    from app.web import routes as legacy
    from app.services.merchant_api_keys import api_key_fingerprint
    api_keys=[];signing=[];rules=[];risk_settings=None
    root=actor.role=='superadmin';manage=actor.role in MANAGERS
    kind=view.slug
    def add_fields(names,money=()):p.selected['fields'] += [cell(n,getattr(row,n,None),money=money,currency=getattr(row,'currency',view.currency)) for n in names if n not in view.fields]
    if view.section=='operations' and kind in {'deposits','payouts'}:
        merchant=await db.scalar(select(m.Merchant).where(m.Merchant.id==row.merchant_id))
        if merchant:p.links.append(dict(title=merchant.name,text='Мерчант',href=link(base,'network','merchants',id=merchant.id)))
        if kind=='deposits':
            req=await db.scalar(select(m.Requisite).where(m.Requisite.id==row.requisites_id)) if row.requisites_id else None
            if req and legacy._can_view_requisite_value(actor,req):
                p.panels.append(dict(title='Назначение',columns=['Параметр','Значение'],rows=[[cell('name','Реквизит'),cell('value',legacy.display_requisite_value(req))],[cell('name','Получатель'),cell('value',req.full_name or req.owner_name)],[cell('name','Банк / оператор'),cell('value',req.bank_name)],[cell('name','Срок по правилам операции · UTC'),cell('value',deposit_deadline(row))]]))
                trader=await db.scalar(select(m.User).where(m.User.id==req.trader_id,m.User.role.in_(('operator','trader'))))
                if trader:p.links.append(dict(title=trader.email,text='Трейдер',href=link(base,'network','traders',id=trader.id)))
            metadata=row.metadata_json or {}
            p.selected['fields'].append(cell('failure_reason',metadata.get('failure_reason')))
            if manage:
                snap=await db.scalar(select(m.OperationFeeSnapshot).where(m.OperationFeeSnapshot.deposit_id==row.id))
                if snap:p.panels.append(panel('Условия расчёта комиссии',[snap],('merchant_rate_percent','merchant_fee_amount','executor_type','executor_rate_percent','executor_fee_amount','platform_margin_percent','platform_income_amount','calculation_base_amount','currency','settlement_status','rate_snapshot_at','settled_at'),money=('merchant_fee_amount','executor_fee_amount','platform_income_amount','calculation_base_amount')))
                else:p.notice='Сохранённые условия комиссии отсутствуют. Текущие тарифы к этой записи не применяются.'
                p.selected['fields'].append(cell('merchant_payable_amount',metadata.get('merchant_payable_amount',metadata.get('merchant_net_amount')),money=('merchant_payable_amount',)))
        appeals=await related(db,m.Appeal,m.Appeal.operation_id==row.id,100)
        p.links += [dict(title='Обращение · '+STATES.get(a.status,(a.status,''))[0],text=str(a.id),href=link(base,'operations','appeals',id=a.id)) for a in appeals]
        events=await related(db,m.WebhookEvent,m.WebhookEvent.payload['operation_id'].as_string()==str(row.id),100)
        # Current payload uses id; support both historic contract shapes without raw payload output.
        if not events:events=await related(db,m.WebhookEvent,m.WebhookEvent.payload['id'].as_string()==str(row.id),100)
        p.links += [dict(title='Webhook · '+STATES.get(e.status,(e.status,''))[0],text=e.event_type,href=link(base,'integrations','webhooks',id=e.id)) for e in events]
        if root:
            audits=await related(db,m.AuditLog,m.AuditLog.target_id==str(row.id),100)
            p.panels.append(panel('История зафиксированных действий',audits,('created_at','action','actor_id','target_type','target_id')))
    if view.section=='operations' and kind=='appeals':
        messages=await related(db,m.AppealMessage,m.AppealMessage.appeal_id==row.id,300)
        p.panels.append(panel('Сообщения · последние 300',list(reversed(messages)),('created_at','author_id','message')))
        p.selected['fields'].append(cell('expires_at',appeal_deadline(row)))
        p.links.append(dict(title='Связанная операция',text=row.operation_type,href=link(base,'operations','deposits' if row.operation_type=='deposit' else 'payouts',id=row.operation_id)))
        for msg in messages:
            if msg.attachment_path and '/' not in msg.attachment_path and '\\' not in msg.attachment_path:p.links.append(dict(title='Вложение к сообщению',text=display(msg.created_at),href=base+'/appeals/files/'+msg.attachment_path))
        for key,label in [('receipt_file','Чек'),('statement_file','Выписка')]:
            info=(row.metadata_json or {}).get(key)
            if isinstance(info,dict):
                path=str(info.get('path',''))
                if path and '/' not in path and '\\' not in path:p.links.append(dict(title=label,text='Защищённый просмотр файла',href=legacy._appeal_file_info_url(info,base)))
    if view.section in {'network','integrations'} and kind in {'merchants','credentials'} and manage:
        from app.services.integration_modes import integration_status
        mode_state = await integration_status(db, row)
        mode_rows = [[cell('mode', mode_state['label'])]]
        mode_rows += [[cell('reason', r['message'])] for r in mode_state['blocking_reasons']]
        p.panels.append(dict(title='Режим интеграции · отдельное окружение и credentials', columns=['Состояние / условия'], rows=mode_rows))
        if root:
            command = 'suspend' if mode_state['can_suspend'] else 'activate'
            confirmation = 'SUSPEND' if command == 'suspend' else 'PRODUCTION'
            action = f.action(f'merchants/{row.id}/production/{command}',
                'Приостановить Production' if command == 'suspend' else 'Подключить Production',
                [f.field('confirmation','Введите '+confirmation), f.reason()], danger=True,
                effect='Изменится доступ к боевому API. Данные и балансы Sandbox не переносятся. Действие будет записано в аудит.')
            action['disabled'] = not (mode_state['can_suspend'] or mode_state['can_activate'])
            p.actions.append(action)
        api_keys=await related(db,m.ApiKey,m.ApiKey.merchant_id==row.id,100)
        p.panels.append(dict(title='API-ключи · только отпечатки',columns=['Отпечаток','Режим','Активен','Последнее использование'],rows=[[cell('key',api_key_fingerprint(k.api_key)),cell('mode',k.mode),cell('is_active',k.is_active),cell('time',k.last_used_at)] for k in api_keys]))
        if root:
            signing=await related(db,m.MerchantWebhookSigningKey,m.MerchantWebhookSigningKey.merchant_id==row.id,100)
            p.panels.append(panel('Ключи подписи Webhook',signing,('key_id','status','created_at','retire_at')))
        owner=await db.scalar(select(m.User).where(m.User.id==row.owner_id,m.User.role=='merchant'))
        if owner and root:p.actions+=f.account_actions(owner,actor)
        if root:
            balances=await related(db,m.Balance,m.Balance.merchant_id==row.id,100)
            p.links += [dict(title='Средства мерчанта · '+b.currency,text='Баланс, записи и Rolling',href=link(base,'finance','merchants',id=b.id)) for b in balances]
    if view.section=='network' and manage and kind in {'merchants','traders','aggregators'}:
        entity={'merchants':'merchant','traders':'trader','aggregators':'aggregator'}[kind]
        rules=await related(db,m.FeeRule,(m.FeeRule.entity_id==row.id)&(m.FeeRule.entity_type==entity),500)
        p.panels.append(panel('Тарифы и версии',rules,('payment_method','min_amount','max_amount','rate_percent','version','is_active','effective_from','effective_to'),money=('min_amount','max_amount')))
    if view.section=='network' and kind=='aggregators' and manage:
        from app.services.aggregator_credentials import aggregator_integration_status
        state = await aggregator_integration_status(db, row)
        p.panels.append(dict(title='Режим интеграции агрегатора',columns=['Состояние / условия'],rows=
            [[cell('mode',state['label'])]]+[[cell('reason',x['message'])] for x in state['blocking_reasons']]))
        labels={'active':'Выдан','suspended':'Приостановлен','revoked':'Отозван'}
        p.panels.append(dict(title='Ключи агрегатора · секреты не отображаются',columns=['ID ключа','Окружение','Состояние','Последнее использование'],rows=
            [[cell('id',k['id']),cell('mode',k['mode'].title()),cell('status',labels[k['status']]),cell('time',k['last_used_at'])] for k in state['keys']]))
        if root:
            mode=state['environment'];current=next((k for k in state['keys'] if k['mode']==mode and k['status']!='revoked'),None)
            command='rotate' if current else 'issue'
            fields=[f.hidden('mode',mode),f.field('confirmation','Введите '+mode.upper()),f.reason()]
            if current: fields.append(f.hidden('key_id',current['id']))
            item=f.action(f'aggregators/{row.id}/credentials/{command}',('Заменить' if current else 'Выпустить')+' '+mode.title()+'-ключ',fields,
                effect='Секрет показывается один раз. Он используется для запросов API и подписи callbacks. При замене прежние подписи перестают работать. Выпуск ключа не активирует Production.',danger=True)
            item['disabled']=not state['can_rotate' if current else 'can_issue'];p.actions.append(item)
            for cmd,label in [('suspend','Приостановить ключ'),('revoke','Отозвать ключ')]:
                if current and (cmd=='revoke' or current['status']=='active'):
                    p.actions.append(f.action(f'aggregators/{row.id}/credentials/{cmd}',label,
                        [f.hidden('mode',mode),f.hidden('key_id',current['id']),f.field('confirmation','Введите '+cmd.upper()),f.reason()],danger=True,
                        effect='Новые API-запросы с этим ключом будут отклонены. Уже принятые операции и callbacks завершаются по прежним правилам.'))
            cmd='suspend' if state['can_suspend'] else 'activate'
            item=f.action(f'aggregators/{row.id}/production/{cmd}','Приостановить Production' if cmd=='suspend' else 'Подключить Production',
                [f.field('confirmation','Введите '+('SUSPEND' if cmd=='suspend' else 'PRODUCTION')),f.reason()],danger=True,
                effect='Изменится допуск агрегатора к боевому API. Тестовые операции и балансы не переносятся. Действие записывается в аудит.')
            item['disabled']=not(state['can_suspend'] or state['can_activate']);p.actions.append(item)
    if view.section=='network' and kind=='traders':
        reqs=await related(db,m.Requisite,m.Requisite.trader_id==row.id,300)
        if manage:p.links += [dict(title=r.bank_name or 'Реквизит',text=display(r.status),href=link(base,'network','requisites',id=r.id)) for r in reqs]
        if root:risk_settings=await db.scalar(select(m.TraderAntiscamSettings).where(m.TraderAntiscamSettings.trader_id==row.id))
    if view.section=='network' and kind=='requisites':
        if legacy._can_view_requisite_value(actor,row):
            p.selected['fields'].append(dict(key='requisite_value',label='Платёжный реквизит',value=legacy.display_requisite_value(row),tone='',money=False,currency=''))
        add_fields(('owner_name','full_name','request_count','timeframe','success_delay_minutes','operation_limit','usage_count','last_success_at','enabled','risk_score'))
        if root:risk_settings=await db.scalar(select(m.RequisiteAntiscamSettings).where(m.RequisiteAntiscamSettings.requisite_id==row.id))
    if view.section=='network' and kind in {'teamleads','traders','merchants'} and manage:
        for model,related_key,fields,title in [(m.TeamLeadTraderAssignment,'trader_id',('teamlead_id','trader_id','effective_from','effective_to'),'Назначения трейдеров'),(m.TeamLeadMerchantAssignment,'merchant_id',('teamlead_id','merchant_id','valid_from','valid_to'),'Назначения мерчантов')]:
            if kind!='teamleads' and related_key!=kind[:-1]+'_id':continue
            condition=model.teamlead_id==row.id if kind=='teamleads' else getattr(model,related_key)==row.id
            assignments=await related(db,model,condition,300)
            p.panels.append(panel(title,assignments,fields+(('commission_percent',) if root else ())))
    if view.section=='network' and kind=='aggregators':
        add_fields(('callback_url','success_url','fail_url'))
        p.links.append(dict(title='Финансы агрегатора',text='Баланс, hold и оборот',href=link(base,'finance','aggregators',id=row.id)))
    if view.section=='finance':
        if kind in {'settlements','teamlead-settlements'}:
            add_fields(('tx_hash','reject_reason','network','trc20_address','wallet_address','rate_rub','rapira_rate_rub','rate_source','rate_side','rate_symbol','fee_rub','amount_rub','processed_at','completed_at','rejected_at'),money=('fee_rub','amount_rub'))
            if kind=='settlements' and row.status=='pending':p.notice='Подтверждайте выплату только после фактической отправки средств.'
        ledger_spec={'traders':(m.TraderLedgerEntry,m.TraderLedgerEntry.trader_id==row.id,('created_at','entry_type','amount','balance_after','hold_after','operation_id','description')),'merchants':(m.LedgerEntry,m.LedgerEntry.merchant_id==getattr(row,'merchant_id',None),('created_at','entry_type','amount','operation_id','description')),'teamleads':(m.TeamLeadLedgerEntry,m.TeamLeadLedgerEntry.teamlead_id==getattr(row,'teamlead_id',None),('created_at','entry_type','amount_rub','available_after','frozen_after','debt_after','source_type','reason'))}
        if kind in ledger_spec:
            model,condition,fields=ledger_spec[kind];entries=await related(db,model,condition,200)
            p.panels.append(panel('Финансовые записи · последние 200',entries,fields,money=('amount','amount_rub','balance_after','hold_after','available_after','frozen_after','debt_after')))
        if kind=='merchants':
            account=await db.scalar(select(m.MerchantRollingAccount).where(m.MerchantRollingAccount.merchant_id==row.merchant_id))
            p.panels.append(panel('Rolling account',[account] if account else [],('status','principal_usdt','recovered_usdt','outstanding_usdt'),money=('principal_usdt','recovered_usdt','outstanding_usdt'),currency='USDT'))
            allocations=await related(db,m.MerchantRollingAllocation,m.MerchantRollingAllocation.merchant_id==row.merchant_id,100)
            p.panels.append(panel('Распределение Rolling / settle',allocations,('deposit_id','status','merchant_payable_rub','rolling_applied_usdt','settle_credited_rub','eligibility_status'),money=('merchant_payable_rub','rolling_applied_usdt','settle_credited_rub')))
        if kind=='rolling':
            add_fields(('tx_hash','network','destination_address','sent_at','comment','dispute_reason','cancel_reason'))
            consumption=await related(db,m.MerchantRollingTransferConsumption,m.MerchantRollingTransferConsumption.rolling_transfer_id==row.id,300)
            p.panels.append(panel('Погашение по операциям',consumption,('created_at','deposit_id','entry_type','amount_usdt','amount_rub','rate_rub'),money=('amount_usdt','amount_rub')))
        if kind in {'accruals','merchant-accruals'}:add_fields(('gross_rub','credited_to_available_rub','applied_to_debt_rub','reversed_at','reversal_reason'),money=('gross_rub','credited_to_available_rub','applied_to_debt_rub'))
    if view.section=='integrations' and kind=='webhooks':
        from app.web.view_models import build_attempts_by_event_id
        attempts=await related(db,m.WebhookDeliveryAttempt,m.WebhookDeliveryAttempt.webhook_event_id==row.id,100)
        safe=build_attempts_by_event_id(attempts).get(row.id,[])
        p.panels.append(dict(title='История доставки',columns=['Попытка','Время','HTTP','Состояние','Ошибка'],rows=[[cell('attempt_no',a.attempt_no),cell('created_at',a.created_at),cell('status_code',a.status_code),cell('status',a.status),cell('error',a.error)] for a in safe]))
        payload=row.payload if isinstance(row.payload,dict) else {}
        p.panels.append(dict(title='Семантика события',columns=['Поле','Значение'],rows=[[cell('name',LABELS.get(k,k)),cell(k,payload[k])] for k in ('id','operation_id','external_id','event','event_type','operation_type','status','amount','currency') if k in payload]))
        p.selected['fields'].append(cell('last_error',sanitize_response_snippet(row.last_error,limit=500)))
    if view.section=='integrations' and kind=='callbacks':
        p.selected['fields'].append(cell('error',sanitize_response_snippet(row.error_message,limit=500)))
    if view.section=='control' and kind=='audit':
        from app.web.view_models import build_audit_view
        safe=build_audit_view(row,str(row.actor_id or 'Система'))
        # No raw details JSON; even unknown values in the audit record stay out of the DOM.
        p.selected['fields'] += [cell(k,sanitize_response_snippet(safe[k],limit=600)) for k in ('old_status','new_status','reason')]
    if view.section=='control' and kind=='risk':
        p.selected['fields'].append(cell('reason',sanitize_response_snippet(row.reason,limit=600)))
    p.actions += f.detail_actions(view,row,actor,choices=opts,rules=rules,api_keys=api_keys,signing_keys=signing,risk_settings=risk_settings)


async def control_forms(db,view,p):
    if view.slug=='antiscam':
        row=await db.scalar(select(m.AntiscamGlobalSettings).limit(1))
        names=('antiscam_enabled','auto_disable_traffic_enabled','failed_payments_in_row_limit_requisite','failed_payments_in_row_limit_trader','min_payments_for_conversion_check','conversion_check_window_minutes','min_requisite_conversion_percent','min_trader_conversion_percent','conversion_drop_percent_limit','max_confirmation_delay_minutes','merchant_complaints_limit_requisite','merchant_complaints_limit_trader','high_amount_extra_risk_enabled','high_amount_threshold','freeze_withdrawals_on_trader_auto_pause','default_reinstate_mode','limited_reinstate_duration_minutes','limited_reinstate_max_active_payments','limited_reinstate_max_amount')
        # Existing command defaults, not persisted by this GET when the singleton is absent.
        defaults=(True,True,5,10,20,60,'35','45','40',10,3,5,False,'100000',True,'limited',120,3,'30000')
        p.actions.append(f.action('antiscam/settings','Настроить антискам',[f.setting_field(n,getattr(row,n,d)) for n,d in zip(names,defaults)],effect='Изменятся общие действующие параметры риска и автоматической остановки трафика.',danger=True))
        p.links.append(dict(title='Индивидуальные настройки',text='Откройте карточку трейдера или реквизита',href=link('/staff/cabinet','network','traders')))
    if view.slug=='ai':
        from app.services.ai_office import ai_config_view,AI_OFFICE_EVENT_OPTIONS
        row=await db.scalar(select(m.AIIntegrationConfig).limit(1));cfg=ai_config_view(row)
        p.panels.append(dict(title='Состояние секретов',columns=['Секрет','Настроен'],rows=[
            [cell('name',label),cell('value','Настроен (configured)' if cfg[key+'_configured'] else 'Не настроен')]
            for key,label in [('api_key','API key'),('bearer_token','Bearer token'),('hmac_secret','HMAC secret')]
        ]))
        fields=[f.field('enabled','Интеграция включена',cfg['enabled'],kind='checkbox'),f.choice('environment','Среда',[('local','Local'),('staging','Staging'),('production','Production')],cfg['environment']),f.field('base_url','Base URL',cfg['base_url'],required=False),f.field('health_path','Health path',cfg['health_path']),f.field('api_version','API version',cfg['api_version'],required=False),f.choice('auth_type','Авторизация',[(x,x) for x in ('none','api_key','bearer','hmac')],cfg['auth_type'])]
        fields += [f.field(n,label,kind='password',required=False,hint='Пустое поле сохраняет настроенный секрет.') for n,label in [('api_key','Новый API key'),('bearer_token','Новый Bearer token'),('hmac_secret','Новый HMAC secret')]]
        fields += [f.field(n,l,cfg[n],kind='number') for n,l in [('timeout_seconds','Timeout, секунд'),('connect_timeout_seconds','Connect timeout, секунд'),('max_retries','Повторов')]]
        fields += [f.field('verify_tls','Проверять TLS',cfg['verify_tls'],kind='checkbox'),f.choice('selected_events','События',[(x,x) for x in sorted(AI_OFFICE_EVENT_OPTIONS)],cfg['selected_events'],multiple=True,required=False),f.field('change_reason','Причина',kind='textarea')]
        p.actions.append(f.action('ai-office/config','Настроить AI-интеграцию',fields,effect='Изменятся параметры внешнего подключения. Входящие команды не включаются.'))
        if row:
            p.actions.append(f.action('ai-office/test-connection','Проверить соединение',effect='Будет выполнен существующий запрос к настроенному внешнему сервису.'))
            for key,label in [('api_key','API key'),('bearer_token','Bearer token'),('hmac_secret','HMAC secret')]:
                if cfg[key+'_configured']:p.actions.append(f.action('ai-office/config/secrets/'+key+'/clear','Удалить '+label,[f.field('change_reason','Причина')],danger=True))
        p.note='Только сохранённый результат проверки соединения. Новая проверка выполняется явно по кнопке; секреты не отображаются.'
