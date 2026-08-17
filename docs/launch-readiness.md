# TractorCloser launch readiness

This checklist is for the point between a successful interface pilot and the
first real customer-data connection. Complete it in a separate staging
environment before enabling any live integration.

## Environments

| Environment | Purpose | Data |
| --- | --- | --- |
| Tractor Bob pilot | Product testing and user acceptance | Clearly marked test records only |
| Staging | Deploy and integration rehearsal | Synthetic data, no customer information |
| Production | Dealer operation | Real dealership data only |

Create a separate Render API service and Postgres database for staging. Use a
different JWT secret, different seed passwords, and a staging frontend URL.
Set `APP_ENVIRONMENT=staging`. Do not point staging at the production database.

## Before production

- [ ] Confirm every account role: salesperson, admin, and developer.
- [ ] Confirm a salesperson only sees leads and deals assigned to that person.
- [ ] Confirm an admin can assign, export, create salesperson accounts, and
      review Intake.
- [ ] Confirm developer support access and lockdown are restricted to developer
      accounts and leave an audit event.
- [ ] Download an admin workspace backup and open every CSV inside it.
- [ ] Confirm current-month dashboard metrics match the Deals page.
- [ ] Confirm a new lead, a customer update, a follow-up, a sent response, a
      sold deal, and a reverted sale still exist after signing out and back in.
- [ ] Replace all seed passwords and confirm `JWT_SECRET` is generated and not
      stored in GitHub.
- [ ] Set `ALLOWED_ORIGINS` to the exact production frontend domain. Never use
      the open testing default in production.
- [ ] Set `APP_ENVIRONMENT=production`.
- [ ] Remove or archive marked test data only after making a verified backup.

## Release process

1. Upload the release to GitHub and let the staging service deploy it first.
2. Verify `/health` reports status `ok` and the expected schema version.
3. Run the acceptance checklist above using staging accounts.
4. Download and inspect a staging backup.
5. Deploy the same committed release to production.
6. Run a short production smoke test with an admin account, then monitor audit
   history and Render logs.

## Database upgrades

The API records schema upgrades in the `schema_migrations` table. Future
database changes must be added as a new numbered upgrade in
`backend/app/migrations.py`; do not add untracked startup SQL. A backup should
be downloaded before every release that includes a new migration.
