import json
import boto3
import hmac
import time
import logging


logger = logging.getLogger()
logger.setLevel(logging.INFO)

# setup dynamodb connection
dynamodb = boto3.resource('dynamodb')
table = dynamodb.Table('bw-ums-master')
tableName = 'bw-ums-master'

# setup secrets manager + per-account credential cache (survives across warm invocations)
secrets_client = boto3.client('secretsmanager')
_CREDENTIAL_CACHE = {}
_CREDENTIAL_TTL_SECONDS = 300


def get_credential(account: str):
    '''Fetch an account's API key and the client identifiers it may read.

    The secret is named "{account}_bw_api_key" and looks like:
        {"key": "...", "clients": ["BRNKS01", "BRNKS02"]}

    One account can cover several clientIdentifier values in DynamoDB, which is
    how a single customer with multiple client ids uses one credential. Each
    account has its own secret so one can be revoked (rotate or delete their
    secret) without affecting anyone else. Returns None if no secret exists.'''
    now = time.time()
    cached = _CREDENTIAL_CACHE.get(account)
    if cached and now - cached[1] < _CREDENTIAL_TTL_SECONDS:
        return cached[0]

    secret_name = f"{account}_bw_api_key"
    try:
        resp = secrets_client.get_secret_value(SecretId=secret_name)
    except secrets_client.exceptions.ResourceNotFoundException:
        return None

    secret = json.loads(resp['SecretString'])
    credential = {
        'key': secret['key'],
        'clients': secret.get('clients') or [account],
    }
    _CREDENTIAL_CACHE[account] = (credential, now)
    return credential


def get_header(headers, name):
    if not headers:
        return None
    name_lower = name.lower()
    for k, v in headers.items():
        if k.lower() == name_lower:
            return v
    return None


def authorized_clients(headers):
    '''Authenticate the caller and return the client identifiers they may read.
    Returns None if the account or key is missing, unknown, or wrong.

    The account header only selects which secret to check against; trust comes
    from the presented key matching THAT account's key. A caller who claims
    someone else's account still has to produce that account's key.'''
    account = get_header(headers, 'x-client-id')
    presented_key = get_header(headers, 'x-api-key')
    if not account or not presented_key:
        return None

    credential = get_credential(account)
    if not credential:
        return None

    if not hmac.compare_digest(credential['key'], presented_key):
        return None

    return credential['clients']


def create_vehicle_result(vehicle: dict):
    image_indexes = ['imageExterior', 'imageInterior', 'imageManual']
    result_indexes = ['exterior', 'interior', 'manual']
    images_data = {}

    for idx, image_index in enumerate(image_indexes):
        for image in vehicle.get(image_index, []):
            images_data.setdefault(result_indexes[idx], []).append({
                'photo_name': image['imageTag'],
                'url': image['imageUrl'],
            })

    return {
        "client": vehicle['clientIdentifier'],
        "vehicle": {
            "image_set": {
                "VIN": vehicle['assetIdentifier'],
            },
            "images": images_data,
        },
    }


def query_for_vehicles(client: str, identifier: str):
    query_response = table.query(
        TableName=tableName,
        IndexName='assetIdentifier-clientIdentifier-index',
        KeyConditionExpression='assetIdentifier = :entered_identifier AND clientIdentifier = :entered_client',
        ExpressionAttributeValues={
            ':entered_identifier': identifier,
            ':entered_client': client
        },
    )

    return query_response['Items']


def find_vehicle(clients, identifier: str):
    '''Look for the asset under each client identifier the caller may read,
    stopping at the first match. Returns None if none of them have it.'''
    for client in clients:
        items = query_for_vehicles(client, identifier)
        if items:
            return items[0]
    return None


def make_response(status_code: int, body: dict) -> dict:
    return {
        "statusCode": status_code,
        "body": json.dumps(body),
    }


def error_response(status_code: int, message: str) -> dict:
    return make_response(status_code, {"message": message})


def lambda_handler(event, context):
    try:
        identifier = event['pathParameters']['identifier']

        clients = authorized_clients(event.get('headers'))
        if not clients:
            return error_response(401, "Unauthorized")

        vehicle = find_vehicle(clients, identifier)
        if not vehicle:
            return error_response(404, "Vehicle not found")

        return make_response(200, create_vehicle_result(vehicle))
    except Exception as ex:
        logger.error(str(ex))
        return error_response(500, "Internal server error")
