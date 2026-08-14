# TractorCloser Backend

This first backend slice establishes the secure foundations for the Tractor Bob test workspace:

- Email/password authentication
- Salesperson, Admin, and Developer roles
- Workspace separation and Central Time configuration
- Audited developer support access
- A first administrative CSV export endpoint

## Local setup

1. Create a Python virtual environment and install `requirements.txt`.
2. Copy `.env.example` to `.env`.
3. Set a unique password for each `SEED_*_PASSWORD` variable.
4. Run `uvicorn app.main:app --reload` from this `backend` folder.

The application seeds the Tractor Bob workspace and test users only when the corresponding password environment variables are set. Passwords are never stored in source control.

## Next build slice

Add tenant-scoped customer, pipeline, activity, follow-up, deal, quote, and inventory tables before connecting the existing interface to the API.
