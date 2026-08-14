# MachineMatch CRM Integration

## Purpose
MachineMatch is the ecommerce/fitment front end. TractorCloser is the sales CRM and follow-up workspace.

## Lead flow
1. Customer voluntarily submits a phone number (preferred) and optionally email through MachineMatch.
2. MachineMatch attaches known context: mower brand, model, year, deck, serial number, viewed products, saved cart/order, cart value, fitment confidence, source/campaign, and timestamp.
3. A lead is created or updated in TractorCloser.
4. TractorCloser assigns a priority score and pipeline stage.
5. Zachary works the lead from TractorCloser: call, text, notes, quote, follow-up, won/lost.

## Pipeline
NEW -> CONTACTED -> QUALIFIED -> QUOTE -> FOLLOW-UP -> WON / LOST

## Priority model
- +10 phone captured
- +10 machine identified
- +15 fitment verified
- +20 saved cart/order
- +25 checkout started
- +30 cart value >= $200
- +40 high-value/ACS interest

80+ HOT / 50-79 WARM / below 50 NURTURE

## Minimum lead payload
```json
{
  "source": "MachineMatch",
  "name": "",
  "phone": "",
  "email": "",
  "sms_consent": false,
  "machine": {"brand":"", "model":"", "year":"", "deck":"", "serial":""},
  "interest": [{"sku":"", "title":"", "quantity":1}],
  "cart_value": 0,
  "fitment_status": "",
  "source_campaign": "",
  "priority_score": 0,
  "stage": "NEW",
  "created_at": ""
}
```

## Privacy
Only send customer information the customer voluntarily provides or that Shopify legitimately makes available for the customer's transaction/checkout. SMS marketing requires explicit consent. Do not scrape or enrich private personal data.

## Implementation order
1. Build MachineMatch lead-capture funnel.
2. Add a server-side integration layer/webhook rather than exposing CRM credentials in Shopify theme JavaScript.
3. Create/update TractorCloser lead records idempotently.
4. Display MachineMatch context in the TractorCloser lead detail view.
5. Add call/text/follow-up actions and status tracking.
6. Add abandoned-cart recovery and service reminders after the core flow is reliable.
