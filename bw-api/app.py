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
    

def create_vehicle_result(found_vehicles: list):
    try:
        result_list = []

        for vehicle in found_vehicles:
            image_indexes = ['imageExterior', 'imageInterior', 'imageManual']
            result_indexes = ['exterior', 'interior', 'manual']
            images_data = {}

            for idx, image_index in enumerate(image_indexes):
                for image in vehicle[image_index]:
                    if image_index in images_data:
                        images_data[result_indexes[idx]].append({
                            'photo_name': image['imageTag'],
                            'url': image['imageUrl']
                        })
                    else:
                        images_data[result_indexes[idx]] = [{
                            'photo_name': image['imageTag'],
                            'url': image['imageUrl']
                        }]

            result_entry = {
                "client": vehicle['clientIdentifier'],
                "rooftop": "NEEDED",
                "vehicle": {
                    "image_set": {
                        "capture_dts_UTC": "NEEDED",
                        "nodeid": vehicle['pk'],
                        "VIN": vehicle['assetIdentifier']
                    }
                },
                "images": images_data
            }

            result_list.append(result_entry)
        logger.error(result_list)
        return result_list
    except Exception as ex:
        logger.error(str(ex))


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


def create_response(status_code: int, message: str = None, data=None) -> dict:
    '''Creates a response dict for the lambda function to return.'''
    body = {}
    if message:
        body["message"] = message
    if data is not None:
        body["data"] = data

    return {
        "statusCode": status_code,
        "body": json.dumps(body)
    }


def lambda_handler(event, context):
    try:
        path_params = event['pathParameters']
        identifier = path_params['identifier']
        client = path_params['client']

        provided_key = get_header(event.get('headers'), 'x-api-key')
        if not is_authorized(client, provided_key):
            return create_response(401, "Unauthorized")

        items = query_for_vehicles(client, identifier)
        result = create_vehicle_result(items)
        return create_response(200, data=result)
    except Exception as ex:
        logger.error(str(ex))
        return create_response(500, "Internal server error")
