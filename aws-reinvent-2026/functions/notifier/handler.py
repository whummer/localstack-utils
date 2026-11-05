"""SQS-triggered Lambda: turns SNS notifications into a fake email to the finance team."""

import json
import os
import time

import boto3

ddb = boto3.client("dynamodb")


def handler(event, context):
    for record in event["Records"]:
        msg = json.loads(json.loads(record["body"])["Message"])  # SQS body = SNS envelope
        text = f"Email to finance-close@northwind.example: {msg['text']}"
        ddb.update_item(
            TableName=os.environ["RUNS_TABLE"],
            Key={"closeId": {"S": msg["closeId"]}},
            UpdateExpression="SET notifications = list_append(if_not_exists(notifications, :e), :n)",
            ExpressionAttributeValues={
                ":e": {"L": []},
                ":n": {"L": [{"M": {"text": {"S": text}, "at": {"N": str(int(time.time() * 1000))}}}]},
            },
        )
    return {"processed": len(event["Records"])}
