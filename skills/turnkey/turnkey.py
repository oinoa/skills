#!/usr/bin/env python3
"""OINOA turnkey setup: the person's request to OINOA's specialists, posted from this server.

  turnkey.py status                      may the person post a request now?
  turnkey.py post            < text      post it, only after their clear yes to this exact text
  turnkey.py post --dry-run  < text      check everything, post nothing

The text comes on standard input (a heredoc), or as the arguments after `post`.
Prints one line. A line that starts with ERROR says what happened."""
import getpass
import json
import os
import sys
import urllib.error
import urllib.request

URL = os.environ.get("OINOA_TURNKEY_URL", "https://my.oinoa.com/api/agent/turnkey")
SAY = {
    "team_only": "ERROR team_only: a specialist comes with the Business plan («Бизнес»); the person moves to it in the OINOA app.",
    "no_plan": "ERROR no_plan: no live OINOA plan on this account; do not offer a specialist.",
    "off": "ERROR off: turnkey setup is not open yet; do not offer it.",
    "unknown_server": "ERROR unknown_server: OINOA does not know this server; do not offer a specialist.",
    "too_many": "ERROR too_many: three requests are open already; the person closes one in the OINOA app, Настройки → Настройка под ключ.",
    "bad_text": "ERROR bad_text: the text is empty or longer than 1000 characters; make it shorter.",
}


def call(method, body=None):
    req = urllib.request.Request(URL, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", "User-Agent": "oinoa-turnkey/1"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}
    except (urllib.error.URLError, OSError, ValueError):
        return 0, {}


def fail(st, d):
    err = d.get("error") if isinstance(d, dict) else None
    print(SAY.get(err) or f"ERROR unreachable: OINOA did not answer ({st or 'no connection'}); try again in a few minutes.")
    return 1


def main(argv):
    if argv[:1] == ["status"]:
        st, d = call("GET")
        if st == 200 and isinstance(d, dict) and d.get("ok") is True:
            print("OK available: you may offer a specialist")
            return 0
        return fail(st, d)
    if argv[:1] == ["post"]:
        dry = "--dry-run" in argv[1:]
        words = [a for a in argv[1:] if a != "--dry-run"]
        text = (" ".join(words) if words else ("" if sys.stdin.isatty() else sys.stdin.read())).strip()
        if not text:
            print(SAY["bad_text"])
            return 1
        me = getpass.getuser()  # the agent's Linux user is agent-<its name>
        st, d = call("POST", {"text": text, "agent": me[6:] if me.startswith("agent-") else me, **({"dryRun": True} if dry else {})})
        if st == 200 and isinstance(d, dict) and "request" in d:
            print("OK checked: nothing was posted" if dry else f"OK posted request {d['request']}")
            return 0
        return fail(st, d)
    print(__doc__.strip())
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
