"""Dry-run-first repair for one proven legacy pending Deposit without a hold."""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict
from uuid import UUID

from app.db.session import AsyncSessionLocal
from app.services.deposit_lifecycle import (
    LegacyPendingRepairBlocked,
    repair_legacy_pending_without_hold,
)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            'Verify or finalize exactly one legacy expired Deposit that has '
            'no financial hold evidence.'
        )
    )
    parser.add_argument('--deposit-id', type=UUID, required=True)
    parser.add_argument('--actor-id', type=UUID, required=True)
    parser.add_argument('--reason', required=True)
    parser.add_argument(
        '--apply',
        action='store_true',
        help='Commit the repair. Without this flag the command is read-only.',
    )
    return parser.parse_args()


async def _run(args: argparse.Namespace) -> int:
    async with AsyncSessionLocal() as db:
        try:
            result = await repair_legacy_pending_without_hold(
                db,
                deposit_id=args.deposit_id,
                actor_id=args.actor_id,
                reason=args.reason,
                dry_run=not args.apply,
            )
            if args.apply:
                await db.commit()
            else:
                await db.rollback()
        except LegacyPendingRepairBlocked as exc:
            await db.rollback()
            print(
                json.dumps(
                    {
                        'mode': 'apply',
                        'changed': False,
                        'evidence': asdict(exc.evidence),
                    },
                    default=str,
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            return 2
        except Exception:
            await db.rollback()
            raise
    print(
        json.dumps(
            {
                'mode': 'apply' if args.apply else 'dry-run',
                **asdict(result),
            },
            default=str,
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if result.evidence.eligible else 2


def main() -> int:
    return asyncio.run(_run(_arguments()))


if __name__ == '__main__':
    raise SystemExit(main())
