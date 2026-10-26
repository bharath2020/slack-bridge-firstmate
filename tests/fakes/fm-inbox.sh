#!/usr/bin/env python3
"""Fake fm-inbox.sh for tests. Data lives under $FM_HOME (a temp dir).

  notes/<id>.json         {"request_id", "body", "announced"}
  replies.json            [{"id","body","cursor"}]   written by the test
  status.txt              returned by `status`
  fail-note               if present, `note` exits 1
  unannounced             if present, waking Firstmate fails (`note` exits 3,
                          `announce` exits 1)
  hang-after-save         if present, `note` saves the note, then hangs
                          before waking Firstmate (the caller's timeout fires)
"""
import json, os, sys, time
from pathlib import Path

home = Path(os.environ["FM_HOME"])
notes = home / "notes"
notes.mkdir(parents=True, exist_ok=True)
cmd, args = sys.argv[1], sys.argv[2:]
(home / "calls.log").open("a").write(json.dumps(sys.argv[1:]) + "\n")


def wake(path):
    rec = json.loads(path.read_text())
    if not rec.get("announced") and not (home / "unannounced").exists():
        rec["announced"] = True
        path.write_text(json.dumps(rec))
    return rec["announced"]


def answer(outcome, nid, announced):
    print(json.dumps({"schema": "fm-inbox-note.v1", "outcome": outcome, "id": nid,
                      "announced": announced, "acknowledged": False}))
    sys.exit(0 if announced else 3)


if cmd == "note":
    assert args[0] == "--request-id" and args[2] == "--json" and args[3] == "-", args
    rid = args[1]
    body = sys.stdin.read()
    if (home / "fail-note").exists():
        sys.exit(1)
    for p in notes.glob("*.json"):
        if json.loads(p.read_text())["request_id"] == rid:
            answer("replay", p.stem, wake(p))
    nid = "n%04d" % (len(list(notes.glob("*.json"))) + 1)
    path = notes / f"{nid}.json"
    path.write_text(json.dumps({"request_id": rid, "body": body, "announced": False}))
    if (home / "hang-after-save").exists():
        time.sleep(30)
    answer("created", nid, wake(path))
if cmd == "announce":
    sys.exit(0 if wake(notes / f"{args[0]}.json") else 1)
if cmd == "receipts":
    after = args[args.index("--after") + 1] if "--after" in args else ""
    replies = json.loads((home / "replies.json").read_text()) if (home / "replies.json").exists() else []
    replies = sorted((r for r in replies if r["cursor"] > after), key=lambda r: r["cursor"])
    pending = []
    for p in sorted(notes.glob("*.json")):
        rec = json.loads(p.read_text())
        pending.append({"id": p.stem, "request_id": rec["request_id"],
                        "announced": rec.get("announced", True), "acknowledged": False})
    print(json.dumps({"schema": "fm-inbox-receipts.v1", "pending": pending, "handled": [],
                      "replies": replies,
                      "reply_cursor": replies[-1]["cursor"] if replies else after,
                      "omitted": []}))
    sys.exit(0)
if cmd == "status":
    print((home / "status.txt").read_text() if (home / "status.txt").exists() else "idle")
    sys.exit(0)
sys.exit(64)
