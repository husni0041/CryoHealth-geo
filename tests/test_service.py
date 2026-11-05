from unittest.mock import patch

from fastapi.testclient import TestClient

from pipeline.service import app

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
