# Webhook и QR NSPK — локальное исправление

Дата: 2026-10-01. Только локальные изменения. Push, Production, банковских запросов и переводов не было.

## Исходное состояние и сохранность

TradeSpace: `codex/qr2-bank-drafts`, исходный HEAD `280d169c051f27ef87d204d04d58d7618f7c54ab`.
PlaySpace: `codex/demo1`, исходный HEAD `b81b952efbd54b004d52351da1ae6cd84f38606a`.
Оба дерева до задачи были чистыми. Исходный отдельный проект не изменялся.

Артефакты и проверенные резервные копии находятся вне Git: `R:\TradeSpace-Archive\webhook-qr-fix-20261001-162159`. PostgreSQL dump восстановлен в отдельную временную БД и проверен по записям/хешам; SQLite backup проверен через integrity/FK checks, сохранены vault/config и Redis. Никакого повторного seed/reset пользовательского контура. Секреты и полные банковские QR в отчёт не включены.

Пользовательский контур: TradeSpace `127.0.0.1:8011`, PlaySpace `127.0.0.1:8088`, PostgreSQL 55441, Redis 56391. Синтетические сценарии использовали отдельные БД/Redis/SQLite и порты 8012/8090/8091, 55442/56392; recovery — 55432/56379. Банковских переводов не было.

## Причина задержки и восстановление

У правильного пользовательского мерчанта `webhook_url` был NULL. Три существующих события `deposit.paid` уже находились в durable outbox: `configuration_required`, `missing_webhook_url`, ноль попыток. Это не ошибка HMAC: ключи и версии заказа согласованы, попытки ещё не выполнялись. PlaySpace получил paid через API polling, но корректно не создал credit без подписанного webhook.

Через существующую Superadmin форму с CSRF/2FA сохранён локальный receiver `http://127.0.0.1:8088/webhooks/tradespace`. Из настроек мерчанта изменились только URL и updated_at. Через штатный `/api/v1/admin/webhooks/{event}/retry` повторены исходные события, оплата повторно не подтверждалась.

| Исходное пополнение | Сумма RUB | Результат |
|---|---:|---|
| СБП | 2000.00 | HTTP 204, один credit |
| C2C | 2002.00 | HTTP 204, один credit |
| Мобильная коммерция | 1001.00 | HTTP 204, один credit |

Игроку исходных трёх заказов зачислено **5003.00 RUB gross**. Комиссия мерчанта повторно не вычиталась. Хеши таблиц deposits, balances, ledger_entries, trader_ledger_entries, platform_ledger_entries и operation_fee_snapshots до/после повторов совпали. Доказательство: `existing-recovery-result.json`.

Во время задачи пользователь создал ещё один СБП-заказ 1001.00 RUB до исправления URL. После отдельного явного разрешения владельца его existing paid webhook также доставлен штатным retry: HTTP 204, один credit, финансовые хеши TradeSpace не изменились. Всего по четырём пользовательским пополнениям зачислено **6004.00 RUB**; это сумма credit по сессиям, не утверждение о балансе каждого игрока. Доказательство: `additional-recovery-result.json`. Пользовательский неуспешный QR-запрос без доступного реквизита сохранён без изменений.

## Изменения доставки и интерфейса

- PlaySpace launcher умеет запускать настоящий Celery worker и beat в том же изолированном пользовательском окружении. Worker получает событие сразу после commit, а не ждёт двухсекундного диагностического deliver-loop. Штатные outbox, HMAC, retry/backoff, HTTP sender и финансовые сервисы TradeSpace не переписаны.
- PlaySpace публикует сигнал обновления только после commit существующей атомарной обработки webhook. SSE подписка привязана к действующей серверной сессии, не передаёт IDs, суммы или секреты. История, баланс и открытая оплата читаются заново автоматически. Пятисекундное чтение остаётся резервным; polling сам не зачисляет деньги.
- Исправлена гонка первой загрузки: JS/CSS/HTML не создают анонимные сессии, owner initialization ждёт начальный `/api/session`. Иначе параллельные ресурсы могли перезаписывать player cookie и приводить к SQLite busy/отказу загрузки JS.
- Браузер не разделяет cookie по портам. Браузерный QA переведён на `localhost:8090`, пользовательский `127.0.0.1:8088` сохранён. Оригинальные игроки и их операции не удалялись. Неподтверждённо принадлежащие тесту пустые анонимные сессии в пользовательском SQLite не удаляются наугад.
- Добавлен receiver-only ingress: публично доступны только health и POST webhook. Тело/подпись сохраняются byte-for-byte, owner/player paths дают 404; при падении PlaySpace — 503, successful response только от реального receiver.

## HTTPS

Проверена настоящая доставка через временный TLS-туннель localtunnel: отдельный синтетический Merchant, реальный API create, trader confirm, TradeSpace outbox sender, HMAC receiver, HTTP 204 и ровно один credit. TLS verification не отключалась, SSRF и подписи не обходились. Это actual HTTP E2E, не эмуляция sender. `public-synthetic-result.json` содержит результат.

Пользовательский receiver reachability проверен по `https://bright-trees-switch.loca.lt`: health 200, private routes 404, unsigned webhook 401. Реальные пользовательские events через третью сторону не передавались; их восстановление выполнено локально. Туннель временный, URL зависит от живого процесса; не является стабильным production endpoint. Cloudflare Quick Tunnel в текущей сети не установился (DNS/TLS), без отключения TLS использован localtunnel только для согласованной проверки receiver. Для постоянного внешнего контура нужен стабильный согласованный HTTPS-домен.

## QR: диагностика, границы и сохранённые правила

Исходный PNG 300×300 успешно декодирован: один читаемый QR, короткая HTTPS-ссылка `qr.nspk.ru` без query/type. Прежний отказ происходил из-за обязательного `type=01`, а не качества картинки. Префикс идентификатора, отсутствие суммы и выбор режима не доказывают тип.

По [официальной документации НСПК](https://sbp.nspk.ru/api/new/) type 01/02/03 различаются; короткая ссылка требует дополнительных метаданных, для которых предусмотрен банковский доступ к qrc-data API. Доступов нет, поэтому запросы к банкам и произвольные HTTP-загрузки uploaded URL не выполняются.

- Отдельные ошибки: нечитаемое изображение, неподдерживаемый формат, explicit dynamic, static fixed amount, неизвестный тип, некорректные параметры.
- Explicit static/open-amount проходит прежнюю строгую URL/parameter validation и выдаётся клиенту. `crc` проверяется как поле формата, не заявляется новая банковская проверка контрольной суммы/принадлежности.
- Короткую ссылку можно сохранить только как review/disabled draft. Редактирование без нового изображения сохраняет запрет. Activation запрещена. Дополнительная проверка перед routing исключает unknown/malformed QR даже если другой management path выставит enabled.
- Offline preview защищён ролью/2FA/CSRF, не сохраняет реквизит, не возвращает полный QR URL и не меняет финансы.
- «Динамический QR через банк» виден как недоступный с объяснением; подмена поля POST не активирует режим. Банковских адаптеров, фиктивных проверок и генерации bank dynamic QR нет.
- Метод остаётся sbp_qr. Snapshot, FeeSnapshot, ручное подтверждение, lock/unique QR+сумма+валюта между Merchant/Aggregator, порядок споров, отсутствие trader decline и финансовая математика сохранены.

Изображение владельца проверено в браузере на desktop/mobile, но не сохранено как активный/тестовый реквизит. Синтетический static QR проверен фактической выдачей и оплатой между приложениями.

## Проверки и точные результаты

| Проверка | Результат |
|---|---|
| Full TradeSpace pytest | 475 collected: сначала 468 PASS / 7 FAIL из-за guard порта fixture 55442 вместо 55432; те же 7 без изменения assertions повторены на требуемом изолированном 55432: 7 PASS. Не выдаётся за единый зелёный запуск 475 |
| Новые QR classification/ownership/CSRF/routing cases | 8 PASS |
| Golden | 23 исполняемых oracle PASS; 16/16 snapshots MATCH, expected money/statuses не менялись |
| Native Freeze | 97 protected files / 18 areas PASS без override |
| Deterministic preflight | 19/19 PASS; отдельный live Rapira preflight также 19/19 PASS |
| HTTP smoke | 4/4 PASS |
| Recovery/business | 14/14 PASS, включая настоящий worker/beat и HTTP 500 → scheduled retry → 204 |
| Recovery FX | Только в изолированном settlement сценарии quote HTTP fixture; не реальный банковский/блокчейн перевод |
| PlaySpace unit/HTTP tests | 28 PASS; upstream fixture в unit tests явно обозначен |
| Четыре метода real HTTP | SBP 1200.01, C2C 1300.02, mobile 1400.03, QR 1500.04: 5400.10 RUB gross, по одному credit, корректные merchant net/trader balance/hold |
| Дубликаты/security receiver | Exact repeat и 4 конкурентных semantic events/метод: 204, credit один; invalid HMAC: 401; mismatch amount: 409, без новых credits |
| Реальная остановка PlaySpace | 503 → scheduled retry → 204, один credit; предыдущие четыре метода сохраняются после реального restart |
| Браузер обоих приложений | QR upload/type на desktop/mobile; четыре отдельных синтетических пополнения 1800.07/1900.08/2000.09/2100.10 отображаются успешными; QR и mobile открытая страница обновилась без ручного reload |
| Задержка локального credit | Измерено 0.140–0.172 с от отправки trader confirmation в тестах; не SLA для публичной сети |
| Public HTTPS actual sender | 204, одна проводка receiver, private API 404; только synthetic data |
| Docker | Окончательный локальный Linux image собран, compileall app/scripts внутри контейнера PASS |
| Синтаксис | Python compileall и node --check трёх изменённых JS PASS |

Артефакты: `test-output/full.log`, `seven.log`, `qr-new.log`, `golden-final.log`, `recovery-final.log`, `preflight-deterministic.log`, `preflight-live.log`, `smoke.log`, `playspace-final.log`, `docker-final.log`, `http-four-result.json`, `http-outage-result.json`. Неверная исходная fixture-конфигурация исправлялась только в отдельном test runtime, assertions продукта не ослаблялись.

## Freeze review

Приняты только три защищённых изменения: payment_qr.py (классификация без догадок), requisites.py (не выдавать непроверенный QR до hold), web/routes.py (preview и неактивный draft). Полный diff прочитан; ни один финансовый расчёт, model/migration, API response, webhook payload/header, ledger или appeal state engine не изменён. Coverage guard сохранён: 97 файлов/18 областей, никаких постоянных allow. baseline provenance указывает исходный HEAD этой задачи. Golden snapshots не редактировались. Обновление выполнено штатным record-baseline.

## Изменённые файлы

TradeSpace: `app/services/payment_qr.py`, `app/services/requisites.py`, `app/web/routes.py`, `app/web/ui_messages.py`, `app/presentation/tradespace/trader/view_models.py`, `app/templates/tradespace/trader/_requisite_fields.html`, `app/templates/tradespace/trader/requisites.html`, `app/static/tradespace/js/shell.js`, `tests/test_qr_classification.py`, `tests/golden/freeze_zone_baseline.json`, этот отчёт.

PlaySpace: `server.py`, `owner.py`, `public/app.js`, `public/owner.js`, `scripts/tradespace_demo.py`, `scripts/webhook_ingress.py`, `scripts/integration_regression.py`, `scripts/integration_delivery_recovery.py`, `tests/test_updates.py`, `README.md`, соответствующий отчёт.

## Запуск и ограничения

Порядок: запустить существующие user PostgreSQL/Redis без удаления данных → `scripts/tradespace_demo.py api`, `worker`, `beat` (отдельные процессы) → `server.py` PlaySpace → при необходимости receiver-only ingress и TLS tunnel. Команды и cookie isolation подробно в README PlaySpace. Не запускать prepare/seed над текущими пользовательскими БД.

Банковские подключения остаются черновиками; неизвестный тип оригинального короткого QR без метаданных банка не снимается. Динамический QR и настоящие переводы не реализованы/не тестировались. «Моментально» означает немедленную постановку в очередь и push UI после commit; при недоступной сети работают предусмотренные retry, мгновенная внешняя доставка не гарантируется.

Снимки desktop/mobile находятся вне Git в локальной visualizations директории и приватном архиве проверки. Данные и секреты тестового runtime не включаются в локальные коммиты. Финальные SHA, гигиена staged tree и очистка одноразового стенда указаны в итоговой передаче результата.

## Итоговая гигиена и очистка

После перезапуска исправленных приложений health API/PlaySpace/receiver = 200. Повторная read-only сверка сохранила все 3 первоначальных пользователя TradeSpace и 1 мерчанта; каждый из четырёх разрешённых paid-заказов имеет один credit, общий баланс исходного игрока **6004.00 RUB**. Неподтверждённые/ошибочные пользовательские заказы не удалялись.

Удалены только четыре помеченных `tradespace.webhook-qr-fix=true` одноразовых QA/recovery контейнера с их томами, отдельные fixture SQLite/vault и процессы, включая синтетический публичный tunnel. Вместе с ними удалены все созданные тестовые Merchant/Trader и их QA операции. Пользовательские контейнеры/томы/аккаунты и проверенные backup сохранены. `cleanup-result.json` и `final-user-state.json` фиксируют результат.

Git staged diff --check PASS в обоих проектах. Release artifact policy PASS. Проверка полного staged tree pinned Gitleaks: TradeSpace 348 файлов и PlaySpace 41 файл, секретов не обнаружено. Штатная конфигурация исключает только ранее объявленный публичный CI encryption fixture; исключения не добавлялись. Первый запуск сканера с абсолютным корнем не применил существующие relative-path правила, повтор с правильным working directory завершился без findings. Защищённые staged bytes сверены с working tree, native Freeze PASS97/18 без override.

Локальный образ `tradespace-webhook-qr-fix:20261001` соответствует окончательному исполняемому коду: manifest list SHA256 `ffc7e7074c0ad0bd5568fbd0db2c7828adbaf666ace4c46f8dee2609cb78c71e`. Внутри выполнена Linux Python compilation. Это локальная build-проверка, не deploy.

Финальная проверка туннеля: прежний bright-trees-switch адрес завершил работу (503); receiver-only tunnel восстановлен, текущий временный адрес **https://rare-pets-notice.loca.lt/webhooks/tradespace**. Фактически проверены health200 / private404 / unsigned401, без пользовательских payload. Старый URL не считать действующим; конфигурация пользовательского Merchant остаётся прямой локальной.
