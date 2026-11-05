#!/usr/bin/env python3
"""Seed (or reset) the demo data: bank and ledger files per entity, chaos flags, empty run tables.

Deterministic on purpose, so every take of the demo is identical. Planted discrepancies:

  US  2 timing differences, 1 bank fee, 1 duplicate bank debit
  DE  1 timing, 1 bank fee, 1 FX conversion difference, 1 unexplained wire (the agent will try to plug it
      into Retained Earnings and policy will block that). The DE bank feed is HELD BACK in staging/:
      the close waits for it until someone delivers it.
  UK  2 timing differences, 1 bank fee

Usage: seed.py [--period 2026-10]   (AWS_ENDPOINT_URL selects the LocalStack instance)
       DEMO_THINK_SECONDS=0 seed.py  turns off the simulated agent thinking time (default 2.5 s per agent turn)
"""

import csv
import io
import os
import random
import sys

import boto3

BUCKET = "close-documents"
PERIOD = sys.argv[sys.argv.index("--period") + 1] if "--period" in sys.argv else "2026-10"

ROWS = {"US": 40, "DE": 36, "UK": 30}
VENDORS = ["Initech", "Globex", "Umbrella Ltd", "Stark Supplies", "Wayne Logistics", "Hooli Cloud", "Soylent Foods"]


def base_rows(entity: str, n: int) -> list[dict]:
    rnd = random.Random(f"northwind-{entity}-{PERIOD}")
    rows = []
    for i in range(1, n + 1):
        amount = rnd.randrange(8_000, 640_000, 50) * rnd.choice([1, 1, -1])
        rows.append({"ref": f"INV-{entity}-{i:03d}", "date": f"{PERIOD}-{rnd.randint(1, 28):02d}",
                     "description": f"{rnd.choice(VENDORS)} {'receipt' if amount > 0 else 'payment'}",
                     "amount_cents": amount})
    return rows


def build(entity: str) -> tuple[list[dict], list[dict]]:
    ledger = base_rows(entity, ROWS[entity])
    bank = [dict(r) for r in ledger]
    extra_ledger, extra_bank = [], []

    def ledger_only(ref, desc, amount):   # booked, not yet cleared by the bank
        extra_ledger.append({"ref": ref, "date": f"{PERIOD}-30", "description": desc, "amount_cents": amount})

    def bank_only(ref, desc, amount):
        extra_bank.append({"ref": ref, "date": f"{PERIOD}-29", "description": desc, "amount_cents": amount})

    if entity == "US":
        ledger_only("CHK-US-01", "Cheque 20418 Globex", -184_500)
        ledger_only("CHK-US-02", "Cheque 20419 Initech", -92_000)
        bank_only("FEE-US-01", "MONTHLY ACCOUNT FEE", -4_500)
        bank[6]["amount_cents"] = ledger[6]["amount_cents"] = -420_000   # INV-US-007: the one the bank debits twice
        extra_bank.append(dict(bank[6]))                                  # duplicate bank debit
    elif entity == "DE":
        ledger_only("CHK-DE-01", "Überweisung Hooli Cloud", -310_000)
        bank_only("FEE-DE-01", "KONTOFÜHRUNGSGEBÜHR", -2_800)
        bank[11]["amount_cents"] = 1_262_500                              # INV-DE-012: ledger booked at 12,500.00
        ledger[11]["amount_cents"] = 1_250_000
        bank_only("WIRE-DE-UNK", "INCOMING WIRE REF UNKNOWN", 1_845_000)
    else:
        ledger_only("CHK-UK-01", "Cheque 8841 Wayne Logistics", -64_200)
        ledger_only("CHK-UK-02", "Cheque 8842 Stark Supplies", -118_900)
        bank_only("FEE-UK-01", "ACCOUNT MAINTENANCE", -1_900)
    return ledger + extra_ledger, bank + extra_bank


def to_csv(rows: list[dict]) -> bytes:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=["ref", "date", "description", "amount_cents"])
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue().encode()


def clear_table(ddb, table: str, key: str) -> None:
    for item in ddb.scan(TableName=table, ProjectionExpression="#k", ExpressionAttributeNames={"#k": key})["Items"]:
        ddb.delete_item(TableName=table, Key={key: item[key]})


def stop_running_closes() -> int:
    """Executions still waiting for a feed or sign-off would otherwise outlive the rows we are about to delete."""
    lam, stopped = boto3.client("lambda"), 0
    try:
        for page in lam.get_paginator("list_durable_executions_by_function").paginate(
                FunctionName="close-workflow", Statuses=["RUNNING"]):
            for ex in page.get("DurableExecutions", []):
                lam.stop_durable_execution(DurableExecutionArn=ex["DurableExecutionArn"])
                stopped += 1
    except Exception as exc:  # first deploy: nothing to stop yet
        print(f"(could not stop earlier closes: {type(exc).__name__})")
    return stopped


def main(think_seconds: float | None = None) -> None:
    s3, ddb = boto3.client("s3"), boto3.client("dynamodb")
    stopped = stop_running_closes()
    if stopped:
        print(f"stopped {stopped} running close execution(s) from earlier runs")
    # remove earlier feeds so a reset really starts from scratch
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=BUCKET, Prefix="feeds/"):
        for obj in page.get("Contents", []):
            s3.delete_object(Bucket=BUCKET, Key=obj["Key"])
    for entity in ROWS:
        ledger, bank = build(entity)
        s3.put_object(Bucket=BUCKET, Key=f"feeds/{PERIOD}/{entity}/ledger.csv", Body=to_csv(ledger))
        held = entity == "DE"
        s3.put_object(Bucket=BUCKET, Key=f"{'staging' if held else 'feeds'}/{PERIOD}/{entity}/bank.csv", Body=to_csv(bank))
    clear_table(ddb, "close-runs", "closeId")
    clear_table(ddb, "close-postings", "journalId")
    ddb.put_item(TableName="close-config", Item={
        "key": {"S": "chaos"}, "killSandbox": {"N": "0"}, "erpFailures": {"N": "0"}, "erpDown": {"BOOL": False}})
    think = think_seconds if think_seconds is not None else float(os.environ.get("DEMO_THINK_SECONDS", "2.5"))
    ddb.put_item(TableName="close-config", Item={"key": {"S": "demo"}, "thinkSeconds": {"N": str(think)}})
    print(f"seeded period {PERIOD}: feeds for US, DE, UK (DE bank feed held in staging/), agent think time {think}s")


if __name__ == "__main__":
    main()
