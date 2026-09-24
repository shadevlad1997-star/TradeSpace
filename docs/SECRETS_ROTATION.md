# Secrets rotation

Rotate all credentials that were ever displayed in a terminal, chat, URL,
screen capture, exported environment, or support bundle before production.

1. Generate independent high-entropy values for every application and database
   secret; never reuse values between TradeSpace, Casino, Redis, PostgreSQL,
   monitoring, providers, or backup encryption.
2. Store new values in the production secrets manager and record only owner,
   purpose, creation time, and rotation due date in the inventory.
3. Rotate provider keys with overlap where supported, validate signed callbacks,
   and then revoke the previous keys.
4. Rotate application signing keys using the documented session invalidation
   window and revoke old sessions.
5. Rotate the backup recipient by creating a new offline identity, producing
   and restoring a backup for the new recipient, then retiring old artifacts
   only with owner approval.
6. Confirm exact secret values do not occur in application, proxy, worker, or
   audit logs.

Never print secret values in deployment reports or commit them to Git.
