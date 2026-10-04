# TradeSpace — Wave 8 handoff

23 сентября 2026. `R:\TradeSpace`, `codex/tradespace-wave1`, HEAD `8b88337e55eb5d8b6d3a3d2ac78e7ed8943d0279`.

Исправления favicon, статусов/причин, общего меню «Ещё», кнопок и пользовательской копии реализованы. Полный отчёт: [WAVE8_REPORT.md](WAVE8_REPORT.md). Исходный workspace не менялся. Commit/push/deploy не было.

## Проверенные gates

- Full PostgreSQL **247 passed**, 2 прежних warnings; presentation **40 passed**; JS menu tests **4/4**.
- Freeze **81/18 PASS**, без override и изменений protected files.
- Golden **16/16 дважды**, 17 executable oracles каждый раз.
- Preflight deterministic **19/19**, external **19/19**; HTTP smoke **4/4**.
- Recovery **14/14 дважды**: внешний и локальный курс, отдельные synthetic runs; оба включают реальный Webhook **500 → retry → 204**.
- Compilation и diff --check PASS.
- Финансовые формулы/статусы/БД/migrations/auth/HMAC/headers/permissions без изменений.
- Golden business_result/gaps без изменений. Обновлены ТОЛЬКО provenance hashes GB-12/13/16 и index после перевода одной UI assertion в settlement test. Подробное доказательство — внешний `golden-provenance-review.json`. Не запускать `--record` автоматически.

## Что реально не завершено

1. Computer Use остановил браузерное управление из-за недостоверного определения URL. Нового visual QA нет. Нужны Trader/Merchant/TeamLead/Support/Admin/Superadmin на desktop/tablet/mobile, меню/клавиатура/touch/overflow/длинные строки, вкладка favicon после cache clear и screenshots. Старые снимки не подтверждают Wave 8. Остаток Wave 7 operator/disabled flag/wallet-Rolling-AI visual также не закрыт.
2. В локальной dev DB ранее созданная `VISUAL-W2-PENDING` требует освобождения 11 941,20 RUB при stored hold 0,00. Worker сохраняет финансовый guard и не завершает её. Код и запись не исправлялись. Не считать dev DB готовыми данными для server move; требуется отдельное решение о тестовых данных.
3. Golden gaps сохранены: 9 automated, 5 partial, 2 manual. Ручные specialized appeal и частичные payout/expiration/decline/no-requisite/browser-rejection сценарии не стали автоматически доказанными.

## Среда и доказательства

Локальный API снова запущен на `http://127.0.0.1:8000`, через существующий runtime launcher; данные доступа не менялись. Временный deterministic API 8003 остановлен. Browser QA server не создавался.

`R:\TradeSpace-Archive\wave8-20260923` содержит before hashes/files, роль/financial test логи, recovery JSON, provenance review, actual HTTP favicon evidence, identity scan, task-only diff, status до/после. В `WAVE8_REPORT.md` — полный список собственных изменений отдельно от pre-existing worktree.

Следующее разрешённое действие — закончить перечисленную приёмку и обновить результат. Серверный этап автоматически не начинать, core не исправлять ради UI.

READY FOR WAVE 9 / SERVER MOVE: NO
Причина: отсутствует обязательное браузерное visual acceptance; не принят перенос локальных тестовых данных с известной несогласованной visual fixture.
