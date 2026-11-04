"""Shared by the workflow (policy guardrail, journal drafting, fallback planner) and the agent runtime.

Pure functions only: no AWS calls, so the agent runtime stays a stateless brain
and every side effect remains a checkpointed durable step.
"""

import json
import re

ENTITIES = {"US": "USD", "DE": "EUR", "UK": "GBP"}
FX_TO_USD = {"USD": 1.0, "EUR": 1.08, "GBP": 1.27}   # fixed close rates for the demo
APPROVAL_THRESHOLD_USD = 2_500_00   # cents: above this total, a controller must sign off
RESTRICTED_ACCOUNTS = {"3000": "Share Capital", "3900": "Retained Earnings"}
SUSPENSE = "2999"                   # where unexplained items belong until investigated
RANK = {"approve": 0, "escalate": 1, "reject": 2}
ACCOUNTS = {"1000": "Cash", "1300": "Other Receivable", "6300": "Bank Fees", "7100": "FX Gain/Loss",
            "2999": "Suspense", "3900": "Retained Earnings", "3000": "Share Capital"}

SYSTEM = """You are a month-end close agent reconciling the cash account of ONE legal entity.
Call tools one per reply. Reply with ONLY one JSON object, no prose. Allowed:
{"action":"fetch_feeds","args":{"entity":"<US|DE|UK>"}}
{"action":"run_analysis","args":{"entity":"<entity>"}}   (omit "code" to use the standard pandas reconciliation, it runs in an isolated sandbox)
{"action":"draft_journals","args":{"entity":"<entity>","plugAccount":"<account for unexplained items>"}}
{"action":"final","args":{"decision":"approve|escalate|reject","reason":"<short>"}}
Order: fetch_feeds, run_analysis, draft_journals, final. Tool results arrive as TOOL_RESULT messages."""

TOOLS = {"fetch_feeds", "run_analysis", "draft_journals", "final"}

# The standard reconciliation, run by the agent in the sandbox. `inputs` maps
# "ledger"/"bank" to local CSV paths; the script fills `result`.
RECON_CODE = '''\
import csv
from collections import Counter

def load(path):
    with open(path, newline="") as f:
        return [dict(r, amount_cents=int(r["amount_cents"])) for r in csv.DictReader(f)]

ledger, bank = load(inputs["ledger"]), load(inputs["bank"])
want = Counter((r["ref"], r["amount_cents"]) for r in ledger)
have = Counter((r["ref"], r["amount_cents"]) for r in bank)
matched = sum((want & have).values())
l_rest, b_rest = want - have, have - want          # booked-only and bank-only rows
desc = {(r["ref"], r["amount_cents"]): r["description"] for r in ledger + bank}
bank_refs = {r["ref"] for r in bank}
ledger_refs = {r["ref"] for r in ledger}
items, fx_refs = [], set()
for (ref, amt), n in l_rest.items():
    twin = [a for (r, a) in b_rest if r == ref]
    if twin:   # same reference, different amount: currency conversion difference
        items.append(dict(type="fx", ref=ref, description=desc[(ref, amt)], amountCents=twin[0] - amt))
        fx_refs.add(ref)
    else:      # booked but not yet cleared by the bank
        items += [dict(type="timing", ref=ref, description=desc[(ref, amt)], amountCents=amt)] * n
for (ref, amt), n in b_rest.items():
    if ref in fx_refs:
        continue
    kind = "fee" if ref.startswith("FEE-") else "duplicate" if ref in ledger_refs else "unexplained"
    items += [dict(type=kind, ref=ref, description=desc[(ref, amt)], amountCents=amt)] * n
by_type = {t: sum(1 for i in items if i["type"] == t)
           for t in ("timing", "duplicate", "fee", "fx", "unexplained")}
result.update(matched=matched, ledgerRows=len(ledger), bankRows=len(bank),
              matchRate=round(matched / max(len(ledger), len(bank)), 3), items=items, byType=by_type)
print(f"{matched} of {len(ledger)} ledger rows matched; exceptions: {by_type}")
'''


def money(cents: int, currency: str = "USD") -> str:
    sym = {"USD": "$", "EUR": "€", "GBP": "£"}.get(currency, "")
    return f"{'-' if cents < 0 else ''}{sym}{abs(cents) / 100:,.0f}"


def to_usd_cents(cents: int, currency: str) -> int:
    return int(round(cents * FX_TO_USD[currency]))


# ---------------------------------------------------------------- journals


def draft_journals(entity: str, currency: str, items: list, plug_account: str) -> list[dict]:
    """Turn reconciliation exceptions into journal entries. Timing differences need none."""
    out = []

    def add(item, debit, credit, desc, amount):
        out.append({"id": f"JE-{entity}-{len(out) + 1:02d}", "entity": entity, "currency": currency,
                    "description": desc, "debitAccount": debit, "creditAccount": credit,
                    "amountCents": abs(int(amount)), "ref": item["ref"], "status": "PROPOSED"})

    for it in items:
        t, amt = it["type"], it["amountCents"]
        if t == "fee":
            add(it, "6300", "1000", f"Bank fee {it['ref']}: {it['description']}", amt)
        elif t == "duplicate":
            add(it, "1300", "1000", f"Duplicate bank debit {it['ref']}, claim refund", amt)
        elif t == "fx":
            gain = amt > 0
            add(it, "1000" if gain else "7100", "7100" if gain else "1000",
                f"FX difference on {it['ref']}", amt)
        elif t == "unexplained":
            add(it, "1000", str(plug_account or SUSPENSE), f"Unexplained receipt {it['ref']}: {it['description']}", amt)
    return out


def apply_policy(journals: list[dict]) -> list[dict]:
    """Hard rules: no journal may touch a restricted (equity) account."""
    for j in journals:
        hit = [a for a in (j["debitAccount"], j["creditAccount"]) if a in RESTRICTED_ACCOUNTS]
        if hit:
            j["status"] = "BLOCKED"
            j["reason"] = (f"account {hit[0]} ({RESTRICTED_ACCOUNTS[hit[0]]}) is restricted; "
                           f"book to {SUSPENSE} Suspense and investigate")
    return journals


def journals_total_usd(journals: list[dict]) -> int:
    return sum(to_usd_cents(j["amountCents"], j["currency"]) for j in journals if j["status"] != "BLOCKED")


def rule_decision(journals: list[dict]) -> tuple[str, str]:
    blocked = [j for j in journals if j["status"] == "BLOCKED"]
    total = journals_total_usd(journals)
    if blocked:
        return "escalate", f"{len(blocked)} journal(s) blocked by policy, controller must review"
    if total > APPROVAL_THRESHOLD_USD:
        return "escalate", f"journals total {money(total)}, above the {money(APPROVAL_THRESHOLD_USD)} sign-off threshold"
    return "approve", f"journals total {money(total)}, within the auto-post threshold"


# --------------------------------------------------- model + rule planner


def parse_entity(text: str) -> str:
    m = re.search(r"entity (\w+)", text)
    return m.group(1) if m else "US"


def rule_planner(messages: list) -> dict:
    ent = parse_entity(messages[0][1])
    done = {}
    for _, t in messages[1:]:
        if t.startswith("TOOL_RESULT "):
            name, _, body = t[len("TOOL_RESULT "):].partition(": ")
            done[name] = json.loads(body)
    if "fetch_feeds" not in done:
        return {"action": "fetch_feeds", "args": {"entity": ent}}
    if "run_analysis" not in done:
        return {"action": "run_analysis", "args": {"entity": ent}}
    if "draft_journals" not in done:
        # The scripted agent "plugs" unexplained receipts into retained earnings. Policy will object.
        return {"action": "draft_journals", "args": {"entity": ent, "plugAccount": "3900"}}
    return {"action": "final", "args": {"decision": "approve", "reason": (
        f"{done['run_analysis']['matched']} items matched, "
        f"{done['draft_journals']['count']} journal(s) drafted")}}


def extract_json(text: str) -> dict | None:
    m = re.search(r"\{.*\}", text, re.S)
    try:
        return json.loads(m.group(0)) if m else None
    except ValueError:
        return None
