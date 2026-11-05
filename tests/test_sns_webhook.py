from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock, patch

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID

from pipeline.sns_webhook import (
    SnsWebhookError,
    _load_signing_certificate,
    _signed_text,
    confirm_subscription,
    verify_message,
)

TOPIC_ARN = "arn:aws:sns:us-east-1:123456789012:sentinel-scenes"
CERT_URL = "https://sns.us-east-1.amazonaws.com/SimpleNotificationService-abc123.pem"


@pytest.fixture
def signing_material():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "SNS test signer")])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .sign(private_key, hashes.SHA256())
    )
    return private_key, certificate


def _payload(private_key, message_type="Notification"):
    payload = {
        "Type": message_type,
        "MessageId": "message-123",
        "Message": '{"collection":"sentinel-2-l2a"}',
        "Timestamp": "2026-11-05T05:00:00.000Z",
        "TopicArn": TOPIC_ARN,
        "SignatureVersion": "2",
        "SigningCertURL": CERT_URL,
    }
    if message_type == "SubscriptionConfirmation":
        payload["SubscribeURL"] = (
            "https://sns.us-east-1.amazonaws.com/?Action=ConfirmSubscription"
            "&TopicArn=arn%3Aaws%3Asns%3Aus-east-1%3A123456789012%3Asentinel-scenes&Token=token"
        )
        payload["Token"] = "token"
    payload["Signature"] = base64.b64encode(
        private_key.sign(_signed_text(payload, message_type), padding.PKCS1v15(), hashes.SHA256())
    ).decode("ascii")
    return payload


def test_verify_message_accepts_a_valid_signed_notification(signing_material):
    private_key, certificate = signing_material
    payload = _payload(private_key)
    _load_signing_certificate.cache_clear()

    with patch("pipeline.sns_webhook._load_signing_certificate", return_value=certificate):
        assert verify_message(payload, TOPIC_ARN) == "Notification"


def test_verify_message_rejects_a_modified_message(signing_material):
    private_key, certificate = signing_material
    payload = _payload(private_key)
    payload["Message"] = '{"collection":"untrusted"}'

    with patch("pipeline.sns_webhook._load_signing_certificate", return_value=certificate):
        with pytest.raises(SnsWebhookError, match="signature is invalid") as error:
            verify_message(payload, TOPIC_ARN)

    assert error.value.status_code == 401


def test_verify_message_rejects_a_different_topic_before_fetching_certificate():
    payload = {
        "Type": "Notification",
        "TopicArn": "arn:aws:sns:us-east-1:123456789012:other-topic",
    }
    with patch("pipeline.sns_webhook._load_signing_certificate") as load_certificate:
        with pytest.raises(SnsWebhookError) as error:
            verify_message(payload, TOPIC_ARN)

    assert error.value.status_code == 403
    load_certificate.assert_not_called()


@pytest.mark.parametrize(
    "certificate_url",
    [
        "http://sns.us-east-1.amazonaws.com/SimpleNotificationService-a.pem",
        "https://attacker.example/SimpleNotificationService-a.pem",
        "https://sns.us-west-2.amazonaws.com/SimpleNotificationService-a.pem",
        "https://sns.us-east-1.amazonaws.com/redirect?next=https://attacker.example",
        "https://user@sns.us-east-1.amazonaws.com/SimpleNotificationService-a.pem",
    ],
)
def test_verify_message_rejects_untrusted_certificate_urls(signing_material, certificate_url):
    private_key, _ = signing_material
    payload = _payload(private_key)
    payload["SigningCertURL"] = certificate_url

    with patch("pipeline.sns_webhook._load_signing_certificate") as load_certificate:
        with pytest.raises(SnsWebhookError) as error:
            verify_message(payload, TOPIC_ARN)

    assert error.value.status_code == 400
    load_certificate.assert_not_called()


def test_confirm_subscription_only_fetches_a_validated_topic_url():
    payload = {
        "SubscribeURL": (
            "https://sns.us-east-1.amazonaws.com/?Action=ConfirmSubscription"
            "&TopicArn=arn%3Aaws%3Asns%3Aus-east-1%3A123456789012%3Asentinel-scenes&Token=token"
        )
    }
    response = Mock(status_code=200)
    with patch("pipeline.sns_webhook.requests.get", return_value=response) as get:
        confirm_subscription(payload, TOPIC_ARN)

    get.assert_called_once_with(payload["SubscribeURL"], timeout=5, allow_redirects=False)
    response.raise_for_status.assert_called_once()


def test_confirm_subscription_rejects_external_url():
    with patch("pipeline.sns_webhook.requests.get") as get:
        with pytest.raises(SnsWebhookError):
            confirm_subscription({"SubscribeURL": "https://attacker.example/"}, TOPIC_ARN)

    get.assert_not_called()
