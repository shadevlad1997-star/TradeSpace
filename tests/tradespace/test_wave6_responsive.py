"""Wave 6 contracts for role navigation and mobile financial history visibility."""
import asyncio
import re

from app.presentation.tradespace.navigation import (
    MOBILE_DESTINATIONS, mobile_navigation_for_role, mobile_overflow_for_role,
    navigation_for_role,
)
from tests.tradespace.test_wave4_teamlead import login
from tests.tradespace.test_wave5_staff import fixture, client


def test_mobile_destinations_are_role_scoped_and_complete():
    for role, expected in MOBILE_DESTINATIONS.items():
        main = mobile_navigation_for_role(role)
        overflow = mobile_overflow_for_role(role)
        assert len(main) == 4
        assert tuple(item.slug for item in main) == expected
        assert len({item.slug for item in (*main, *overflow)}) == len(navigation_for_role(role))
        assert set(main + overflow) == set(navigation_for_role(role))
    assert "disputes" in MOBILE_DESTINATIONS["trader"]
    assert "integration" in MOBILE_DESTINATIONS["merchant"]
    assert "settlements" in MOBILE_DESTINATIONS["teamlead"]


def test_merchant_payout_and_settlement_history_has_mobile_equivalent():
    async def scenario():
        async with fixture() as (data, _), client() as browser:
            await login(browser, data["merchantowner"], realm="merchant")
            operations = await browser.get("/merchant/cabinet/tradespace/operations")
            assert operations.status_code == 200
            payout = data["payout"]
            payout_cards = re.findall(
                r'<article class="ts-mobile-operation" aria-label="Выплата.*?</article>',
                operations.text, flags=re.DOTALL,
            )
            assert len(payout_cards) == 1
            assert payout.external_id[:8] in payout_cards[0]
            assert str(payout.amount) in payout_cards[0]
            assert "ts-data-table-wrap--has-cards" in operations.text

            settlements = await browser.get("/merchant/cabinet/tradespace/settlements")
            assert settlements.status_code == 200
            cards = re.findall(
                r'<article class="ts-mobile-operation" aria-label="Расчёт.*?</article>',
                settlements.text, flags=re.DOTALL,
            )
            assert len(cards) == 1
            assert str(data["settlement"].total_debit_rub) in cards[0]
            assert str(data["settlement"].amount_usdt) in cards[0]
            assert str(data["settlement"].fee_usdt) in cards[0]
            assert "ts-data-table-wrap--has-cards" in settlements.text
    asyncio.run(scenario())


def test_account_menus_hide_legacy_entry_for_every_user_realm_with_safe_entry_alias():
    async def scenario():
        async with fixture() as (data, _):
            for key, realm in (
                ("trader", "trader"),
                ("merchantowner", "merchant"),
                ("lead", "staff"),
                ("support", "staff"),
                ("admin", "staff"),
                ("super", "staff"),
            ):
                async with client() as browser:
                    user = data[key]
                    await login(browser, user, realm=realm)
                    cabinet = await browser.get(f"/{realm}/cabinet")
                    assert cabinet.status_code == 200
                    desktop = re.search(
                        r'<details class="ts-account".*?</details>',
                        cabinet.text, flags=re.DOTALL,
                    )
                    mobile = re.search(
                        r'<div class="ts-mobile-sheet".*?</section>',
                        cabinet.text, flags=re.DOTALL,
                    )
                    assert desktop and mobile
                    for menu in (desktop.group(), mobile.group()):
                        assert "Предыдущий кабинет" not in menu
                        assert f'href="/{realm}/cabinet/legacy"' not in menu

                    fallback = await browser.get(
                        f"/{realm}/cabinet/legacy", follow_redirects=False,
                    )
                    assert fallback.status_code == 303
                    assert fallback.headers["location"] == f"/{realm}/cabinet"

    asyncio.run(scenario())
