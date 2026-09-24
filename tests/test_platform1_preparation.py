import json
from decimal import Decimal
from pathlib import Path
import uuid

from scripts import platform1_freeze_guard as freeze_guard
from scripts import platform1_golden as golden


def _recovery_result_fixture(path: Path) -> str:
    checks = sorted(
        {
            name
            for scenario in golden.SCENARIOS
            for name in scenario["recovery_checks"]
        }
    )
    volatile_marker = f"volatile-{uuid.uuid4()}"
    path.write_text(
        json.dumps(
            [
                {
                    "check": name,
                    "status": "PASS",
                    "detail": volatile_marker,
                }
                for name in checks
            ]
        ),
        encoding="utf-8",
    )
    return volatile_marker


def test_golden_manifest_covers_gb01_through_gb16_once():
    ids = [scenario["scenario_id"] for scenario in golden.SCENARIOS]
    assert ids == [f"GB-{number:02d}" for number in range(1, 17)]
    counts = {
        status: sum(
            scenario["automation"] == status
            for scenario in golden.SCENARIOS
        )
        for status in ("AUTOMATED", "PARTIALLY AUTOMATED", "MANUAL")
    }
    assert counts == {
        "AUTOMATED": 9,
        "PARTIALLY AUTOMATED": 7,
        "MANUAL": 0,
    }
    assert all(
        scenario["business_result"]
        for scenario in golden.SCENARIOS
    )
    assert all(
        scenario["automation"] == "AUTOMATED" or scenario["gap"]
        for scenario in golden.SCENARIOS
    )


def test_normalizer_removes_volatile_values_redacts_and_preserves_sequences():
    payload = {
        "amount": Decimal("1250.500000"),
        "id": str(uuid.uuid4()),
        "merchant_id": str(uuid.uuid4()),
        "created_at": "2026-01-02T03:04:05Z",
        "webhook_secret": "must-not-leak",
        "ordered_status_codes": [500, 204],
        "unordered": {"b", "a"},
        "external_id": "golden-abcdef1234567890abcdef1234567890",
    }
    normalized = golden.normalize(payload)
    assert "id" not in normalized
    assert normalized["merchant_id"] == "<uuid>"
    assert "created_at" not in normalized
    assert normalized["webhook_secret"] == "<redacted>"
    assert normalized["amount"] == "1250.500000"
    assert normalized["ordered_status_codes"] == [500, 204]
    assert normalized["unordered"] == ["a", "b"]
    assert normalized["external_id"] == "golden-<random>"


def test_rendered_golden_set_is_byte_deterministic():
    recovery = Path("test-results/platform1-unit-live-smoke.json")
    recovery.parent.mkdir(exist_ok=True)
    try:
        volatile_marker = _recovery_result_fixture(recovery)
        first = golden.render_snapshots(recovery)
        second = golden.render_snapshots(recovery)
        assert first == second
        assert set(first) == {
            "index.json",
            *{f"gb-{number:02d}.json" for number in range(1, 17)},
        }
        index = json.loads(first["index.json"])
        assert index["scenario_count"] == 16
        assert index["automation_counts"] == {
            "AUTOMATED": 9,
            "MANUAL": 0,
            "PARTIALLY AUTOMATED": 7,
        }
        assert volatile_marker not in "".join(first.values())
    finally:
        recovery.unlink(missing_ok=True)


def test_freeze_guard_maps_all_areas_and_protected_controller():
    assert [area.area_id for area in freeze_guard.AREAS] == [
        f"FZ-{number:02d}" for number in range(1, 19)
    ]
    protected = freeze_guard.collect_protected_files()
    assert "app/web/routes.py" in protected
    assert protected["app/web/routes.py"]["areas"] == ["FZ-18"]
    assert protected["app/api/v1/admin.py"]["areas"] == ["FZ-18"]
    assert protected["app/schemas/common.py"]["areas"] == ["FZ-18"]
    assert "app/services/requisites.py" in protected
    for path in ("app/web/__init__.py", "app/presentation/tradespace/routes.py", "app/presentation/tradespace/wallet.py"):
        assert protected[path]["areas"] == ["FZ-03"]
    assert protected["app/services/requisites.py"]["areas"] == ["FZ-05"]
    assert any(path.startswith("alembic/versions/") for path in protected)
    assert all(row["sha256"] for row in protected.values())


def test_freeze_guard_reports_area_and_requires_explicit_override(capsys):
    baseline = freeze_guard.build_baseline()
    baseline["files"]["app/services/requisites.py"]["sha256"] = "0" * 64
    baseline_path = Path("test-results/platform1-unit-freeze.json")
    baseline_path.parent.mkdir(exist_ok=True)
    baseline_path.write_text(json.dumps(baseline), encoding="utf-8")
    try:
        assert freeze_guard.verify_baseline(
            baseline_path,
            allowed_areas=set(),
            override_reason=None,
        ) == 1
        output = capsys.readouterr().out
        assert "app/services/requisites.py" in output
        assert "FZ-05" in output
        assert "Deterministic routing" in output

        assert freeze_guard.verify_baseline(
            baseline_path,
            allowed_areas={"FZ-05"},
            override_reason="Authorized isolated routing core task",
        ) == 0
        output = capsys.readouterr().out
        assert "OVERRIDE protected file modified" in output
        assert "OVERRIDE REASON" in output
    finally:
        baseline_path.unlink(missing_ok=True)
