# Browser and mobile push notifications

TractorCloser supports standard Web Push. It stores each approved device
subscription in the TractorCloser database and uses VAPID credentials from the
server to deliver visible alerts. No paid notification vendor is required.

## What users will receive

- A salesperson receives a lead assignment.
- Managers receive a recorded deal from another salesperson.
- Developers receive developer sign-in and security-control events.

In-app notifications are saved even when browser delivery is unavailable.

## Render settings

Add these protected environment variables to the **tractorcloser-api** service;
never add them to GitHub or a frontend file:

- `VAPID_PUBLIC_KEY`
- `VAPID_PRIVATE_KEY`
- `VAPID_CLAIMS_EMAIL` — a monitored organization email address.

Generate one VAPID key pair for TractorCloser and retain it for the life of the
application. Replacing the pair invalidates existing device subscriptions, so
users will simply enable notifications again.

## User setup

1. Open TractorCloser using its HTTPS address.
2. On iPhone or iPad, use Safari’s Share menu to add TractorCloser to the Home
   Screen, then open that installed app.
3. Open **Account** and select **Enable browser notifications**.
4. Approve the operating-system permission prompt.

Do not request permission automatically at sign-in. It should be requested only
after the user chooses to enable notifications.

## Delivery boundary

The current release delivers events created by TractorCloser. Overdue
follow-up alerts need a scheduled server job before they can arrive when no one
has TractorCloser open; add that job before production use.
