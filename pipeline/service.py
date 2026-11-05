"""uvicorn pipeline.service:app

GET  /health        — liveness
POST /run            — trigger one observation batch pass now (same logic the daily
                        scheduler runs)
POST /run-hazard     — trigger one hazard-scoring pass now, independent of /run

POST /webhooks/stac-event — receive a signed AWS SNS notification and queue the same
                            observation + hazard pass used by the daily schedule

The manual POST routes /run and /run-hazard have no auth and are intended for a private
network. The SNS webhook instead verifies AWS's signed envelope against one configured
topic before accepting requests. Outbound calls to CryoHealth-api's /alerts/hazard-scores
use service-to-service auth (CryoHealth-api#12).

The scheduler runs in-process (APScheduler) rather than relying on an external cron
hitting this service, so "the service is up" and "the schedule runs" are the same
guarantee — no separate scheduler infra to keep in sync with this one. The daily job
runs the observation batch and the hazard pass back to back, in that order, so hazard
scoring always sees that run's freshest observation (if any landed).
"""

from __future__ import annotations

import json
import logging
import os
import threading
from contextlib import asynccontextmanager
from dataclasses import asdict

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool

from pipeline.batch import run_latest
from pipeline.hazard_batch import run_hazard_pass
from pipeline.sns_webhook import SnsWebhookError, confirm_subscription, verify_message
from pipeline.stac_event import should_process
from pipeline.stac_source import PlanetaryComputerSource

logger = logging.getLogger(__name__)

RUN_INTERVAL_HOURS = 24
AWS_SNS_TOPIC_ARN_ENV = "AWS_SNS_TOPIC_ARN"


def _make_source():
    if os.environ.get("CDSE_CLIENT_ID"):
        from pipeline.cdse_source import CdseSource

        return CdseSource()
    logger.warning("CDSE_CLIENT_ID not set — falling back to Planetary Computer (not the production source)")
    return PlanetaryComputerSource()


def _run_observations() -> None:
    source = _make_source()
    results = run_latest(source)
    for r in results:
        logger.info(
            "batch: %s written=%d checked=%d stale=%s error=%s",
            r.slug,
            r.observations_written,
            r.scenes_checked,
            r.stale,
            r.error,
        )


def _run_hazard() -> None:
    results = run_hazard_pass()
    for r in results:
        logger.info(
            "hazard: %s score=%s tier=%s alert_created=%s error=%s",
            r.slug,
            r.score,
            r.tier,
            r.alert_created,
            r.error,
        )


def _daily_job() -> None:
    _run_observations()
    _run_hazard()


_event_state = threading.Lock()
_event_running = False
_event_rerun = False


def _event_job() -> None:
    """Run the pipeline for a webhook event, coalescing bursts of notifications.

    SNS can deliver many notifications close together (one per scene). Only one
    event-driven pass runs at a time; notifications arriving mid-run queue exactly one
    follow-up pass so a scene published during a run is still picked up.
    """
    global _event_running, _event_rerun
    with _event_state:
        if _event_running:
            _event_rerun = True
            logger.info("webhook: pipeline pass already running, queued one follow-up")
            return
        _event_running = True
    try:
        while True:
            _daily_job()
            with _event_state:
                if not _event_rerun:
                    _event_running = False
                    return
                _event_rerun = False
    except BaseException:
        with _event_state:
            _event_running = False
            _event_rerun = False
        raise


scheduler = AsyncIOScheduler()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    scheduler.add_job(
        _daily_job, IntervalTrigger(hours=RUN_INTERVAL_HOURS), id="daily-job", replace_existing=True
    )
    scheduler.start()
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(title="CryoHealth-geo", lifespan=lifespan)


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "scheduler_running": scheduler.running}


@app.post("/run")
def run_now() -> dict:
    source = _make_source()
    results = run_latest(source)
    return {"results": [asdict(r) for r in results]}


@app.post("/run-hazard")
def run_hazard_now() -> dict:
    results = run_hazard_pass()
    return {"results": [asdict(r) for r in results]}


@app.post("/webhooks/stac-event", status_code=202)
async def receive_stac_event(request: Request, background_tasks: BackgroundTasks) -> dict:
    topic_arn = os.environ.get(AWS_SNS_TOPIC_ARN_ENV)
    if not topic_arn:
        raise HTTPException(status_code=503, detail="AWS SNS webhook is not configured")

    # AWS SNS posts its JSON envelope as Content-Type text/plain, which FastAPI's
    # typed body parsing would reject, so parse the raw body regardless of content type.
    try:
        payload = json.loads(await request.body())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="AWS SNS request is not valid JSON") from exc

    # verify/confirm make blocking HTTP calls (certificate fetch, SubscribeURL), so keep
    # them off the event loop.
    try:
        message_type = await run_in_threadpool(verify_message, payload, topic_arn)
        if message_type == "SubscriptionConfirmation":
            await run_in_threadpool(confirm_subscription, payload, topic_arn)
    except SnsWebhookError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    if message_type == "Notification":
        try:
            relevant, reason = should_process(payload.get("Message"))
        except ValueError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        if not relevant:
            logger.info("webhook: ignored notification: %s", reason)
            return {"status": "ignored", "reason": reason}
        background_tasks.add_task(_event_job)
        return {"status": "accepted"}
    if message_type == "SubscriptionConfirmation":
        return {"status": "subscription confirmed"}
    return {"status": "unsubscription confirmed"}
