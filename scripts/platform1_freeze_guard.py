"""Guard Platform #1 work against accidental processing-core changes.

The recorded baseline intentionally hashes the current recovery worktree rather
than Git HEAD: recovery changes that pre-date this gate are therefore accepted,
while later edits to FZ-01..FZ-18 fail by default.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASELINE = ROOT / "tests" / "golden" / "freeze_zone_baseline.json"


@dataclass(frozen=True)
class FreezeArea:
    area_id: str
    reason: str
    patterns: tuple[str, ...]


AREAS = (
    FreezeArea("FZ-01", "Production schema, persisted entities, constraints and migration history.", ("app/models/*.py", "app/core/enums.py", "alembic/versions/*.py")),
    FreezeArea("FZ-02", "Merchant HMAC, replay protection and API-key mode are external security contracts.", ("app/api/deps.py", "app/core/merchant_hmac.py", "app/services/merchant_api_keys.py", "app/services/integration_modes.py", "app/api/v1/integration.py", "app/db/session.py", "app/core/config.py")),
    FreezeArea("FZ-03", "Shared authentication, realm, CSRF/session, product-only entry routes and wallet-reader authorization.", ("app/core/security.py", "app/core/logging.py", "app/core/session.py", "app/core/middleware.py", "app/core/auth_hardening.py", "app/core/rate_limit.py", "app/core/client_ip.py", "app/core/access.py", "app/web/__init__.py", "app/presentation/tradespace/routes.py", "app/presentation/tradespace/wallet.py")),
    FreezeArea("FZ-04", "Idempotent Deposit/Aggregator creation and initial collateral reservation.", ("app/api/v1/merchant.py", "app/services/aggregators.py")),
    FreezeArea("FZ-05", "Deterministic routing, Requisite eligibility and collateral reservation.", ("app/services/requisites.py",)),
    FreezeArea("FZ-06", "Fee resolution, immutable fee snapshots and platform-margin mathematics.", ("app/services/fee_tiers.py", "app/services/fees.py", "app/services/platform_income.py")),
    FreezeArea("FZ-07", "Merchant/Trader balance mutation and reconciliation semantics.", ("app/services/ledger.py", "app/services/finance_reconciliation.py")),
    FreezeArea("FZ-08", "Authoritative Deposit confirmation, failure and expiration transitions.", ("app/services/deposit_confirmation.py", "app/services/deposit_lifecycle.py", "app/services/deposit_ttl.py")),
    FreezeArea("FZ-09", "Existing Payout reservation, completion and release accounting.", ("app/services/payouts.py",)),
    FreezeArea("FZ-10", "Rolling funding, FIFO recovery and strict financial quote behavior.", ("app/services/rolling.py", "app/services/rapira.py")),
    FreezeArea("FZ-11", "TeamLead attribution, accrual, debt, reversal and settlement accounting.", ("app/services/teamlead.py",)),
    FreezeArea("FZ-12", "Merchant settlement freeze, completion and rejection accounting.", ("app/services/settlements.py",)),
    FreezeArea("FZ-13", "Appeal and SMS paths that can change Deposit and financial outcomes.", ("app/services/appeals.py", "app/services/sms.py")),
    FreezeArea("FZ-14", "Webhook payload, signature, durable delivery and enqueue semantics.", ("app/services/webhook_payloads.py", "app/services/webhooks.py", "app/services/webhook_signing_keys.py", "app/services/webhook_enqueue.py")),
    FreezeArea("FZ-15", "Aggregator processing, authentication and callback delivery semantics.", ("app/api/v1/aggregator.py", "app/services/aggregators.py", "app/services/aggregator_credentials.py", "app/services/aggregator_enqueue.py")),
    FreezeArea("FZ-16", "Risk, antiscam, compliance and provider eligibility used by routing.", ("app/services/risk.py", "app/services/antiscam.py", "app/services/compliance.py", "app/core/compliance.py", "app/core/validators.py", "app/core/payment_methods.py", "app/core/requisite_providers.py", "app/core/russian_banks.py", "app/core/mobile_operators.py")),
    FreezeArea("FZ-17", "Celery lifecycle, expiration and delivery scheduling.", ("app/workers/*.py",)),
    FreezeArea(
        "FZ-18",
        "Existing browser/admin mutation handlers, schemas and API contract ordering.",
        (
            "app/web/routes.py",
            "app/api/v1/admin.py",
            "app/api/v1/cabinet.py",
            "app/api/v1/router.py",
            "app/schemas/common.py",
            "app/main.py",
        ),
    ),
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_value(*args: str) -> str:
    return subprocess.check_output(
        ["git", *args], cwd=ROOT, text=True, encoding="utf-8"
    ).strip()


def collect_protected_files() -> dict[str, dict[str, object]]:
    protected: dict[str, dict[str, object]] = {}
    for area in AREAS:
        matched: set[Path] = set()
        for pattern in area.patterns:
            matched.update(
                path for path in ROOT.glob(pattern)
                if path.is_file() and "__pycache__" not in path.parts
            )
        if not matched:
            raise RuntimeError(f"{area.area_id} has no matching protected files")
        for path in sorted(matched):
            relative = path.relative_to(ROOT).as_posix()
            row = protected.setdefault(
                relative,
                {"sha256": _sha256(path), "areas": []},
            )
            row["areas"].append(area.area_id)
    for row in protected.values():
        row["areas"] = sorted(set(row["areas"]))
    return dict(sorted(protected.items()))


def build_baseline() -> dict[str, object]:
    return {
        "schema_version": 1,
        "baseline_branch": _git_value("branch", "--show-current"),
        "baseline_commit": _git_value("rev-parse", "HEAD"),
        "areas": [
            {
                "id": area.area_id,
                "reason": area.reason,
                "patterns": list(area.patterns),
            }
            for area in AREAS
        ],
        "files": collect_protected_files(),
    }


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def verify_baseline(
    baseline_path: Path,
    *,
    allowed_areas: set[str],
    override_reason: str | None,
) -> int:
    if not baseline_path.exists():
        print(f"FAIL freeze baseline missing: {baseline_path.relative_to(ROOT)}")
        return 2
    known_ids = {area.area_id for area in AREAS}
    unknown = allowed_areas - known_ids
    if unknown:
        print("FAIL unknown override area(s): " + ", ".join(sorted(unknown)))
        return 2
    if allowed_areas and (not override_reason or len(override_reason.strip()) < 12):
        print("FAIL explicit core override requires --reason with at least 12 characters")
        return 2

    recorded = json.loads(baseline_path.read_text(encoding="utf-8"))
    expected_files = recorded["files"]
    current_files = collect_protected_files()
    area_reasons = {area.area_id: area.reason for area in AREAS}
    all_paths = sorted(set(expected_files) | set(current_files))
    blocked: list[tuple[str, list[str], str]] = []
    overridden: list[tuple[str, list[str], str]] = []

    for relative in all_paths:
        expected = expected_files.get(relative)
        current = current_files.get(relative)
        if expected == current:
            continue
        if expected is None:
            state = "added"
            areas = list(current["areas"])
        elif current is None:
            state = "deleted"
            areas = list(expected["areas"])
        else:
            state = "modified"
            areas = sorted(set(expected["areas"]) | set(current["areas"]))
        item = (relative, areas, state)
        if set(areas).issubset(allowed_areas):
            overridden.append(item)
        else:
            blocked.append(item)

    for relative, areas, state in blocked:
        print(f"FAIL protected file {state}: {relative}")
        for area_id in areas:
            print(f"  {area_id}: {area_reasons[area_id]}")
    for relative, areas, state in overridden:
        print(
            f"OVERRIDE protected file {state}: {relative} "
            f"({', '.join(areas)})"
        )
    if overridden:
        print(f"OVERRIDE REASON: {override_reason.strip()}")
    if blocked:
        print(
            "RESULT FAIL: core freeze violation. Use --allow only in a separately "
            "authorized core-change task."
        )
        return 1

    print(
        f"RESULT PASS: {len(expected_files)} protected files across "
        f"{len(AREAS)} freeze areas match the recorded baseline."
    )
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--baseline",
        type=Path,
        default=DEFAULT_BASELINE,
        help="Freeze baseline JSON path.",
    )
    parser.add_argument(
        "--record-baseline",
        action="store_true",
        help="Record hashes for the current explicitly accepted recovery worktree.",
    )
    parser.add_argument(
        "--allow",
        action="append",
        default=[],
        metavar="FZ-NN",
        help="Explicitly allow one freeze area for an authorized core task.",
    )
    parser.add_argument(
        "--reason",
        help="Required explanation when --allow is used.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    baseline_path = args.baseline
    if not baseline_path.is_absolute():
        baseline_path = ROOT / baseline_path
    if args.record_baseline:
        if args.allow:
            raise SystemExit("--allow cannot be combined with --record-baseline")
        payload = build_baseline()
        _write_json(baseline_path, payload)
        print(
            f"RECORDED {len(payload['files'])} protected files across "
            f"{len(payload['areas'])} areas: {baseline_path.relative_to(ROOT)}"
        )
        return 0
    return verify_baseline(
        baseline_path,
        allowed_areas=set(args.allow),
        override_reason=args.reason,
    )


if __name__ == "__main__":
    raise SystemExit(main())
