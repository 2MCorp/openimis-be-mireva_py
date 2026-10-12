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
MIREVA_MODEL_VERSION = "mireva-concord"  # optional; the SDK default is "mireva-origin"
MIREVA_REVIEWER_RIGHTS = [111010]  # optional; rights whose holders are notified (default: the right needed to submit a review)
MIREVA_SCOPE_REVIEWERS_BY_DISTRICT = False  # optional; True limits reviewers to their assigned districts
MIREVA_STORE_PAYLOAD = True  # default; set False to keep only a hash of each request
MIREVA_AUTO_REJECT = "hard_rules"  # default; "always" also auto-rejects model-only REJECTs
```

## Async flow

1. `Claim` is saved in `STATUS_CHECKED`.
2. Signal handler queues `evaluate_claim_async`.
3. Celery task invokes `MirevaService.evaluate_claim`.
4. Service maps payload, calls `MirevaClient.predict`, persists result, and applies workflow action.
   `APPROVE` marks the claim processed. `REVIEW` selects it for manual review. `REJECT` rejects the
   claim automatically only when a hard rule fired; a model-only `REJECT` is sent to review (set
   `MIREVA_AUTO_REJECT = "always"` to restore automatic rejection).

## OMOP code mapping

Diagnosis (ICD-10), item and service codes are translated to OMOP concept ids through
`OmopConceptMapping`. Unmapped codes are sent with a stable site-specific id (>= 2,000,000,000)
instead of `0`, so they stay distinguishable, but the model and the clinical rules only get
real signal from codes that have a proper mapping.

```bash
# See how much of the codes used by active claims are mapped, and export the gaps as a template
python manage.py mireva_mapping_coverage --top 10 --export unmapped.csv

# Fill in concept_id (and optionally concept_name/domain) in the CSV, then load it.
# Rows with an empty concept_id are skipped, so this can be done in several passes.
python manage.py mireva_load_omop_mappings unmapped.csv --dry-run
python manage.py mireva_load_omop_mappings unmapped.csv
```

ICD-10 codes are matched without dots and case-insensitively (`A00.0` equals `A000`).
Standard concept ids come from OHDSI Athena (https://athena.ohdsi.org); concept ids in the
local range are rejected by the loader.

## Review notifications and rejection reasons

- **Notifications:** when a claim goes to manual review (including a model-only `REJECT` that was
  downgraded), every active user whose unblocked role holds the reviewer right gets an in-app
  notification linking to the claim, with the risk level, score and top signals. It is sent after
  the transaction commits, once per claim, and a failure never undoes the claim decision. With `MIREVA_SCOPE_REVIEWERS_BY_DISTRICT = True` a reviewer who has district assignments is only
  notified for claims of those districts (reviewers without assignments are notified for all).
  Requires the `notification` module; without it the review is only logged. Push over websocket needs a
  reachable channel layer (RabbitMQ), as configured for the notification module.
- **Rejection reasons:** hard-rule rejections use openIMIS reason codes: visit outside the policy
  window is `21` (no coverage); amount, deceased patient, duplicate and gender/diagnosis rules are
  `20` (invalid claim). Anything else is `20`. `Claim.explanation` lists the rule details.

## Audit trail, recovery and feedback

- **Audit:** every evaluation is stored in `MirevaEvaluation` (decision, action taken, model
  version, request hash and the request payload). `MirevaClaimResult` keeps only the latest
  result. Payloads contain claim/patient UUIDs and codes, no names; set
  `MIREVA_STORE_PAYLOAD = False` to keep only the hash (this disables the training export).
- **Recovery:** claims that stayed `CHECKED` without a result (for example the broker was down)
  are picked up by `python manage.py mireva_sweep_unevaluated`, or schedule the Celery task
  `mireva.tasks.sweep_unevaluated_claims` with Celery beat, e.g. every 10 minutes.
- **Feedback:** `python manage.py mireva_export_training_data --output labels.jsonl` writes
  reviewer decisions with the request MIREVA saw (labels `APPROVE=0`, `REJECT=2`; reviewer
  `REVIEW` rows are skipped). It also prints how often reviewers overrode MIREVA per model
  version, which is the first drift signal to watch.

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
