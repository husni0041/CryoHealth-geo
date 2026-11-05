"""Verify and handle AWS SNS HTTP(S) message envelopes."""

from __future__ import annotations

import base64
import binascii
import re
from datetime import UTC, datetime
from functools import lru_cache
from urllib.parse import parse_qs, urlsplit

import requests
from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

_TOPIC_ARN_RE = re.compile(
    r"^arn:(?P<partition>aws|aws-us-gov|aws-cn):sns:"
    r"(?P<region>[a-z0-9-]+):(?P<account>\d{12}):(?P<topic>[A-Za-z0-9_-]+)$"
)
_CERT_PATH_RE = re.compile(r"^/SimpleNotificationService-[A-Za-z0-9_-]+\.pem$")
_CERT_HOST_RE = re.compile(r"^sns\.(?P<region>[a-z0-9-]+)\.amazonaws\.com(?P<cn>\.cn)?$")
_SIGNATURE_FIELDS = {
    "Notification": ("Message", "MessageId", "Subject", "Timestamp", "TopicArn", "Type"),
    "SubscriptionConfirmation": (
        "Message",
        "MessageId",
        "SubscribeURL",
        "Timestamp",
        "Token",
        "TopicArn",
        "Type",
    ),
    "UnsubscribeConfirmation": (
        "Message",
        "MessageId",
        "SubscribeURL",
        "Timestamp",
        "Token",
        "TopicArn",
        "Type",
    ),
}


class SnsWebhookError(Exception):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


def _topic_region(topic_arn: str) -> str:
    match = _TOPIC_ARN_RE.fullmatch(topic_arn)
    if not match:
        raise SnsWebhookError(503, "AWS_SNS_TOPIC_ARN is invalid")
    return match.group("region")


def _topic_partition(topic_arn: str) -> str:
    match = _TOPIC_ARN_RE.fullmatch(topic_arn)
    if not match:
        raise SnsWebhookError(503, "AWS_SNS_TOPIC_ARN is invalid")
    return match.group("partition")


def _signed_text(payload: dict, message_type: str) -> bytes:
    fields = _SIGNATURE_FIELDS.get(message_type)
    if fields is None:
        raise SnsWebhookError(400, "Unsupported AWS SNS message type")

    values: list[str] = []
    for field in fields:
        if field == "Subject" and field not in payload:
            continue
        value = payload.get(field)
        if not isinstance(value, str) or not value:
            raise SnsWebhookError(400, f"AWS SNS message is missing {field}")
        values.extend((field, value))
    return ("\n".join(values) + "\n").encode("utf-8")


def _validate_certificate_url(url: str, topic_arn: str) -> None:
    try:
        parsed = urlsplit(url)
        host_match = _CERT_HOST_RE.fullmatch(parsed.hostname or "")
        port = parsed.port
    except ValueError as exc:
        raise SnsWebhookError(400, "Invalid AWS SNS signing certificate URL") from exc

    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.query
        or parsed.fragment
        or not host_match
        or not _CERT_PATH_RE.fullmatch(parsed.path)
        or host_match.group("region") != _topic_region(topic_arn)
        or bool(host_match.group("cn")) != (_topic_partition(topic_arn) == "aws-cn")
    ):
        raise SnsWebhookError(400, "Untrusted AWS SNS signing certificate URL")


@lru_cache(maxsize=32)
def _load_signing_certificate(url: str) -> x509.Certificate:
    try:
        response = requests.get(url, timeout=5, allow_redirects=False)
        response.raise_for_status()
        if not 200 <= response.status_code < 300:
            raise requests.HTTPError("AWS SNS signing certificate request was redirected")
    except requests.RequestException as exc:
        raise SnsWebhookError(503, "Could not retrieve AWS SNS signing certificate") from exc
    try:
        certificate = x509.load_pem_x509_certificate(response.content)
    except ValueError as exc:
        raise SnsWebhookError(503, "AWS SNS signing certificate is invalid") from exc

    now = datetime.now(UTC)
    if now < certificate.not_valid_before_utc or now > certificate.not_valid_after_utc:
        raise SnsWebhookError(503, "AWS SNS signing certificate is outside its validity period")
    return certificate


def verify_message(payload: dict, expected_topic_arn: str) -> str:
    if not isinstance(payload, dict):
        raise SnsWebhookError(400, "AWS SNS request must be a JSON object")
    _topic_region(expected_topic_arn)

    message_type = payload.get("Type")
    if not isinstance(message_type, str) or message_type not in _SIGNATURE_FIELDS:
        raise SnsWebhookError(400, "Unsupported AWS SNS message type")
    if payload.get("TopicArn") != expected_topic_arn:
        raise SnsWebhookError(403, "AWS SNS topic is not allowed")

    signature_version = payload.get("SignatureVersion")
    signature_hash = {"1": hashes.SHA1(), "2": hashes.SHA256()}.get(signature_version)
    if signature_hash is None:
        raise SnsWebhookError(400, "Unsupported AWS SNS signature version")

    certificate_url = payload.get("SigningCertURL")
    signature = payload.get("Signature")
    if not isinstance(certificate_url, str) or not isinstance(signature, str):
        raise SnsWebhookError(400, "AWS SNS signature fields are missing")
    _validate_certificate_url(certificate_url, expected_topic_arn)
    signed_text = _signed_text(payload, message_type)

    try:
        signature_bytes = base64.b64decode(signature, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise SnsWebhookError(400, "AWS SNS signature is not valid base64") from exc

    certificate = _load_signing_certificate(certificate_url)
    public_key = certificate.public_key()
    if not isinstance(public_key, rsa.RSAPublicKey):
        raise SnsWebhookError(400, "AWS SNS signing certificate must contain an RSA key")

    try:
        public_key.verify(
            signature_bytes,
            signed_text,
            padding.PKCS1v15(),
            signature_hash,
        )
    except InvalidSignature as exc:
        raise SnsWebhookError(401, "AWS SNS message signature is invalid") from exc

    return message_type


def confirm_subscription(payload: dict, expected_topic_arn: str) -> None:
    url = payload.get("SubscribeURL")
    if not isinstance(url, str):
        raise SnsWebhookError(400, "AWS SNS subscription URL is missing")

    try:
        parsed = urlsplit(url)
        host_match = _CERT_HOST_RE.fullmatch(parsed.hostname or "")
        port = parsed.port
        query = parse_qs(parsed.query, strict_parsing=True)
    except ValueError as exc:
        raise SnsWebhookError(400, "Invalid AWS SNS subscription URL") from exc

    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.path != "/"
        or parsed.fragment
        or not host_match
        or host_match.group("region") != _topic_region(expected_topic_arn)
        or bool(host_match.group("cn")) != (_topic_partition(expected_topic_arn) == "aws-cn")
        or query.get("Action") != ["ConfirmSubscription"]
        or query.get("TopicArn") != [expected_topic_arn]
        or len(query.get("Token", [])) != 1
        or not query["Token"][0]
    ):
        raise SnsWebhookError(400, "Untrusted AWS SNS subscription URL")

    try:
        response = requests.get(url, timeout=5, allow_redirects=False)
        response.raise_for_status()
        if not 200 <= response.status_code < 300:
            raise requests.HTTPError("AWS SNS subscription request was redirected")
    except requests.RequestException as exc:
        raise SnsWebhookError(503, "AWS SNS subscription confirmation failed") from exc
