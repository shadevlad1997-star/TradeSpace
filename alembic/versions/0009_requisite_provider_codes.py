"""split requisite provider codes

Revision ID: 0009_requisite_provider_codes
Revises: 0008_antiscam_risk_control
Create Date: 2026-06-26
"""
from alembic import op
import sqlalchemy as sa

revision = '0009_requisite_provider_codes'
down_revision = '0008_antiscam_risk_control'
branch_labels = None
depends_on = None


def _norm(value: str | None) -> str:
    return (value or '').strip().casefold().replace('ё', 'е')


def _build_lookup(items) -> dict[str, str]:
    lookup: dict[str, str] = {}
    for item in items:
        for value in (item.code, item.display_name, *item.aliases):
            normalized = _norm(value)
            if normalized:
                lookup[normalized] = item.code
    return lookup


def _provider_lookups() -> tuple[dict[str, str], dict[str, str]]:
    try:
        from app.core.russian_banks import TOP_RU_BANKS
        from app.core.mobile_operators import MOBILE_OPERATORS
    except Exception:
        return {}, {}
    return _build_lookup(TOP_RU_BANKS), _build_lookup(MOBILE_OPERATORS)


def _backfill_provider_codes() -> None:
    bank_lookup, operator_lookup = _provider_lookups()
    if not bank_lookup and not operator_lookup:
        return

    bind = op.get_bind()
    rows = bind.execute(sa.text("select id, method, bank_name from requisites where bank_name is not null")).mappings()
    bank_methods = {'sbp', 'c2c', 'card', 'card_number'}
    mobile_methods = {'mobile', 'mobile_commerce'}
    for row in rows:
        method = (row['method'] or '').strip().lower()
        provider = _norm(row['bank_name'])
        if not provider:
            continue
        if method in bank_methods:
            bank_code = bank_lookup.get(provider)
            if bank_code:
                bind.execute(
                    sa.text("update requisites set bank_code = :bank_code where id = :id"),
                    {'bank_code': bank_code, 'id': row['id']},
                )
        elif method in mobile_methods:
            operator_code = operator_lookup.get(provider)
            if operator_code:
                bind.execute(
                    sa.text("update requisites set operator_code = :operator_code where id = :id"),
                    {'operator_code': operator_code, 'id': row['id']},
                )


def upgrade():
    op.add_column('requisites', sa.Column('bank_code', sa.String(length=64), nullable=True))
    op.add_column('requisites', sa.Column('operator_code', sa.String(length=64), nullable=True))
    op.create_index('ix_requisites_bank_code', 'requisites', ['bank_code'])
    op.create_index('ix_requisites_operator_code', 'requisites', ['operator_code'])
    _backfill_provider_codes()


def downgrade():
    op.drop_index('ix_requisites_operator_code', table_name='requisites')
    op.drop_index('ix_requisites_bank_code', table_name='requisites')
    op.drop_column('requisites', 'operator_code')
    op.drop_column('requisites', 'bank_code')
