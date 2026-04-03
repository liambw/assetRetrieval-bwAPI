import json
import boto3
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


def create_response(status_code: int, message: str = None) -> dict:
    '''Creates a response dict for the lambda function to return.'''
    response = {
        "statusCode": status_code,
        "body": {}
    }

    if message:
        response["body"]["message"] = message

    return response


def lambda_handler(event, context):
    try:
        path_params = event['pathParameters']
        identifier = path_params['identifier']
        client = path_params['client']

        response = query_for_vehicles(client, identifier)
        result = create_vehicle_result(response)
    except Exception as ex:
        logger.error(str(ex))
