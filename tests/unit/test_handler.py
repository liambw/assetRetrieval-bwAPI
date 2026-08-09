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
    app._CREDENTIAL_CACHE.clear()

    return {"table": fake_table, "secrets": fake_secrets}


def make_event(account="brinks", identifier="VIN123", api_key="secret-key"):
    headers = {}
    if account is not None:
        headers["x-client-id"] = account
    if api_key is not None:
        headers["x-api-key"] = api_key
    return {
        "pathParameters": {"identifier": identifier},
        "headers": headers,
    }


def secret_payload(key="secret-key", clients=("BRNKS01", "BRNKS02")):
    secret = {"key": key}
    if clients is not None:
        secret["clients"] = list(clients)
    return {"SecretString": json.dumps(secret)}


def ddb_item(**overrides):
    item = {
        "clientIdentifier": "BRNKS01",
        "assetIdentifier": "VIN123",
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
        "client": "BRNKS01",
        "vehicle": {
            "image_set": {"VIN": "VIN123"},
            "images": {
                "exterior": [{"photo_name": "Image 1", "url": "https://example.com/ext1.jpg"}],
                "interior": [{"photo_name": "Image 2", "url": "https://example.com/int1.jpg"}],
                "manual":   [{"photo_name": "Image 3", "url": "https://example.com/man1.jpg"}],
            },
        },
    }


def test_accepts_capitalized_header_names(aws):
    aws["secrets"].get_secret_value.return_value = secret_payload()
    aws["table"].query.return_value = {"Items": [ddb_item()]}

    event = make_event()
    event["headers"] = {"X-Client-Id": "brinks", "X-API-Key": "secret-key"}

    response = app.lambda_handler(event, None)

    assert response["statusCode"] == 200


def test_falls_back_to_second_client_id(aws):
    '''BRNKS01 has no match, so the lookup moves on to BRNKS02.'''
    aws["secrets"].get_secret_value.return_value = secret_payload()
    aws["table"].query.side_effect = [
        {"Items": []},
        {"Items": [ddb_item(clientIdentifier="BRNKS02")]},
    ]

    response = app.lambda_handler(make_event(), None)

    assert response["statusCode"] == 200
    assert json.loads(response["body"])["client"] == "BRNKS02"
    assert aws["table"].query.call_count == 2


def test_stops_at_first_matching_client_id(aws):
    aws["secrets"].get_secret_value.return_value = secret_payload()
    aws["table"].query.return_value = {"Items": [ddb_item()]}

    app.lambda_handler(make_event(), None)

    assert aws["table"].query.call_count == 1


def test_defaults_clients_to_account_when_secret_has_no_list(aws):
    aws["secrets"].get_secret_value.return_value = secret_payload(clients=None)
    aws["table"].query.return_value = {"Items": [ddb_item(clientIdentifier="brinks")]}

    response = app.lambda_handler(make_event(), None)

    assert response["statusCode"] == 200
    _, kwargs = aws["table"].query.call_args
    assert kwargs["ExpressionAttributeValues"][":entered_client"] == "brinks"


def test_returns_first_item_when_query_returns_multiple(aws):
    aws["secrets"].get_secret_value.return_value = secret_payload()
    aws["table"].query.return_value = {
        "Items": [ddb_item(assetIdentifier="VIN123"), ddb_item(assetIdentifier="VIN999")],
    }

    response = app.lambda_handler(make_event(), None)
    body = json.loads(response["body"])

    assert response["statusCode"] == 200
    assert body["vehicle"]["image_set"]["VIN"] == "VIN123"


def test_returns_401_when_api_key_header_missing(aws):
    aws["secrets"].get_secret_value.return_value = secret_payload()

    response = app.lambda_handler(make_event(api_key=None), None)

    assert response["statusCode"] == 401
    assert json.loads(response["body"]) == {"message": "Unauthorized"}


def test_returns_401_when_client_id_header_missing(aws):
    aws["secrets"].get_secret_value.return_value = secret_payload()

    response = app.lambda_handler(make_event(account=None), None)

    assert response["statusCode"] == 401


def test_returns_401_when_api_key_does_not_match(aws):
    aws["secrets"].get_secret_value.return_value = secret_payload(key="real-key")

    response = app.lambda_handler(make_event(api_key="wrong-key"), None)

    assert response["statusCode"] == 401


def test_returns_401_when_account_has_no_secret(aws):
    aws["secrets"].get_secret_value.side_effect = _ResourceNotFoundException()

    response = app.lambda_handler(make_event(), None)

    assert response["statusCode"] == 401


def test_cannot_read_another_accounts_clients(aws):
    '''A valid brinks key does not grant access to a client id outside its list.'''
    aws["secrets"].get_secret_value.return_value = secret_payload()
    aws["table"].query.return_value = {"Items": []}

    response = app.lambda_handler(make_event(), None)

    assert response["statusCode"] == 404
    queried = [
        kwargs["ExpressionAttributeValues"][":entered_client"]
        for _, kwargs in aws["table"].query.call_args_list
    ]
    assert queried == ["BRNKS01", "BRNKS02"]


def test_returns_404_when_no_matching_vehicle(aws):
    aws["secrets"].get_secret_value.return_value = secret_payload()
    aws["table"].query.return_value = {"Items": []}

    response = app.lambda_handler(make_event(), None)

    assert response["statusCode"] == 404
    assert json.loads(response["body"]) == {"message": "Vehicle not found"}


def test_returns_500_when_event_is_malformed(aws):
    response = app.lambda_handler({"headers": {"x-api-key": "x"}}, None)

    assert response["statusCode"] == 500
    assert json.loads(response["body"]) == {"message": "Internal server error"}


def test_credential_cache_avoids_repeat_secrets_calls(aws):
    aws["secrets"].get_secret_value.return_value = secret_payload()
    aws["table"].query.return_value = {"Items": [ddb_item()]}

    app.lambda_handler(make_event(), None)
    app.lambda_handler(make_event(), None)

    assert aws["secrets"].get_secret_value.call_count == 1
