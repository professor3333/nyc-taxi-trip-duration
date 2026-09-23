"""Prove an alert reaches a person, and that recovery does too.

    uv run python scripts/alarm_drill.py              # real failure, ~10-15 min
    uv run python scripts/alarm_drill.py --forced     # delivery leg only, ~1 min

Real mode writes one ERROR line, in the service's own JSON format, into the
function's log group (stream ``alarm-drill``, so it is never mistaken for a
request). From there it travels exactly the path an application error does:

    log line -> ErrorCount metric filter -> ErrorCount alarm -> ALARM action
    -> SNS -> email ... five quiet minutes ... -> OK action -> SNS -> email

(A platform error cannot be caused harmlessly: the Web Adapter passes a
non-HTTP invoke to the app as POST /events, which answers 404 - verified
2026-09-23.) The drill passes only when each hop is observed: the alarm
entered ALARM and then OK, CloudWatch recorded a
*successful* SNS action for both transitions, and SNS counted deliveries to
the topic's confirmed subscriptions without failures. --forced skips the
first hop by setting the alarm state directly (set-alarm-state runs the same
actions). A subscription nobody confirmed fails the drill up front: it would
make every other hop succeed while no one is told.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import boto3

PROJECT = "nyc-taxi-trip-duration"


def say(msg: str) -> None:
    print(f"{datetime.now(UTC):%H:%M:%S}  {msg}", flush=True)


def confirmed_subscriptions(sns: Any, topic: str) -> list[str]:
    subs = sns.list_subscriptions_by_topic(TopicArn=topic)["Subscriptions"]
    for s in subs:
        say(
            f"subscription {s['Protocol']}:{s['Endpoint']} "
            f"-> {s['SubscriptionArn'][:60]}"
        )
    return [s["Endpoint"] for s in subs if s["SubscriptionArn"].startswith("arn:")]


def wait_state(cw: Any, alarm: str, want: str, timeout_s: int) -> datetime:
    deadline = time.monotonic() + timeout_s
    while True:
        a = cw.describe_alarms(AlarmNames=[alarm])["MetricAlarms"][0]
        if a["StateValue"] == want:
            say(f"{alarm} is {want}: {a['StateReason'][:120]}")
            ts: datetime = a["StateUpdatedTimestamp"]
            return ts
        if time.monotonic() > deadline:
            raise SystemExit(f"FAIL {alarm} did not reach {want} within {timeout_s}s")
        time.sleep(20)


def action_results(cw: Any, alarm: str, since: datetime) -> list[str]:
    items = cw.describe_alarm_history(
        AlarmName=alarm, HistoryItemType="Action", StartDate=since, MaxRecords=20
    )["AlarmHistoryItems"]
    return [i["HistorySummary"] for i in items]


def sns_counts(cw: Any, topic_name: str, since: datetime) -> dict[str, float]:
    out = {}
    for metric in ("NumberOfNotificationsDelivered", "NumberOfNotificationsFailed"):
        pts = cw.get_metric_statistics(
            Namespace="AWS/SNS",
            MetricName=metric,
            Dimensions=[{"Name": "TopicName", "Value": topic_name}],
            StartTime=since - timedelta(minutes=5),
            EndTime=datetime.now(UTC) + timedelta(minutes=1),
            Period=60,
            Statistics=["Sum"],
        )["Datapoints"]
        out[metric] = sum(p["Sum"] for p in pts)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--function", default=PROJECT)
    ap.add_argument("--alarm", default=f"{PROJECT}-ErrorCount")
    ap.add_argument("--forced", action="store_true")
    ap.add_argument("--evidence", help="write the drill transcript as JSON")
    args = ap.parse_args(argv)

    cw, sns, logs = (
        boto3.client("cloudwatch"),
        boto3.client("sns"),
        boto3.client("logs"),
    )
    alarm = cw.describe_alarms(AlarmNames=[args.alarm])["MetricAlarms"][0]
    topic = alarm["AlarmActions"][0]
    if alarm.get("OKActions") != alarm["AlarmActions"]:
        raise SystemExit(f"FAIL {args.alarm} does not notify on OK (run monitoring.sh)")
    if not confirmed_subscriptions(sns, topic):
        raise SystemExit(
            "FAIL no confirmed subscription on the alert topic: every alarm "
            "would fire into nothing. Confirm the email from AWS Notifications."
        )
    if alarm["StateValue"] != "OK":
        raise SystemExit(f"FAIL {args.alarm} is {alarm['StateValue']} before the drill")

    start = datetime.now(UTC)
    evidence: dict[str, Any] = {"alarm": args.alarm, "topic": topic, "start": start}
    if args.forced:
        cw.set_alarm_state(
            AlarmName=args.alarm,
            StateValue="ALARM",
            StateReason=f"drill {start:%FT%TZ}",
        )
        evidence["alarm_at"] = wait_state(cw, args.alarm, "ALARM", 120)
        cw.set_alarm_state(
            AlarmName=args.alarm,
            StateValue="OK",
            StateReason=f"drill {start:%FT%TZ} over",
        )
        evidence["ok_at"] = wait_state(cw, args.alarm, "OK", 120)
    else:
        group, stream = f"/aws/lambda/{args.function}", "alarm-drill"
        try:
            logs.create_log_stream(logGroupName=group, logStreamName=stream)
        except logs.exceptions.ResourceAlreadyExistsException:
            pass
        line = {
            "ts": start.isoformat(),
            "level": "ERROR",
            "logger": "alarm_drill",
            "msg": "alarm drill: synthetic error, no request failed",
            "event": "alarm_drill",
            "request_id": "-",
            "model_version": "-",
        }
        logs.put_log_events(
            logGroupName=group,
            logStreamName=stream,
            logEvents=[
                {
                    "timestamp": int(start.timestamp() * 1000),
                    "message": json.dumps(line),
                }
            ],
        )
        say(f"wrote one ERROR line to {group}/{stream}")
        evidence["log_line"] = line
        evidence["alarm_at"] = wait_state(cw, args.alarm, "ALARM", 900)
        evidence["ok_at"] = wait_state(cw, args.alarm, "OK", 1200)

    time.sleep(30)  # action history and SNS metrics land a little later
    actions = action_results(cw, args.alarm, start)
    for a in actions:
        say(f"action: {a}")
    counts = sns_counts(cw, topic.split(":")[-1], start)
    say(f"SNS since drill start: {counts}")
    evidence.update(actions=actions, sns=counts, end=datetime.now(UTC))
    if args.evidence:
        with open(args.evidence, "w") as fh:
            json.dump(evidence, fh, indent=2, default=str)

    ok_actions = [a for a in actions if a.startswith("Successfully executed action")]
    failed = [a for a in actions if a.startswith("Failed")]
    problems = []
    if len(ok_actions) < 2 or failed:
        problems.append(f"expected 2 successful actions (ALARM, OK), got {actions}")
    if counts["NumberOfNotificationsFailed"] > 0:
        problems.append(f"SNS reported failed deliveries: {counts}")
    if counts["NumberOfNotificationsDelivered"] < 2:
        problems.append(
            "SNS has not (yet) counted 2 deliveries; its metrics lag by minutes, "
            f"re-check the topic's NumberOfNotificationsDelivered: {counts}"
        )
    for p in problems:
        print(f"FAIL {p}")
    if not problems:
        print(
            "drill passed: failure -> ALARM -> delivered; recovery -> OK -> delivered"
        )
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
