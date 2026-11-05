from unittest.mock import patch

from fastapi.testclient import TestClient

from pipeline import service
from pipeline.service import app
from pipeline.sns_webhook import SnsWebhookError

TOPIC_ARN = "arn:aws:sns:us-east-1:123456789012:sentinel-scenes"


def test_stac_webhook_is_disabled_without_a_configured_topic(monkeypatch):
    monkeypatch.delenv("AWS_SNS_TOPIC_ARN", raising=False)

    with TestClient(app) as client:
        response = client.post("/webhooks/stac-event", json={})

    assert response.status_code == 503


def test_signed_notification_queues_the_existing_daily_pipeline(monkeypatch):
    monkeypatch.setenv("AWS_SNS_TOPIC_ARN", TOPIC_ARN)
    with (
        patch("pipeline.service.verify_message", return_value="Notification"),
        patch("pipeline.service._daily_job") as daily_job,
        TestClient(app) as client,
    ):
        response = client.post("/webhooks/stac-event", json={"Type": "Notification"})

    assert response.status_code == 202
    assert response.json() == {"status": "accepted"}
    daily_job.assert_called_once_with()


def test_subscription_confirmation_does_not_queue_pipeline(monkeypatch):
    monkeypatch.setenv("AWS_SNS_TOPIC_ARN", TOPIC_ARN)
    with (
        patch("pipeline.service.verify_message", return_value="SubscriptionConfirmation"),
        patch("pipeline.service.confirm_subscription") as confirm,
        patch("pipeline.service._daily_job") as daily_job,
        TestClient(app) as client,
    ):
        response = client.post("/webhooks/stac-event", json={"Type": "SubscriptionConfirmation"})

    assert response.status_code == 202
    assert response.json() == {"status": "subscription confirmed"}
    confirm.assert_called_once()
    daily_job.assert_not_called()


def test_unsubscribe_confirmation_does_not_queue_pipeline(monkeypatch):
    monkeypatch.setenv("AWS_SNS_TOPIC_ARN", TOPIC_ARN)
    with (
        patch("pipeline.service.verify_message", return_value="UnsubscribeConfirmation"),
        patch("pipeline.service._daily_job") as daily_job,
        TestClient(app) as client,
    ):
        response = client.post("/webhooks/stac-event", json={"Type": "UnsubscribeConfirmation"})

    assert response.status_code == 202
    assert response.json() == {"status": "unsubscription confirmed"}
    daily_job.assert_not_called()


def test_sns_text_plain_content_type_is_accepted(monkeypatch):
    # AWS SNS delivers its JSON envelope as text/plain, not application/json.
    monkeypatch.setenv("AWS_SNS_TOPIC_ARN", TOPIC_ARN)
    with (
        patch("pipeline.service.verify_message", return_value="Notification"),
        patch("pipeline.service._daily_job") as daily_job,
        TestClient(app) as client,
    ):
        response = client.post(
            "/webhooks/stac-event",
            content='{"Type": "Notification"}',
            headers={"Content-Type": "text/plain; charset=UTF-8"},
        )

    assert response.status_code == 202
    daily_job.assert_called_once_with()


def test_malformed_json_body_is_rejected_with_400(monkeypatch):
    monkeypatch.setenv("AWS_SNS_TOPIC_ARN", TOPIC_ARN)
    with patch("pipeline.service._daily_job") as daily_job, TestClient(app) as client:
        response = client.post("/webhooks/stac-event", content="not json")

    assert response.status_code == 400
    daily_job.assert_not_called()


def test_invalid_signature_is_rejected_with_401_and_queues_nothing(monkeypatch):
    monkeypatch.setenv("AWS_SNS_TOPIC_ARN", TOPIC_ARN)
    with (
        patch(
            "pipeline.service.verify_message",
            side_effect=SnsWebhookError(401, "AWS SNS message signature is invalid"),
        ),
        patch("pipeline.service._daily_job") as daily_job,
        TestClient(app) as client,
    ):
        response = client.post("/webhooks/stac-event", json={"Type": "Notification"})

    assert response.status_code == 401
    daily_job.assert_not_called()


def test_event_job_coalesces_notifications_that_arrive_mid_run():
    calls = 0

    def fake_daily_job():
        nonlocal calls
        calls += 1
        if calls == 1:
            # Three more notifications land while the first pass is running.
            service._event_job()
            service._event_job()
            service._event_job()

    with patch("pipeline.service._daily_job", side_effect=fake_daily_job):
        service._event_job()

    # The first pass plus exactly one queued follow-up, not one pass per notification.
    assert calls == 2
    assert service._event_running is False


def test_event_job_recovers_after_a_failed_pass():
    with patch("pipeline.service._daily_job", side_effect=RuntimeError("boom")):
        try:
            service._event_job()
        except RuntimeError:
            pass

    with patch("pipeline.service._daily_job") as daily_job:
        service._event_job()

    daily_job.assert_called_once_with()
