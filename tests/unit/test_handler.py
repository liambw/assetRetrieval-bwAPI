import json
from unittest.mock import MagicMock

import pytest

import app


class _ResourceNotFoundException(Exception):
    pass


@pytest.fixture
def aws(monkeypatch):
    fake_table = MagicMock()
    fake_secrets = MagicMock()
    fake_secrets.exceptions.ResourceNotFoundException = _ResourceNotFoundException

    monkeypatch.setattr(app, "table", fake_table)
    monkeypatch.setattr(app, "secrets_client", fake_secrets)
    app._KEY_CACHE.clear()

    return {"table": fake_table, "secrets": fake_secrets}


def make_event(client="acme", identifier="VIN123", api_key="secret-key"):
    headers = {} if api_key is None else {"x-api-key": api_key}
    return {
        "pathParameters": {"client": client, "identifier": identifier},
        "headers": headers,
    }


def secret_payload(key="secret-key"):
    return {"SecretString": json.dumps({"key": key})}


def ddb_item(**overrides):
    item = {
        "clientIdentifier": "acme",
        "assetIdentifier": "VIN123",
        "pk": "node-1",
        "imageExterior": [{"imageTag": "Image 1", "imageUrl": "https://example.com/ext1.jpg"}],
        "imageInterior": [{"imageTag": "Image 2", "imageUrl": "https://example.com/int1.jpg"}],
        "imageManual":   [{"imageTag": "Image 3", "imageUrl": "https://example.com/man1.jpg"}],
    }
    item.update(overrides)
    return item


def test_returns_200_with_vehicle_json(aws):
    aws["secrets"].get_secret_value.return_value = secret_payload()
    aws["table"].query.return_value = {"Items": [ddb_item()]}

    response = app.lambda_handler(make_event(), None)

    assert response["statusCode"] == 200
    assert json.loads(response["body"]) == {
        "client": "acme",
        "vehicle": {
            "image_set": {"nodeid": "node-1", "VIN": "VIN123"},
            "images": {
                "exterior": [{"photo_name": "Image 1", "url": "https://example.com/ext1.jpg"}],
                "interior": [{"photo_name": "Image 2", "url": "https://example.com/int1.jpg"}],
                "manual":   [{"photo_name": "Image 3", "url": "https://example.com/man1.jpg"}],
            },
        },
    }


def test_accepts_capitalized_header_name(aws):
    aws["secrets"].get_secret_value.return_value = secret_payload()
    aws["table"].query.return_value = {"Items": [ddb_item()]}

    event = make_event()
    event["headers"] = {"X-API-Key": "secret-key"}

    response = app.lambda_handler(event, None)

    assert response["statusCode"] == 200


def test_returns_first_item_when_query_returns_multiple(aws):
    aws["secrets"].get_secret_value.return_value = secret_payload()
    aws["table"].query.return_value = {
        "Items": [ddb_item(pk="node-1"), ddb_item(pk="node-2")],
    }

    response = app.lambda_handler(make_event(), None)
    body = json.loads(response["body"])

    assert response["statusCode"] == 200
    assert body["vehicle"]["image_set"]["nodeid"] == "node-1"


def test_returns_401_when_api_key_header_missing(aws):
    aws["secrets"].get_secret_value.return_value = secret_payload()

    response = app.lambda_handler(make_event(api_key=None), None)

    assert response["statusCode"] == 401
    assert json.loads(response["body"]) == {"message": "Unauthorized"}


def test_returns_401_when_api_key_does_not_match(aws):
    aws["secrets"].get_secret_value.return_value = secret_payload(key="real-key")

    response = app.lambda_handler(make_event(api_key="wrong-key"), None)

    assert response["statusCode"] == 401


def test_returns_401_when_client_has_no_secret(aws):
    aws["secrets"].get_secret_value.side_effect = _ResourceNotFoundException()

    response = app.lambda_handler(make_event(), None)

    assert response["statusCode"] == 401


def test_returns_403_when_no_matching_vehicle(aws):
    aws["secrets"].get_secret_value.return_value = secret_payload()
    aws["table"].query.return_value = {"Items": []}

    response = app.lambda_handler(make_event(), None)

    assert response["statusCode"] == 403
    assert json.loads(response["body"]) == {"message": "Vehicle not found"}


def test_returns_500_when_event_is_malformed(aws):
    response = app.lambda_handler({"headers": {"x-api-key": "x"}}, None)

    assert response["statusCode"] == 500
    assert json.loads(response["body"]) == {"message": "Internal server error"}


def test_api_key_cache_avoids_repeat_secrets_calls(aws):
    aws["secrets"].get_secret_value.return_value = secret_payload()
    aws["table"].query.return_value = {"Items": [ddb_item()]}

    app.lambda_handler(make_event(), None)
    app.lambda_handler(make_event(), None)

    assert aws["secrets"].get_secret_value.call_count == 1
