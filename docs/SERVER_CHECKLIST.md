# Server checklist

- Supported Linux distribution, time synchronization, and automatic security
  updates are enabled.
- Docker Engine and Compose v2 are installed from trusted repositories.
- Only SSH and the authenticated TLS reverse proxy are exposed publicly.
- PostgreSQL, Redis, application backends, and monitoring endpoints are not
  bound to public interfaces.
- Persistent volumes and encrypted off-site backups have documented owners.
- Production environment files are outside Git and readable only by the
  service operator.
- The recovery identity is stored separately from encrypted backups.
- DNS, TLS, firewall, rate limiting, monitoring profile, alerting, and log
  retention are reviewed before traffic is enabled.
- Alembic current revisions match heads for TradeSpace and Casino.
- Rollback images and the pre-deploy encrypted backup are verified.
