"""QR-1 immutable snapshot and cross-API active amount uniqueness."""
from alembic import op
import sqlalchemy as sa
revision='0028_static_nspk_qr'
down_revision='0027_aggregator_credentials'
branch_labels=None
depends_on=None

def upgrade():
    op.add_column('requisites',sa.Column('qr_identity',sa.String(32),nullable=True))
    op.create_index('ix_requisites_qr_identity','requisites',['qr_identity'])
    for column in [sa.Column('qr_identity',sa.String(32)),sa.Column('qr_snapshot',sa.JSON()),
                   sa.Column('qr_token',sa.String(64)),sa.Column('qr_reserved',sa.Boolean(),nullable=False,server_default=sa.false())]:
        op.add_column('deposits',column)
    op.create_unique_constraint('uq_deposits_qr_token','deposits',['qr_token'])
    op.create_index('uq_deposit_active_qr_amount','deposits',['qr_identity','amount','currency'],unique=True,postgresql_where=sa.text('qr_reserved = true'))
    op.create_check_constraint('ck_deposit_qr_snapshot','deposits',"method <> 'sbp_qr' OR (qr_identity IS NOT NULL AND qr_snapshot IS NOT NULL AND qr_token IS NOT NULL AND currency = 'RUB')")
    op.execute("""CREATE FUNCTION protect_payment_qr() RETURNS trigger AS $$
    BEGIN
      IF OLD.qr_identity IS NOT NULL AND (NEW.qr_identity IS DISTINCT FROM OLD.qr_identity
        OR NEW.qr_snapshot::jsonb IS DISTINCT FROM OLD.qr_snapshot::jsonb
        OR NEW.qr_token IS DISTINCT FROM OLD.qr_token OR NEW.amount IS DISTINCT FROM OLD.amount
        OR NEW.currency IS DISTINCT FROM OLD.currency OR NEW.method IS DISTINCT FROM OLD.method) THEN
        RAISE EXCEPTION 'issued QR snapshot is immutable';
      END IF;
      IF NEW.status IN ('paid','failed','expired','cancelled') THEN NEW.qr_reserved := false; END IF;
      RETURN NEW;
    END; $$ LANGUAGE plpgsql""")
    op.execute('CREATE TRIGGER protect_payment_qr BEFORE UPDATE ON deposits FOR EACH ROW EXECUTE FUNCTION protect_payment_qr()')

def downgrade():
    connection=op.get_bind()
    if connection.scalar(sa.text("SELECT EXISTS(SELECT 1 FROM deposits WHERE method='sbp_qr') OR EXISTS(SELECT 1 FROM requisites WHERE qr_identity IS NOT NULL)")):
        raise RuntimeError('refusing QR downgrade after activity; preserve issued snapshots')
    op.execute('DROP TRIGGER protect_payment_qr ON deposits')
    op.execute('DROP FUNCTION protect_payment_qr()')
    op.drop_constraint('ck_deposit_qr_snapshot','deposits',type_='check')
    op.drop_index('uq_deposit_active_qr_amount','deposits')
    op.drop_constraint('uq_deposits_qr_token','deposits',type_='unique')
    for name in ['qr_reserved','qr_token','qr_snapshot','qr_identity']:op.drop_column('deposits',name)
    op.drop_index('ix_requisites_qr_identity','requisites')
    op.drop_column('requisites','qr_identity')
