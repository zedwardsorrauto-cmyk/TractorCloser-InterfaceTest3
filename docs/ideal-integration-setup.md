# Ideal read-only connection test

This first Ideal slice only verifies that TractorCloser can read a single inventory record. It cannot create or change a customer, order, unit, or any other Ideal record.

## Render settings

Add these values in the TractorCloser API service's Environment section. Keep every value private; do not add them to GitHub or the frontend.

- `IDEAL_API_BASE_URL` — the HTTPS address supplied by Ideal, with no trailing slash.
- `IDEAL_API_USERNAME` — the dedicated Ideal API user.
- `IDEAL_API_PASSWORD` — that user's password.
- `IDEAL_COMPANY_ID` — the Ideal company identifier.
- `IDEAL_LOCATION_ID` — the dealership location identifier.
- `IDEAL_API_TEST_STOCK_NUMBER` — optional known in-stock unit for a precise test. Leave blank to request one in-stock unit.

The test intentionally refuses non-HTTPS endpoints because it uses Basic Authentication. Ask Ideal for their secure HTTPS endpoint or an approved secure connection path before enabling this test.

## Running the test

Sign in as the Developer account, enter the Tractor Bob support workspace, then open **Integration Health** and choose **Test connection**. A successful test records an audit event and reports only a connection result, response time, and record count.

## Next phase

After this passes, add inventory search and customer matching. Keep customer creation and order posting disabled until their own confirmed test plan is complete.
