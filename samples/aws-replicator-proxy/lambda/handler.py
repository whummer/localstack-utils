import json
import os
import boto3
from decimal import Decimal

TABLE_NAME = os.environ.get("TABLE_NAME", "product-catalog-products")

dynamodb = boto3.resource("dynamodb")


class DecimalEncoder(json.JSONEncoder):
    def default(self, o):  # noqa: ANN001
        if isinstance(o, Decimal):
            return float(o)
        return super().default(o)


CORS_HEADERS = {
    "Access-Control-Allow-Origin":  "*",
    "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
    "Access-Control-Allow-Headers": "content-type",
}


def _response(status, body):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json", **CORS_HEADERS},
        "body": json.dumps(body, cls=DecimalEncoder),
    }


def handler(event, context):
    ctx    = event.get("requestContext", {}).get("http", {})
    method = ctx.get("method") or event.get("httpMethod", "GET")
    path   = ctx.get("path") or event.get("path", "/")

    if method == "OPTIONS":
        return {"statusCode": 200, "headers": CORS_HEADERS, "body": ""}

    parts       = [p for p in path.strip("/").split("/") if p]
    is_products = parts[0:1] == ["products"]
    item_id     = parts[1] if len(parts) > 1 else None

    if not is_products:
        return _response(404, {"error": "not found"})

    table = dynamodb.Table(TABLE_NAME)

    if item_id is None:
        if method == "GET":
            result = table.scan()
            return _response(200, result.get("Items", []))

        if method == "POST":
            body = json.loads(event.get("body") or "{}", parse_float=Decimal)
            table.put_item(Item=body)
            return _response(201, {"message": "created", "item": body})

    else:
        if method == "PUT":
            body = json.loads(event.get("body") or "{}", parse_float=Decimal)
            body["id"] = item_id
            table.put_item(Item=body)
            return _response(200, {"message": "updated", "item": body})

        if method == "DELETE":
            table.delete_item(Key={"id": item_id})
            return _response(200, {"message": "deleted", "id": item_id})

    return _response(404, {"error": "not found"})
