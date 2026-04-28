# openIMIS Backend MIREVA Adapter

`openimis-be-mireva_py` is a thin Django adapter that integrates openIMIS claim workflows with the MIREVA AI fraud detection service.

## What it does

- Listens for claim updates and queues asynchronous fraud evaluation when a claim reaches `STATUS_CHECKED`.
- Maps openIMIS claims and historical context into the OMOP-normalized payload expected by MIREVA.
- Calls MIREVA through `mireva_sdk` only (no model-runtime dependencies).
- Stores decision outputs in `MirevaClaimResult` and updates claim workflow status.
- Exposes GraphQL query/mutation for reading results and submitting reviewer overrides.

## Required settings

Set these in the host openIMIS Django settings:

```python
MIREVA_URL = "http://mireva-serve:8000"
MIREVA_API_KEY = "your-api-key-here"
```

## Async flow

1. `Claim` is saved in `STATUS_CHECKED`.
2. Signal handler queues `evaluate_claim_async`.
3. Celery task invokes `MirevaService.evaluate_claim`.
4. Service maps payload, calls `MirevaClient.predict`, persists result, and applies workflow action.

## Migrations

Generate initial migration from your host openIMIS backend project:

```bash
python manage.py makemigrations mireva
python manage.py migrate
```

## Test data commands

Create ENTERED test data from recent active claims:

```bash
python manage.py mireva_prepare_entered_claims --limit 20
```

Move ENTERED claims to CHECKED:

```bash
python manage.py mireva_mark_entered_checked
```

When you specifically need Django `post_save` signals to fire:

```bash
python manage.py mireva_mark_entered_checked --trigger-signal
```

To force a random existing `ClaimAdminId` assignment before signal-save:

```bash
python manage.py mireva_mark_entered_checked --trigger-signal --assign-random-admin
```

Queue claims directly for MIREVA evaluation (no claim `save()` required):

```bash
python manage.py mireva_queue_claims --status checked --limit 20 --only-without-result
```
