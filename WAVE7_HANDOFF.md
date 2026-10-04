# TradeSpace — Wave 7 handoff

Дата проверки: 2026-09-23. Рабочая папка: `R:\TradeSpace`.
Ветка: `codex/tradespace-wave1`. HEAD: `8b88337e55eb5d8b6d3a3d2ac78e7ed8943d0279`.
Commit, push, deploy не выполнялись. Wave 8 не начата.

## Реализовано

- TradeSpace identity в текущем коде, конфигурации, assets, документации, тестах и локальных DB names.
- `/cabinet`, realm entrypoints, role URLs и `/cabinet/legacy` больше не показывают прежние кабинеты. Ссылка на них отсутствует в desktop/mobile AccountMenu.
- `TRADESPACE_UI_ENABLED` включён по умолчанию. Значения `false`, `0`, `no`, `off` дают авторизованному пользователю нейтральный экран TradeSpace с `no-store`. Флаг не является механизмом отката приложения.
- Realms и cookie names сохранены; TeamLead остаётся в staff realm. Preview доступен по существующей подписанной привязке к сессии.
- Исходящие webhook headers: `X-TradeSpace-Event-ID`, `X-TradeSpace-Event-Type`, `X-TradeSpace-Timestamp`, `X-TradeSpace-Key-ID`, `X-TradeSpace-Signature-Version`, `X-TradeSpace-Signature`. Канонический body и HMAC input не менялись.
- Разрешённый пользователем перенос существующего wallet reader/QR/history с прежними role и 2FA guards; Trader — Финансы, Admin/Superadmin — Финансы / кошелёк.
- Разрешённый пользователем перенос формы регистрации уже отправленного Rolling в карточку мерчанта для Superadmin, включая мерчанта без Balance row. Команда и денежные проверки прежние.
- AI settings показывают только boolean-состояние настроенности секретов. Значения секретов не возвращаются в страницу.
- Одноразовый просмотр секрета агрегатора использует существующий consume-once storage, masking и no-store. Старый secret callback не возвращает старый кабинет.

## Проверки текущего кода

| Gate | Результат |
|---|---|
| Полная PostgreSQL suite | **234 passed**, 2 deprecation warnings |
| Freeze | **81 файла / 18 зон, PASS** |
| Golden | **16/16**, два последовательных verify; по 17 tests PASS |
| Deterministic preflight | **19/19 PASS** |
| HTTP smoke | **4/4 PASS** |
| Recovery с локальным test rate provider | **14/14 PASS**, включая `500 → retry → 204` |
| External preflight | **18/19**, внешний курс временно unavailable |
| Python compilation, git diff --check | PASS |
| Исходный workspace | 225 файлов совпадают с сохранёнными SHA-256 |

Golden сохраняет прежние ограничения: 9 AUTOMATED, 5 PARTIALLY AUTOMATED, 2 MANUAL. Совпадение 16 снимков не означает полную автоматизацию всех 16 сценариев. Пересмотрены только evidence hashes; `business_result`, money values и automation gaps неизменны.

Freeze: из исходных 78 защищённых файлов изменены только preview salt, шесть webhook header names и два browser GET entrypoints. Дополнительно защищены `app/web/__init__.py`, `app/presentation/tradespace/routes.py`, `app/presentation/tradespace/wallet.py`. Финансовые функции и 26 Python-файлов Alembic не менялись. Не обновлять baseline автоматически при следующих изменениях.

## Локальная среда и данные

- API: `http://127.0.0.1:8000`; существующие realm login paths сохранены.
- PostgreSQL: `127.0.0.1:55432`, `tradespace_dev`, `tradespace_test`, роль `tradespace_local`.
- Runtime data: `R:\TradeSpaceData\postgres`, `R:\TradeSpaceData\redis`; настройки содержат `TRADESPACE_DATA_DIR`.
- Head: `0025_teamlead_merchant_referrals`. Новых migrations/schema changes нет.
- При переименовании БД сравнивались все строки/хеши таблиц. Из данных изменены только 22 email синтетических local-аккаунтов; остальные поля пользователей и финансовые таблицы совпали. Последующие recovery runs добавляли только свои синтетические записи в dev.
- Проверка 252 текстовых/JSON/array columns каждой БД не обнаружила прежнего имени.
- Encrypted backups и подробные доказательства: `R:\TradeSpace-Archive\wave7-20260923`. Секреты в этом документе не публикуются.
- Обычный restore старых test fixtures выявил ранее существовавшие dangling antiscam FK. Точная копия проверена отдельным staged restore с временным отключением triggers только в disposable restore DB. Исходные записи не исправлялись и не удалялись. Это ограничение нельзя считать проверкой обычного production restore.

## Незавершённая приёмка

Визуально проверены Trader, Merchant, TeamLead, Support, Admin, Superadmin и Aggregator: desktop 1440×960, mobile 390×844. Сохранены 14 снимков. Все 8 backend roles и оба значения flag проверены автоматическими маршрутными тестами.

Дополнительный визуальный проход `operator`, disabled-flag экрана и окончательный просмотр перенесённых wallet/Rolling/AI элементов не завершены: Computer Use остановил браузерную работу, поскольку не смог надёжно определить текущий URL Windows. Не выдавать автоматические HTML-тесты за визуальную проверку. Временный rollback-only QA сервер остановлен.

Следующий шаг: завершить только указанный визуальный QA, затем подтвердить приёмку Wave 7. При восстановлении внешнего rate provider повторить network-dependent preflight; локальные результаты уже зелёные. Новую Wave без отдельной команды не начинать.

Подробный внешний отчёт: `R:\TradeSpace-Archive\wave7-20260923\WAVE7_IDENTITY_REPORT.md`.

**READY FOR WAVE 8: NO** — осталась описанная визуальная приёмка.


## Уточнение при проверке Wave 8 — 23 сентября 2026

Исходные результаты выше относятся к завершению реализации Wave 7. Проверка brand independence была незакрыта также в части favicon: явный icon link отсутствовал, фактический браузерный cache/tab результат не был проверен. Wave 8 добавила versioned icon во все текущие layouts/login/errors и проверила HTTP bytes/MIME. Полная визуальная приёмка остаётся заблокированной инструментом определения URL. External rate preflight теперь 19/19. Текущее состояние и новые результаты — [WAVE8_HANDOFF.md](WAVE8_HANDOFF.md) и [WAVE8_REPORT.md](WAVE8_REPORT.md).
