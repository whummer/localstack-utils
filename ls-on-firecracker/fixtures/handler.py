import boto3

# LocalStack injects AWS_ENDPOINT_URL into the Lambda execution environment
# automatically, and boto3 has respected that variable since 1.28.0 -- no
# endpoint_url override needed here, on LocalStack or on real AWS.
s3 = boto3.client("s3")


def handler(event, context):
    name = event.get("name", "world")
    bucket = event.get("bucket")

    if bucket:
        s3.create_bucket(Bucket=bucket)

    buckets = [b["Name"] for b in s3.list_buckets()["Buckets"]]
    return {"message": f"hello {name}", "buckets": buckets}
