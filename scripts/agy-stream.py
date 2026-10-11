#!/usr/bin/env python3
"""Antigravity CLI as a crossreview reviewer: the brief on stdin, the review on stdout.

  agy-stream.py BIN MODEL < brief > review

In print mode `agy` takes its prompt only as the value of -p, an argument, and
reads no plain text from stdin; a brief does not fit an argument. Its
stream-json input has no such limit, so this script sends the brief as the one
JSON message that input expects and prints the response of the result the CLI
answers with. The reviewer's command stays the plain `< {brief} > {out}` line
every runner of a roster knows.

Standard library only, Python 3.8 or newer. Exit code: the CLI's own, or 1 when
it reported a failure, 2 when the brief cannot be sent.
"""

import json
import shutil
import subprocess
import sys
import threading

NAME = "agy-stream"


def say(text: str) -> None:
    sys.stderr.write("%s: %s\n" % (NAME, text))
    sys.stderr.flush()


def message(brief: str) -> bytes:
    """One line of ASCII, so no code page between this script and the CLI can touch it."""
    return (json.dumps({"event": "user", "message": {"content": brief}}) + "\n").encode("ascii")


def read_events(lines) -> tuple:
    """(result or None, text of the last agent response) from the CLI's event
    lines, read as they arrive: the conversation id is reported at once, so a
    run that is cut short still leaves it behind."""
    result, partial, last = None, {}, None
    for raw in lines:
        try:
            event = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("event") == "init" and event.get("conversation_id"):
            # Enough to take the answer out of a run that timed out:
            # agy --conversation <id> -p="Write your final review now."
            say("conversation %s" % event["conversation_id"])
        if event.get("event") == "result" and isinstance(event.get("result"), dict):
            result = event["result"]
        step = event.get("step_update")
        if isinstance(step, dict) and step.get("step_type") == "agent_response" and step.get("text_delta"):
            last = str(step.get("step_index"))
            partial.setdefault(last, []).append(str(step["text_delta"]))
    return result, "".join(partial[last]) if partial else ""


def main(argv) -> int:
    if len(argv) < 3:
        say("usage: agy-stream.py BIN MODEL < brief > review")
        return 2
    binary, model = argv[1:-1], argv[-1]
    try:
        brief = sys.stdin.buffer.read().decode("utf-8")
    except UnicodeDecodeError:
        say("the brief is not UTF-8, and JSON carries nothing else")
        return 2
    command = [shutil.which(binary[0]) or binary[0]] + binary[1:] + [
        "--model", model, "--mode", "plan", "--input-format", "stream-json", "--output-format", "stream-json", "-p="]
    try:
        proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    except OSError as exc:
        say("cannot start %s: %s" % (binary[0], exc))
        return 127

    def send() -> None:
        # From a thread: a brief longer than a pipe holds must not wait for a
        # CLI that is already writing events nobody reads yet.
        try:
            proc.stdin.write(message(brief))
            proc.stdin.close()
        except OSError:
            pass  # the CLI went away; its exit code and stderr say why

    threading.Thread(target=send, daemon=True).start()
    result, partial = read_events(proc.stdout)
    code = proc.wait()
    if result is None:
        sys.stdout.buffer.write(partial.encode("utf-8"))
        say("the CLI ended without a result (exit code %s)" % code)
        return code or 1
    failed = result.get("status") != "SUCCESS"
    # A failed turn may report no response at all: what it had streamed is still worth reading.
    review = result.get("response") or (partial if failed else "")
    sys.stdout.buffer.write(str(review).encode("utf-8"))
    sys.stdout.flush()
    denied = [a.get("display_name") or a.get("action") or "?" for a in result.get("denied_actions") or []
              if isinstance(a, dict)]
    if denied:
        say("denied tool calls: %s" % ", ".join(denied))
    if failed:
        say("%s: %s" % (result.get("status") or "no status", result.get("error") or "no error text"))
        return code or 1
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv))
