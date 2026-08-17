# Integration contracts

No provider is enabled by default. Every connection must be tested in staging,
recorded in Integration Health, and approved by a dealership administrator.

## Common incoming lead contract

Every provider should send or map these fields when available:

| Field | Required | Rule |
| --- | --- | --- |
| Source | Yes | Website, Marketplace, business phone, email, or another named source |
| External source ID | Yes when available | Stable provider record/message ID for traceability and duplicate review |
| Received time | Yes | Preserve provider time and normalize for display in dealership time zone |
| Name, phone, email | No | Missing information must not discard the lead |
| First inquiry | No | Preserve original wording and channel context |
| Product interest | No | Keep as unstructured text until inventory matching is enabled |
| Consent / opt-out status | Yes when known | Never invent permission; use `Unknown` when absent |

## Intake and duplicate rules

1. A received record enters **Intake**, not Pipeline.
2. Exact phone, email, or provider ID matches are suggested to an admin.
3. A manager chooses: create customer, attach to existing customer, or not a
   lead.
4. Assignment happens only after that decision.
5. The original source and source reference remain on the customer record.

## Outbound messaging rules

- The composer may prepare text only; it does not send until a provider is
  connected.
- A reachable channel requires a matching phone, email, or social connection.
- Opted-out contacts cannot be offered reply actions.
- A send provider must return a provider message ID and delivery status, both
  recorded in customer activity.
- Callback actions are activities, not messages.

## Recommended connection order

1. Website form or a controlled CSV/manual inbox import.
2. Inventory feed in read-only mode.
3. One messaging provider with outbound delivery receipts.
4. Social marketplace channels.
5. Business phone/call logging and notifications.

Each connection should have a disabled switch, a last-successful-sync time,
an error summary, and a small staging test before live use.
