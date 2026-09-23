"""What this project has actually cost: plan, credits, month to date, by service.

    uv run python scripts/cost_report.py                 # `make cost`
    uv run python scripts/cost_report.py --month 2026-10 # a full past month
    uv run python scripts/cost_report.py --activate-tag  # cost-allocation tag
    uv run python scripts/cost_report.py --json out.json # evidence for docs/cost.md

Three numbers are kept apart, because they answer different questions:

    usage   what the resources would cost at list price after AWS's own
            free allowances (Cost Explorer UnblendedCost, RECORD_TYPE=Usage)
    credits what the Free plan's credits paid (RECORD_TYPE=Credit, negative)
    net     what is actually charged to the account (NetUnblendedCost)

Filtered to tag project=nyc-taxi-trip-duration when that cost-allocation tag
is active; otherwise account-wide, and the report says so (this account holds
nothing else, but the filter is the claim the ledger makes). Cost Explorer
has no data for roughly a day after an account's first usage; that is
reported as such, never as $0.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, date, datetime, timedelta
from typing import Any

import boto3
from botocore.exceptions import ClientError

TAG_KEY, TAG_VALUE = "project", "nyc-taxi-trip-duration"


def month_bounds(month: str | None, today: date) -> tuple[str, str, bool]:
    """[start, end) for Cost Explorer, and whether the month is complete."""
    if month is None:
        start = today.replace(day=1)
        return start.isoformat(), (today + timedelta(days=1)).isoformat(), False
    y, m = (int(x) for x in month.split("-"))
    start = date(y, m, 1)
    nxt = date(y + (m == 12), m % 12 + 1, 1)
    return start.isoformat(), nxt.isoformat(), nxt <= today


def tag_status(ce: Any) -> str:
    tags = ce.list_cost_allocation_tags(TagKeys=[TAG_KEY]).get("CostAllocationTags", [])
    return tags[0]["Status"] if tags else "not yet seen by billing"


def summarise(groups: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for g in groups:
        service, record = g["Keys"]
        row = out.setdefault(service, {"usage": 0.0, "credits": 0.0, "net": 0.0})
        unblended = float(g["Metrics"]["UnblendedCost"]["Amount"])
        net = float(g["Metrics"]["NetUnblendedCost"]["Amount"])
        if record == "Credit":
            row["credits"] += unblended
        else:
            row["usage"] += unblended
        row["net"] += net
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--month", help="YYYY-MM (default: this month to date)")
    ap.add_argument("--activate-tag", action="store_true")
    ap.add_argument("--json")
    args = ap.parse_args(argv)

    ce = boto3.client("ce", region_name="us-east-1")
    report: dict[str, Any] = {"at": datetime.now(UTC).isoformat(timespec="seconds")}
    try:
        plan = boto3.client(
            "freetier", region_name="us-east-1"
        ).get_account_plan_state()
        plan.pop("ResponseMetadata", None)
        report["plan"] = plan
        credits = plan.get("accountPlanRemainingCredits", {})
        print(
            f"plan      {plan['accountPlanType']} ({plan['accountPlanStatus']}), "
            f"credits left {credits.get('amount')} {credits.get('unit')}, "
            f"expires {str(plan.get('accountPlanExpirationDate', '-'))[:10]}"
        )
    except ClientError as e:
        print(f"plan      unavailable ({e.response['Error']['Code']})")

    status = tag_status(ce)
    if args.activate_tag and status != "Active":
        if status == "not yet seen by billing":
            print(
                f"tag       {TAG_KEY}: billing has not seen it yet; "
                "retry once tagged usage has been billed"
            )
        else:
            ce.update_cost_allocation_tags_status(
                CostAllocationTagsStatus=[{"TagKey": TAG_KEY, "Status": "Active"}]
            )
            status = tag_status(ce)
    report["tag_status"] = status
    print(f"tag       {TAG_KEY}={TAG_VALUE}: {status}")

    start, end, complete = month_bounds(args.month, date.today())
    report["period"] = {"start": start, "end": end, "complete": complete}
    query: dict[str, Any] = {
        "TimePeriod": {"Start": start, "End": end},
        "Granularity": "MONTHLY",
        "Metrics": ["UnblendedCost", "NetUnblendedCost"],
        "GroupBy": [
            {"Type": "DIMENSION", "Key": "SERVICE"},
            {"Type": "DIMENSION", "Key": "RECORD_TYPE"},
        ],
    }
    if status == "Active":
        query["Filter"] = {"Tags": {"Key": TAG_KEY, "Values": [TAG_VALUE]}}
    scope = "tag-filtered" if status == "Active" else "account-wide (tag not active)"
    try:
        groups = [
            g
            for r in ce.get_cost_and_usage(**query)["ResultsByTime"]
            for g in r["Groups"]
        ]
    except ce.exceptions.DataUnavailableException:
        report["billing"] = "unavailable"
        print(f"billing   {start}..{end}: Cost Explorer has no data yet (not $0)")
        _write(args.json, report)
        return 0

    rows = summarise(groups)
    report.update(scope=scope, by_service=rows)
    label = "full month" if complete else "month to date"
    print(f"billing   {start}..{end} ({label}, {scope})")
    print(f"  {'service':44} {'usage':>9} {'credits':>9} {'net':>9}")
    totals = {"usage": 0.0, "credits": 0.0, "net": 0.0}
    for service, row in sorted(rows.items(), key=lambda kv: -kv[1]["usage"]):
        print(_line(service, row))
        for k in totals:
            totals[k] += row[k]
    print(_line("TOTAL", totals))
    report["totals"] = totals
    _write(args.json, report)
    return 0


def _line(name: str, row: dict[str, float]) -> str:
    cols = " ".join(f"{row[k]:9.4f}" for k in ("usage", "credits", "net"))
    return f"  {name[:44]:44} {cols}"


def _write(path: str | None, report: dict[str, Any]) -> None:
    if path:
        with open(path, "w") as fh:
            json.dump(report, fh, indent=2, default=str)
            fh.write("\n")


if __name__ == "__main__":
    sys.exit(main())
