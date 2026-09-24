from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy.dialects import postgresql

from app.web.ui_messages import cabinet_redirect_url, resolve_ui_message
from app.web.ui_queries import (
    DEPOSIT_ACTIVE_STATUSES,
    DEPOSIT_TERMINAL_STATUSES,
    apply_trader_deposit_filters,
    normalized_deposit_filters,
    trader_deposit_scope,
)
from app.web.view_models import (
    build_attempts_by_event_id,
    build_audit_view,
    build_webhook_event_view,
    traffic_status_class,
    traffic_status_label,
)


ROOT = Path(__file__).resolve().parents[1]
CABINET = (ROOT / "app/templates/cabinet.html").read_text(encoding="utf-8")
DEPOSIT_ROWS = (ROOT / "app/templates/_deposit_rows.html").read_text(encoding="utf-8")
ROUTES = (ROOT / "app/web/routes.py").read_text(encoding="utf-8")


def test_success_redirect_has_no_flash_or_stale_message():
    assert cabinet_redirect_url("/cabinet", message="Реквизит создан", section="requisites") == (
        "/cabinet?section=requisites"
    )
    assert "msg=" not in cabinet_redirect_url(
        "/cabinet", message="Реквизит создан", section="requisites"
    )
    assert "class=\"msg\"" not in CABINET


def test_ui_error_kind_is_allowlisted_and_html_is_not_accepted_from_query():
    message = resolve_ui_message(
        {
            "ui_error": "twofa_required",
            "kind": "success",
            "message": "<img src=x onerror=alert(1)>",
            "section": "security",
        }
    )
    assert message is not None
    assert message.kind == "security"
    assert "<img" not in message.message
    assert resolve_ui_message({"ui_error": "<script>alert(1)</script>"}) is None


def test_action_error_uses_context_section_without_accepting_arbitrary_section():
    message = resolve_ui_message({"ui_error": "action_failed", "section": "deposits"})
    assert message is not None and message.section == "deposits"
    invalid = resolve_ui_message({"ui_error": "action_failed", "section": "<script>"})
    assert invalid is not None and invalid.section == ""


def test_webhook_attempt_view_redacts_and_truncates_sensitive_response_data():
    event_id = "event-a"
    rows = [
        SimpleNamespace(
            webhook_event_id=event_id,
            attempt_no=1,
            status="failed",
            status_code=401,
            error="Authorization: bearer-secret",
            response_snippet='{"token":"hidden","ok":false}',
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
    ]
    attempts = build_attempts_by_event_id(rows)[event_id]
    assert len(attempts) == 1
    assert "bearer-secret" not in attempts[0].error
    assert "hidden" not in attempts[0].response_snippet
    assert "[redacted]" in attempts[0].response_snippet


def test_webhook_payload_is_redacted_and_only_rendered_in_details_contract():
    event = SimpleNamespace(
        id="event-a",
        event_type="deposit.paid",
        payload={
            "operation_type": "deposit",
            "external_id": "merchant-order-1",
            "amount": "1000.00",
            "currency": "RUB",
            "status": "paid",
            "api_key": "must-not-render",
        },
        status="delivered",
        attempts=1,
        last_error=None,
        last_status_code=200,
        created_at=datetime.now(timezone.utc),
        correlation_id="corr-a",
    )
    view = build_webhook_event_view(event, [])
    assert "must-not-render" not in view["payload_json"]
    webhook_block = CABINET[CABINET.rindex("{% elif s == 'Webhook' %}") : CABINET.index("{% elif s == 'Апелляции' %}")]
    main_table = webhook_block[: webhook_block.index('<div id="webhookDetail')]
    assert "payload_json" not in main_table
    assert "payload_json" in webhook_block
    assert "attempt.response_snippet" in webhook_block


def test_webhook_attempts_are_loaded_by_one_batch_select_not_n_plus_one():
    assert ROUTES.count("select(WebhookDeliveryAttempt)") == 1
    batch = ROUTES.index("select(WebhookDeliveryAttempt)")
    assert ".in_(webhook_event_ids)" in ROUTES[batch : batch + 500]


def test_audit_mapping_has_safe_fallback_and_optional_request_id():
    known = SimpleNamespace(
        id="a1",
        created_at=datetime.now(timezone.utc),
        action="requisite_toggled",
        target_type="requisite",
        target_id="12345678-1234-1234-1234-123456789012",
        ip="127.0.0.1",
        details={"enabled": True, "request_id": "req-1"},
    )
    unknown = SimpleNamespace(**{**known.__dict__, "id": "a2", "action": "internal_future_event", "details": {}})
    known_view = build_audit_view(known, "Owner")
    unknown_view = build_audit_view(unknown, "Owner")
    assert known_view["event_label"] == "Реквизит включён"
    assert known_view["request_id"] == "req-1"
    assert unknown_view["event_label"] == "Системное событие"
    assert unknown_view["request_id"] == ""
    assert unknown_view["raw_action"] == "internal_future_event"


def test_audit_main_table_has_no_raw_json_or_result_column():
    block = CABINET[CABINET.rindex("{% elif s == 'Audit log' %}") : CABINET.rindex("{% elif 'Безопас' in s %}")]
    main_table = block[: block.index('<div id="auditDetail')]
    assert "details_json" not in main_table
    assert "<th>Результат</th>" not in main_table
    assert "details_json" in block
    assert "request_id" in block


def test_deposit_filter_values_are_allowlisted():
    values = normalized_deposit_filters(
        {
            "view": "history",
            "bank_code": "bank-a",
            "payment_method": "sbp",
            "status": "paid",
            "deposit_date": "2026-08-04",
        },
        valid_bank_codes={"bank-a"},
    )
    assert values["view"] == "history"
    assert values["status"] == "paid"
    rejected = normalized_deposit_filters(
        {"view": "invalid", "bank_code": "x' OR 1=1 --", "status": "paid"},
        valid_bank_codes={"bank-a"},
    )
    assert rejected["view"] == "active"
    assert rejected["bank_code"] == ""
    assert rejected["status"] == ""


def test_deposit_status_sets_are_disjoint_and_filters_precede_limit():
    assert not (DEPOSIT_ACTIVE_STATUSES & DEPOSIT_TERMINAL_STATUSES)
    statement = apply_trader_deposit_filters(
        trader_deposit_scope(["00000000-0000-0000-0000-000000000001"]),
        {
            "view": "history",
            "query": "order",
            "amount": "1000.00",
            "requisite": "",
            "date": "",
            "bank_code": "bank-a",
            "payment_method": "sbp",
            "status": "paid",
        },
        matching_requisite_ids=[],
        requisite_filter_ids=[],
        bank_requisite_ids=["00000000-0000-0000-0000-000000000001"],
    ).limit(500)
    sql = str(statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    assert sql.index("WHERE") < sql.index("LIMIT")
    assert "deposits.status = 'paid'" in sql
    assert "deposits.method = 'sbp'" in sql


def test_risk_status_mapping_uses_neutral_fallback_and_safe_colors():
    assert traffic_status_label("active") == "Активен"
    assert traffic_status_class("active") == "status-ok"
    assert traffic_status_class("auto_paused") == "status-bad"
    assert traffic_status_label("future_state") == "Неизвестное состояние"
    assert traffic_status_class("future_state") == "status-neutral"


def test_existing_post_routes_and_copy_full_values_remain_in_templates():
    assert 'action="{{ cabinet_base }}/deposits/{{ d.id }}/confirm"' in CABINET
    assert 'action="{{ cabinet_base }}/requisites/{{ r.id }}/delete"' in CABINET
    assert 'action="{{ cabinet_base }}/antiscam/requisites/{{ r.id }}/pause"' in CABINET
    assert 'data-copy-value="{{ requisite_values.get(rid, \'\') }}"' in CABINET
    assert "bindCopyButtons(body)" in CABINET


def test_trader_polling_is_bound_to_server_rendered_context():
    assert 'data-partial-kind="trader-deposits"' in CABINET
    assert 'data-view="active"' in CABINET
    assert 'data-view="history"' in CABINET
    assert 'data-refresh-url="{{ deposit_refresh_url }}"' in CABINET
    assert "{% set deposit_auto_poll = role == user.role or trader_preview_active %}" in CABINET
    assert "target.dataset.autoPoll!=='true'" in CABINET
    assert "new URL(target.dataset.refreshUrl,window.location.origin)" in CABINET
    assert "preview_role" not in CABINET
    assert "client_role" not in CABINET


def test_role_preview_uses_server_confirmed_subject_for_polling():
    assert 'data-preview-trader="{{ t.id }}"' in CABINET
    assert "trader_preview_urls.get(t.id|string)" in CABINET
    assert "preview_context=" in ROUTES
    assert "verify_preview_context_token(" in ROUTES
    assert "PREVIEW_SUBJECT_SESSION_KEY" in ROUTES
    assert "subject_trader_id=trader_subject.id" in ROUTES
    assert "request.query_params.get('subject_trader_id'" not in ROUTES
    assert "request.url.include_query_params(view='active', status='').query" in CABINET
    assert "request.url.include_query_params(view='history', status='').query" in CABINET
    assert 'action="{{ deposit_page_url }}"' in CABINET
    assert 'href="{{ deposit_page_url }}?view={{ deposit_view }}"' in CABINET


def test_trader_rows_gate_process_action_by_scoped_server_decision():
    assert "dd.get('can_process')" in CABINET
    assert "dd.get('can_process')" in DEPOSIT_ROWS
    assert "deposit.status not in DEPOSIT_ACTIVE_STATUSES" in ROUTES
    assert "str(requisite.trader_id) != str(subject.id)" in ROUTES
    assert "'can_process': _can_render_trader_deposit_process(" in ROUTES


def test_poll_response_context_and_stale_sequence_are_checked_before_dom_update():
    assert "new AbortController()" in CABINET
    assert "const requestSequence=++depositPollSequence" in CABINET
    assert "marker[1]!==expectedKind||marker[2]!==expectedView" in CABINET
    assert "requestSequence!==depositPollSequence" in CABINET
    assert "depositRowsTarget()!==target" in CABINET
    assert "target.dataset.view!==expectedView" in CABINET
    assert CABINET.index("requestSequence!==depositPollSequence") < CABINET.index("target.innerHTML=html")
    assert "error.name==='AbortError'" in CABINET


def test_deposit_partial_declares_kind_and_view_and_pagination_is_refreshed():
    assert "tradespace-partial:" in DEPOSIT_ROWS
    assert "trader-deposits" in DEPOSIT_ROWS
    assert "generic-deposits" in DEPOSIT_ROWS
    assert "refreshTablePagination(target.closest('table'))" in CABINET
    assert "const currentPage=Number(table.dataset.page||'1')" in CABINET
    assert "table.dataset.page=String(page)" in CABINET
