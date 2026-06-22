import json
import boto3
import hmac
import time
from enum import Enum
import logging
from boto3.dynamodb.types import TypeDeserializer


logger = logging.getLogger()
logger.setLevel(logging.INFO)

# setup dynamodb connection
dynamo_client = boto3.client('dynamodb')
dynamodb = boto3.resource('dynamodb')
table = dynamodb.Table('bw-ums-master')
tableName = 'bw-ums-master'

# setup secrets manager + per-client key cache (survives across warm invocations)
secrets_client = boto3.client('secretsmanager')
_KEY_CACHE = {}
_KEY_CACHE_TTL_SECONDS = 300


def get_client_api_key(client_id: str):
    '''Fetch the API key for a client from Secrets Manager, with a TTL cache.
    Returns None if no secret exists for this client.'''
    now = time.time()
    cached = _KEY_CACHE.get(client_id)
    if cached and now - cached[1] < _KEY_CACHE_TTL_SECONDS:
        return cached[0]

    secret_name = f"{client_id}_bw_api_key"
    try:
        resp = secrets_client.get_secret_value(SecretId=secret_name)
    except secrets_client.exceptions.ResourceNotFoundException:
        return None

    key = json.loads(resp['SecretString'])['key']
    _KEY_CACHE[client_id] = (key, now)
    return key


def get_header(headers, name):
    if not headers:
        return None
    name_lower = name.lower()
    for k, v in headers.items():
        if k.lower() == name_lower:
            return v
    return None


def is_authorized(client_id: str, provided_key) -> bool:
    if not provided_key:
        return False
    expected = get_client_api_key(client_id)
    if not expected:
        return False
    return hmac.compare_digest(expected, provided_key)


def ddb_deserialize(data, type_deserializer = TypeDeserializer()):
    if isinstance(data, list):
        return [ddb_deserialize(v) for v in data]

    if isinstance(data, dict):
        try:
            return type_deserializer.deserialize(data)
        except TypeError:
            return {k: ddb_deserialize(v) for k, v in data.items()}
    else:
        return data
    

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
                "nodeid": vehicle['pk'],
                "VIN": vehicle['assetIdentifier'],
            },
            "images": images_data,
        },
    }


def query_for_vehicles(client: str, identifier: str):
    try:
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
    except Exception as ex:
        logger.error(ex)


def make_response(status_code: int, body: dict) -> dict:
    return {
        "statusCode": status_code,
        "body": json.dumps(body),
    }


def error_response(status_code: int, message: str) -> dict:
    return make_response(status_code, {"message": message})


def lambda_handler(event, context):
    try:
        path_params = event['pathParameters']
        identifier = path_params['identifier']
        client = path_params['client']

        provided_key = get_header(event.get('headers'), 'x-api-key')
        if not is_authorized(client, provided_key):
            return error_response(401, "Unauthorized")

        items = query_for_vehicles(client, identifier)
        if not items:
            return error_response(403, "Vehicle not found")

        return make_response(200, create_vehicle_result(items[0]))
    except Exception as ex:
        logger.error(str(ex))
        return error_response(500, "Internal server error")
