# HANDOFF — CryoHealth-geo — 2026-11-05 PKT
Session: sns-webhook-review  Model: claude-sonnet-5-5  Branch: feat/23-aws-sns-stac-webhook  Goal: none  Task: #23

## State
PR #28 (fork `husni0041/CryoHealth-geo` → `uExel/CryoHealth-geo:main`) adds
`POST /webhooks/stac-event`, which accepts signed AWS SNS envelopes, verifies the RSA
signature against the one configured `AWS_SNS_TOPIC_ARN`, confirms subscriptions, and
queues the existing observation + hazard pass in the background. Code-complete and
mocked-tested; **not** verified against a live SNS topic.

## Done this session
- Reviewed PR #28 against issue #23's tasks and acceptance criteria.
- Fixed a real defect: AWS SNS posts its JSON envelope as `Content-Type: text/plain`, which
  the typed `payload: dict` endpoint rejected with 422 (so real SNS delivery and
  subscription confirmation would have failed). The endpoint now reads the raw body,
  returns 400 on malformed JSON, and runs the blocking verify/confirm calls in a threadpool.
- Added `_event_job`: bursts of notifications (one per scene) now coalesce into one running
  pass plus at most one queued follow-up, instead of N concurrent full passes.
- Added `pipeline/stac_event.py`: a notification is skipped (202 `ignored`) only when positively
  irrelevant — a non-Sentinel-2 collection (Sentinel-1 is not ingested) or a footprint outside
  `STAC_EVENT_BBOX` (default approx. Gilgit-Baltistan 72.0,34.0,78.0,37.2). Unreadable messages
  are processed, not dropped (a redundant pass is harmless; a missed scene delays an alert).
- Added tests: text/plain body, malformed JSON (400), invalid signature (401), burst
  coalescing, recovery after a failed pass, and event filtering (collection/bbox/geometry).

## Not done / deferred
- Live verification: set `AWS_SNS_TOPIC_ARN`, expose a public HTTPS endpoint, subscribe the
  topic with a Sentinel-2 filter policy, and confirm a real event drives observation +
  hazard scoring (issue #23's "within 3 minutes" criterion is unproven until then).
- Sentinel-1 *ingestion* is unsupported by the pipeline (S1 events are now skipped, not
  processed); issue #23 mentions it. The issue's HMAC-SHA256 wording is superseded by SNS RSA
  signature verification (AWS signs with RSA).
- Copernicus Data Space (CDSE) notifications are not handled: only the AWS SNS envelope is
  implemented, and the CDSE push format/auth has not been verified against a live subscription.
- The default bbox is an approximation of Gilgit-Baltistan, not the exact boundary; tighten via
  `STAC_EVENT_BBOX` if needed.

## Next action
Merge PR #28, then deploy with `AWS_SNS_TOPIC_ARN` set and run the live SNS check above;
only then tick issue #23's acceptance criteria.

## Open questions for a human
- Which AWS SNS topic ARN / public hostname will production subscribe to?

## Failed approaches (do not retry)
- Typing the webhook body as `payload: dict` — FastAPI only JSON-parses
  `application/json`, so SNS's `text/plain` delivery 422s. Read the raw body instead.

## Loops run
- Local `pytest` + `ruff check`: 91 passed. 2 failed + 2 errors in `test_cdse_source` /
  `test_exposure` are environmental (rasterio picks up PostgreSQL's incompatible PROJ
  database on this Windows machine) and unrelated to this change.

## Files touched
pipeline/service.py, pipeline/stac_event.py (new), tests/test_service.py,
tests/test_stac_event.py (new), README.md, .env.example, docs/ai/HANDOFF.md
(earlier on this branch: pipeline/sns_webhook.py, tests/test_sns_webhook.py, pyproject.toml,
uv.lock)

## Verification status
`ruff check .` clean; `pytest` 91 passed with the 4 environmental failures noted above.
No live AWS SNS or satellite calls were made.

## Resume with
/uexel:orient
