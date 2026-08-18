# TractorCloser pilot workspace

This package contains the TractorCloser frontend, FastAPI backend, and the
release-readiness material for the Tractor Bob pilot.

## Run locally

Open `index.html` in a browser. It routes to the TractorCloser interface and runs with preview data when no backend is connected.

## Deploy as a static site

Upload every file in this package to the root of a GitHub repository. For Render, create a Static Site from that repository and publish the repository root.

## Current scope

Authentication, persistent CRM data, roles, exports, audit history, follow-up
records, lead intake review, and optional AI coaching are implemented for the
pilot. External lead, messaging, phone, and inventory providers remain
disconnected by design.

See [launch readiness](docs/launch-readiness.md) and
[integration contracts](docs/integration-contracts.md) before production or any
real provider connection.

## Browser notifications

The pilot includes installable-web-app files and opt-in browser notifications.
See [browser push setup](docs/browser-push-setup.md) before adding the protected
VAPID settings in Render.
