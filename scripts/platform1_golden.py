"""Record or verify deterministic Platform #1 business golden snapshots.

The harness intentionally delegates behavior checks to the recovered project's
existing PostgreSQL and live-recovery tests. Snapshots contain only normalized
business contracts and immutable evidence hashes; they never duplicate the
production implementation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import uuid
from datetime import date, datetime, time
from decimal import Decimal
from difflib import unified_diff
from pathlib import Path
from typing import Any

from scripts.recovery_run import local_environment


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SNAPSHOT_DIR = ROOT / "tests" / "golden" / "snapshots"
DEFAULT_RECOVERY_RESULTS = ROOT / "test-results" / "live-smoke.json"
UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-"
    r"[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$"
)
TIMESTAMP_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?$"
)
RANDOM_SUFFIX_RE = re.compile(r"(?<=[-_:])[0-9a-f]{16,64}\b", re.IGNORECASE)
VOLATILE_KEYS = {
    "id",
    "request_id",
    "correlation_id",
    "nonce",
    "created_at",
    "updated_at",
    "requested_at",
    "completed_at",
    "failed_at",
    "delivered_at",
    "next_attempt_at",
    "lease_expires_at",
}
SECRET_KEY_PARTS = ("secret", "password", "token", "ciphertext", "encrypted")


def _scenario(
    scenario_id: str,
    title: str,
    automation: str,
    *,
    pytest_nodes: tuple[str, ...] = (),
    recovery_checks: tuple[str, ...] = (),
    business_result: dict[str, Any],
    gap: str | None = None,
) -> dict[str, Any]:
    return {
        "scenario_id": scenario_id,
        "title": title,
        "automation": automation,
        "pytest_nodes": list(pytest_nodes),
        "recovery_checks": list(recovery_checks),
        "business_result": business_result,
        "gap": gap,
    }


SCENARIOS = (
    _scenario(
        "GB-01",
        "Merchant HMAC v2 creates a Deposit",
        "AUTOMATED",
        pytest_nodes=(
            "tests/test_merchant_hmac_v2_postgres.py::test_hmac_v2_normative_vector_and_tamper_detection",
            "tests/test_merchant_hmac_v2_postgres.py::test_hmac_v2_replay_idempotency_tamper_and_fail_closed",
        ),
        recovery_checks=("permissions", "deposit_created"),
        business_result={
            "authentication": {"version": "2", "valid_signature": "accepted", "unsigned": "rejected"},
            "operation": {"kind": "deposit", "state": "pending", "assignment_count": 1},
            "fees": {"snapshot_created": True},
            "trader": {"collateral_hold_created": True},
            "events": {"business_terminal_events": 0},
        },
    ),
    _scenario(
        "GB-02",
        "Exact create replay",
        "AUTOMATED",
        pytest_nodes=(
            "tests/test_merchant_hmac_v2_postgres.py::test_hmac_v2_replay_idempotency_tamper_and_fail_closed",
        ),
        recovery_checks=("deposit_created",),
        business_result={
            "exact_request_replay": "hmac_replay_detected",
            "fresh_nonce_same_business_key": {"idempotent": True, "same_operation": True},
            "changed_payload_same_key": "idempotency_conflict",
            "operation_count": 1,
            "collateral_reservation_count": 1,
        },
    ),
    _scenario(
        "GB-03",
        "No matching Requisite",
        "PARTIALLY AUTOMATED",
        pytest_nodes=(
            "tests/test_bank_requisites_postgres.py::test_new_banks_work_in_trader_create_edit_and_merchant_api",
        ),
        recovery_checks=("trader_setup", "deposit_created"),
        business_result={
            "captured": {"eligible_requisite_assignment": True, "disabled_requisite_not_selected": True},
            "not_captured": {"all_candidates_unavailable_error": True, "zero_orphan_hold_assertion": True},
        },
        gap="The recovered suite proves positive routing and enable/disable behavior, but has no isolated all-candidates-unavailable financial delta assertion.",
    ),
    _scenario(
        "GB-04",
        "Trader confirms ordinary Deposit",
        "AUTOMATED",
        pytest_nodes=(
            "tests/test_teamlead_postgres.py::test_postgres_paid_accrues_once_from_gross_without_changing_trader_merchant_or_rolling",
        ),
        recovery_checks=("deposit_paid",),
        business_result={
            "operation": {"state": "paid"},
            "recovery_fixture": {
                "gross_rub": "5000.00",
                "merchant_balance_after_rub": "4250.00",
                "trader_balance_after_rub": "95350.00",
                "trader_hold_after_rub": "0.00",
            },
            "commission": {
                "merchant_fee_rub": "750.00",
                "executor_fee_rub": "350.00",
                "platform_margin_rub": "400.00",
                "immutable_snapshot": True,
            },
            "idempotent_financial_settlement": True,
        },
    ),
    _scenario(
        "GB-05",
        "Dual TeamLead attribution",
        "AUTOMATED",
        pytest_nodes=(
            "tests/test_teamlead_postgres.py::test_postgres_independent_referral_matrix_shared_balance_settlement_and_reversal",
        ),
        business_result={
            "operation": {"state": "paid"},
            "attribution": {"trader_referral": "creation_time_snapshot", "merchant_referral": "creation_time_snapshot"},
            "accrual": {"unique_per_deposit_and_source": True, "debt_applied_before_available": True},
            "platform": {"gross_margin_recorded": True, "referral_expense_recorded": True},
            "reconciliation": {"teamlead_balance": True, "platform_income": True},
        },
    ),
    _scenario(
        "GB-06",
        "Deposit decline and retry",
        "PARTIALLY AUTOMATED",
        pytest_nodes=(
            "tests/test_rolling_postgres.py::test_released_allocation_and_pending_topup_do_not_change_aggregates",
        ),
        recovery_checks=("cancellation",),
        business_result={
            "captured": {"merchant_cancel_terminal": ["cancelled", "failed"], "trader_hold_after_rub": "0.00", "rolling_pending_release": True},
            "not_captured": {"browser_explicit_decline_end_to_end": True, "duplicate_decline_event_cardinality": True},
        },
        gap="Cancellation and Rolling release are automated; the current suite does not run a complete browser decline plus duplicate retry with event cardinality.",
    ),
    _scenario(
        "GB-07",
        "TTL expiration",
        "PARTIALLY AUTOMATED",
        pytest_nodes=(
            "tests/test_rolling_unit.py::test_ttl_uses_immutable_expires_at",
            "tests/test_legacy_pending_repair_postgres.py::test_new_deposit_with_missing_hold_remains_worker_fail_closed",
        ),
        business_result={
            "captured": {"deadline_source": "immutable_expires_at", "missing_hold_worker_behavior": "fail_closed"},
            "expected_current_terminal": {"state": "failed", "failure_reason": "trader_timeout"},
            "not_captured": {"normal_pending_full_accounting_expiration_end_to_end": True},
        },
        gap="TTL predicate and fail-closed worker behavior are automated; a normal held Deposit expiration with full ledger/event snapshot is not isolated by the recovered suite.",
    ),
    _scenario(
        "GB-08",
        "Rolling funding and FIFO recovery",
        "AUTOMATED",
        pytest_nodes=(
            "tests/test_rolling_postgres.py::test_fifo_can_consume_multiple_transfers_and_cross_into_settle",
            "tests/test_rolling_postgres.py::test_account_aggregates_and_reconciliation_match_transfers_and_consumptions",
        ),
        business_result={
            "funding": {"confirmed_transfer_order": "FIFO"},
            "fifo_fixture": {
                "merchant_payable_rub": "35000.00",
                "rolling_recovered_usdt": "300.000000",
                "rolling_recovered_rub": "30000.00",
                "ordinary_settle_credit_rub": "5000.00",
                "consumptions_usdt": ["100.000000", "200.000000"],
            },
            "reconciliation_fixture": {"principal_usdt": "200.000000", "recovered_usdt": "120.000000", "outstanding_usdt": "80.000000", "ok": True},
        },
    ),
    _scenario(
        "GB-09",
        "Appeal accepted to paid",
        "PARTIALLY AUTOMATED",
        pytest_nodes=(
            "tests/test_acceptance_financial_postgres.py::test_acceptance_specialized_appeal_outcome_and_duplicate_resolution[approve]",
            "tests/test_acceptance_known_gaps.py::test_generic_appeal_http_finance_duplicate_and_concurrency[approved]",
            "tests/test_acceptance_known_gaps.py::test_generic_appeal_http_rolls_back_invalid_hold_and_enforces_roles",
        ),
        business_result={
            "captured": {"appeal_status": "approved", "deposit_status": "paid", "trader_hold": "0.00", "trader_balance": "99050.00", "merchant_available": "1900.00", "event_type": "deposit.paid", "payload_status": "paid", "one_financial_outcome": True, "invalid_hold_rolls_back": True},
            "not_captured": ["combined TeamLead and Rolling appeal fixture", "real-browser interaction"],
        },
        gap="Authorized HTTP and specialized service finance are verified; combined referral/Rolling appeal attribution and real-browser interaction are outside this fixture.",
    ),
    _scenario(
        "GB-10",
        "Appeal rejection restores consistent pending event",
        "PARTIALLY AUTOMATED",
        pytest_nodes=(
            "tests/test_acceptance_financial_postgres.py::test_acceptance_specialized_appeal_outcome_and_duplicate_resolution[reject]",
            "tests/test_acceptance_known_gaps.py::test_generic_appeal_http_finance_duplicate_and_concurrency[rejected]",
        ),
        business_result={
            "captured": {"appeal_status": "rejected", "deposit_status": "pending", "trader_hold": "950.00", "trader_balance": "100000.00", "merchant_available": "1000.00", "event_type": "deposit.pending", "payload_status": "pending", "no_duplicate_financial_effect": True},
            "not_captured": ["external merchant consumer compatibility", "real-browser interaction"],
        },
        gap="F07 event/payload mismatch is corrected and verified locally; external consumers and real-browser interaction require separate acceptance.",
    ),
    _scenario(
        "GB-11",
        "Webhook delivery recovery 500 to 204",
        "AUTOMATED",
        pytest_nodes=(
            "tests/test_webhook_hardening_postgres.py::test_delivery_success_uses_dedicated_secret_and_redacted_logs",
            "tests/test_webhook_hardening_postgres.py::test_retry_policy_terminal_dead_letter_and_configuration_required",
            "tests/test_webhook_hardening_postgres.py::test_claim_race_stale_lease_scanner_enqueue_recovery_and_manual_retry",
        ),
        recovery_checks=("webhook",),
        business_result={
            "event": {"same_event_id_across_attempts": True, "source_finance_unchanged": True},
            "attempt_status_codes": [500, 204],
            "final_status": "delivered",
            "signature": {"dedicated_webhook_key": True, "valid": True, "api_key_header_absent": True},
            "retry": {"scheduled_after_500": True, "attempt_count": 2},
        },
    ),
    _scenario(
        "GB-12",
        "Merchant partial settlement complete",
        "AUTOMATED",
        pytest_nodes=(
            "tests/test_settlement_hardening_postgres.py::test_exact_available_idempotency_and_quote_snapshot",
            "tests/test_settlement_hardening_postgres.py::test_completion_rejection_idempotency_and_tx_hash_uniqueness",
        ),
        recovery_checks=("settlement",),
        business_result={
            "quote": {"strict_live_ask": True, "immutable_snapshot": True, "volatile_rate_omitted": True},
            "settlement": {"states": ["pending", "completed"], "partial_request": True, "one_pending_guard": True, "idempotent_completion": True},
            "money": {"available_to_frozen_once": True, "frozen_consumed_once": True, "network_fee_included": True},
            "execution": {"manual_tx_evidence": True, "real_blockchain_transfer": False},
        },
    ),
    _scenario(
        "GB-13",
        "Merchant settlement rejection control",
        "PARTIALLY AUTOMATED",
        pytest_nodes=(
            "tests/test_settlement_hardening_postgres.py::test_completion_rejection_idempotency_and_tx_hash_uniqueness",
            "tests/test_remediation_finance_postgres.py::test_settlement_reject_real_browser_post_csrf_roles_repeat_and_reconcile",
        ),
        business_result={
            "captured": {"service_state": "rejected", "frozen_release_once": True, "repeated_service_rejection_idempotent": True, "authorized_csrf_http_post": True, "wrong_roles_denied": True, "missing_csrf_denied": True, "fixture_available_after": "1000.00", "fixture_frozen_after": "0.00"},
            "not_captured": {"real_browser_rejection_interaction": True},
        },
        gap="F09 HTTP browser-route finance/CSRF/roles are verified; actual browser interaction remains blocked by the browser tool.",
    ),
    _scenario(
        "GB-14",
        "TeamLead settlement lifecycle",
        "AUTOMATED",
        pytest_nodes=(
            "tests/test_teamlead_postgres.py::test_postgres_settlement_fee_freeze_reject_complete_and_168_hour_cooldown",
            "tests/test_teamlead_postgres.py::test_postgres_settlement_complete_uses_requested_rub_and_tx_hash_unique_for_completed",
        ),
        business_result={
            "eligibility": {"positive_available": True, "debt_blocks": True, "one_pending": True},
            "money": {"available_to_frozen": True, "rejection_releases": True, "completion_consumes": True, "fee_usdt": "5.000000"},
            "settlement": {"states": ["pending", "rejected", "completed"], "cooldown_hours": 168, "completion_idempotent": True, "completed_tx_hash_unique": True},
            "reconciliation": True,
        },
    ),
    _scenario(
        "GB-15",
        "Payout accounting lifecycle",
        "PARTIALLY AUTOMATED",
        pytest_nodes=(
            "tests/test_merchant_hmac_v2_postgres.py::test_hmac_v2_replay_idempotency_tamper_and_fail_closed",
        ),
        business_result={
            "captured": {"payout_create": True, "create_idempotency": True, "changed_payload_conflict": True, "concurrent_duplicate_protection": True},
            "not_captured": {"cancel_balance_release": True, "admin_complete_debit": True, "admin_reject_release": True, "admin_fail_release": True, "external_execution": "not implemented"},
        },
        gap="The HMAC suite proves Payout creation/idempotency, but the recovered tests do not snapshot the complete terminal balance matrix. External execution does not exist.",
    ),
    _scenario(
        "GB-16",
        "Full reconciliation",
        "AUTOMATED",
        pytest_nodes=(
            "tests/test_rolling_postgres.py::test_account_aggregates_and_reconciliation_match_transfers_and_consumptions",
            "tests/test_teamlead_postgres.py::test_postgres_independent_referral_matrix_shared_balance_settlement_and_reversal",
            "tests/test_settlement_hardening_postgres.py::test_completion_rejection_idempotency_and_tx_hash_uniqueness",
        ),
        recovery_checks=("reconciliation",),
        business_result={
            "merchant_ledger_reconciled": True,
            "trader_ledger_reconciled": True,
            "teamlead_ledger_reconciled": True,
            "rolling_aggregates_reconciled": True,
            "platform_income_reconciled": True,
            "history_rewritten": False,
        },
    ),
)


NORMALIZATION_RULES = (
    "Decimal values are serialized as non-exponent strings.",
    "UUID values and UUID-shaped strings become <uuid> unless a semantic alias is explicitly supplied.",
    "Datetime/date/time values and ISO timestamp strings become <timestamp>.",
    "Autogenerated ids, request/correlation ids, nonces and lifecycle timestamps are removed.",
    "Secret/password/token/ciphertext/encrypted fields become <redacted>.",
    "Long random hexadecimal suffixes become <random>.",
    "Mapping keys and explicitly unordered evidence sets are sorted; declared business sequences retain order.",
    "Live Rapira numeric rates and environment-specific URLs are omitted; quote side/source/freshness semantics remain.",
    "Recovery details containing run ids are reduced to check name and PASS status.",
)


def normalize(value: Any, *, key: str | None = None) -> Any:
    """Return a deterministic, secret-safe representation of observed data."""
    lowered = (key or "").lower()
    if key in VOLATILE_KEYS:
        return None
    if any(part in lowered for part in SECRET_KEY_PARTS):
        return "<redacted>"
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, uuid.UUID):
        return "<uuid>"
    if isinstance(value, (datetime, date, time)):
        return "<timestamp>"
    if isinstance(value, dict):
        normalized = {
            str(item_key): normalize(item_value, key=str(item_key))
            for item_key, item_value in sorted(value.items(), key=lambda item: str(item[0]))
            if str(item_key) not in VOLATILE_KEYS
        }
        return normalized
    if isinstance(value, (list, tuple)):
        return [normalize(item) for item in value]
    if isinstance(value, set):
        rows = [normalize(item) for item in value]
        return sorted(rows, key=lambda item: canonical_json(item))
    if isinstance(value, str):
        if UUID_RE.fullmatch(value):
            return "<uuid>"
        if TIMESTAMP_RE.fullmatch(value):
            return "<timestamp>"
        return RANDOM_SUFFIX_RE.sub("<random>", value)
    return value


def canonical_json(payload: Any) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def pretty_json(payload: Any) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
    ) + "\n"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _evidence_files(scenario: dict[str, Any]) -> list[str]:
    files = {node.split("::", 1)[0] for node in scenario["pytest_nodes"]}
    if scenario["recovery_checks"]:
        files.add("scripts/recovery_smoke.py")
    return sorted(files)


def load_recovery_checks(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {
        str(row.get("check")): str(row.get("status"))
        for row in payload
        if isinstance(row, dict) and row.get("check")
    }


def run_pytest_evidence() -> None:
    nodes = sorted(
        {
            node
            for scenario in SCENARIOS
            for node in scenario["pytest_nodes"]
        }
    )
    junit = ROOT / "test-results" / "platform1-golden-pytest.xml"
    junit.parent.mkdir(exist_ok=True)
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "-p",
        "no:cacheprovider",
        f"--junitxml={junit.as_posix()}",
        *nodes,
    ]
    print(f"Running {len(nodes)} unique recovered test oracles...", flush=True)
    result = subprocess.run(
        command,
        cwd=ROOT,
        env=local_environment(test=True),
        check=False,
    )
    if result.returncode:
        raise SystemExit(f"Golden evidence pytest failed with exit code {result.returncode}")


def render_snapshots(recovery_results: Path) -> dict[str, str]:
    recovery = load_recovery_checks(recovery_results)
    rendered: dict[str, str] = {}
    scenario_hashes: dict[str, str] = {}
    status_counts = {"AUTOMATED": 0, "PARTIALLY AUTOMATED": 0, "MANUAL": 0}

    for scenario in SCENARIOS:
        missing_recovery = [
            name
            for name in scenario["recovery_checks"]
            if recovery.get(name) != "PASS"
        ]
        if missing_recovery:
            raise SystemExit(
                f"{scenario['scenario_id']} missing PASS recovery evidence: "
                + ", ".join(missing_recovery)
            )
        evidence_files = _evidence_files(scenario)
        evidence_hashes = {}
        for relative in evidence_files:
            path = ROOT / relative
            if not path.exists():
                raise SystemExit(f"Evidence file missing: {relative}")
            evidence_hashes[relative] = _sha256(path)
        payload = normalize(
            {
                "schema_version": 1,
                "scenario_id": scenario["scenario_id"],
                "title": scenario["title"],
                "automation": scenario["automation"],
                "business_result": scenario["business_result"],
                "evidence": {
                    "pytest_nodes": sorted(scenario["pytest_nodes"]),
                    "recovery_checks": {
                        name: recovery[name]
                        for name in sorted(scenario["recovery_checks"])
                    },
                    "source_sha256": evidence_hashes,
                },
                "automation_gap": scenario["gap"],
            }
        )
        name = scenario["scenario_id"].lower() + ".json"
        body = pretty_json(payload)
        rendered[name] = body
        scenario_hashes[scenario["scenario_id"]] = hashlib.sha256(
            body.encode("utf-8")
        ).hexdigest()
        status_counts[scenario["automation"]] += 1

    index = normalize(
        {
            "schema_version": 1,
            "snapshot_set": "platform1-production-proven-baseline",
            "scenario_count": len(SCENARIOS),
            "automation_counts": status_counts,
            "normalization_rules": list(NORMALIZATION_RULES),
            "scenario_sha256": scenario_hashes,
        }
    )
    rendered["index.json"] = pretty_json(index)
    return dict(sorted(rendered.items()))


def record(snapshot_dir: Path, rendered: dict[str, str]) -> None:
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    for stale in snapshot_dir.glob("*.json"):
        if stale.name not in rendered:
            stale.unlink()
    for name, body in rendered.items():
        (snapshot_dir / name).write_text(
            body,
            encoding="utf-8",
            newline="\n",
        )
    print(f"RECORDED {len(SCENARIOS)}/16 golden scenarios in {snapshot_dir.relative_to(ROOT)}")


def verify(snapshot_dir: Path, rendered: dict[str, str]) -> int:
    failures = 0
    expected_names = set(rendered)
    actual_names = {path.name for path in snapshot_dir.glob("*.json")}
    for missing in sorted(expected_names - actual_names):
        print(f"FAIL missing snapshot: {missing}")
        failures += 1
    for extra in sorted(actual_names - expected_names):
        print(f"FAIL unexpected snapshot: {extra}")
        failures += 1
    for name, expected in rendered.items():
        path = snapshot_dir / name
        if not path.exists():
            continue
        actual = path.read_text(encoding="utf-8")
        if actual == expected:
            print(f"PASS {name}")
            continue
        failures += 1
        print(f"FAIL {name}: deterministic snapshot differs")
        diff = unified_diff(
            actual.splitlines(),
            expected.splitlines(),
            fromfile=f"baseline/{name}",
            tofile=f"observed/{name}",
            lineterm="",
        )
        for line in list(diff)[:120]:
            print(line)
    if failures:
        print(f"RESULT FAIL: {failures} golden snapshot difference(s)")
        return 1
    print(f"RESULT PASS: {len(SCENARIOS)}/16 deterministic golden snapshots match")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--record", action="store_true", help="Run evidence and record baseline snapshots.")
    mode.add_argument("--verify", action="store_true", help="Run evidence and compare with recorded snapshots.")
    parser.add_argument("--snapshot-dir", type=Path, default=DEFAULT_SNAPSHOT_DIR)
    parser.add_argument("--recovery-results", type=Path, default=DEFAULT_RECOVERY_RESULTS)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    snapshot_dir = args.snapshot_dir if args.snapshot_dir.is_absolute() else ROOT / args.snapshot_dir
    recovery_results = args.recovery_results if args.recovery_results.is_absolute() else ROOT / args.recovery_results
    run_pytest_evidence()
    rendered = render_snapshots(recovery_results)
    if args.record:
        record(snapshot_dir, rendered)
        return 0
    return verify(snapshot_dir, rendered)


if __name__ == "__main__":
    raise SystemExit(main())
