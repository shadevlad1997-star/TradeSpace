"""Keep the four TradeSpace login pages on the same minimal, accessible form."""

import asyncio
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import httpx

from app.main import app


LOGO_URL = "/static/tradespace/login-logo.svg?v=1"
REMOVED_COPY = (
    "Используйте выданные данные.",
    "Оставьте пустым, если 2FA не подключена.",
    "Доступ выдаёт администратор платформы.",
)


def test_login_logo_and_form_contract_for_all_realms():
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000"
        ) as browser:
            for realm in ("trader", "merchant", "staff", "aggregator"):
                page = await browser.get(f"/{realm}/login")
                assert page.status_code == 200
                markup = page.text
                assert '<h2 class="ts-auth-card__logo"><img' in markup
                assert f'src="{LOGO_URL}" alt="TradeSpace"' in markup
                assert "login-logo.png" not in markup
                assert 'Вход в TradeSpace</h2>' not in markup
                assert all(copy not in markup for copy in REMOVED_COPY)
                assert f'<form method="post" action="/{realm}/login"' in markup
                assert re.search(r'name="csrf_token"\s+value="[^"]+"', markup)
                for name in ("email", "password", "otp"):
                    assert f'name="{name}"' in markup
                assert "Войти в кабинет" in markup
            logo = await browser.get(LOGO_URL)
            assert logo.status_code == 200
            assert logo.headers["content-type"].startswith("image/svg+xml")
            assert logo.content == Path("app/static/tradespace/login-logo.svg").read_bytes()
            vector = ET.fromstring(logo.content)
            tags = [node.tag.rsplit("}", 1)[-1] for node in vector.iter()]
            assert tags.count("path") >= 2
            assert not set(tags) & {"image", "text", "foreignObject"}
            assert b"data:image" not in logo.content
            assert b"font-family" not in logo.content

    asyncio.run(scenario())
