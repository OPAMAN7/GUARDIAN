"""Offline checks for GUARD's Telegram wiring.

The risky part is not the network call — it is that GUARD now edits
parent-dashboard/server-ts/.env, the file that also holds JWT_SECRET and the
admin password. A regression there does not throw; it quietly destroys a
credential. So most of this suite is about surgical editing: rewrite in place,
never duplicate, never drop a line, never touch a comment.

No network is used: telegram_api is stubbed. Run: python3 tools/test_guard_telegram.py
"""
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from urllib.error import HTTPError, URLError
import io
import json
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import GUARD  # noqa: E402

PASS = 0
FAIL = 0

EXISTING_ENV = """\
# Server Configuration
PORT=3000
JWT_SECRET=super-secret-signing-key
ADMIN_USERNAME=admin
ADMIN_PASSWORD=keep-me-please

# ─── AI progress reports ────────────────────────────────────────────────────
GEMINI_API_KEY=AQ.example-key
AI_MODEL=gemini-2.5-flash
AI_SEND_TITLES=false
"""


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}" + (f" -- {detail}" if detail else ""))


def with_env(text=EXISTING_ENV):
    """Point GUARD at a throwaway .env. The real one is never opened here."""
    tmp = Path(tempfile.mkdtemp(prefix="guard-env-")) / ".env"
    if text is not None:
        tmp.write_text(text, encoding="utf-8")
    GUARD.SERVER_ENV_PATH = tmp
    return tmp


class _FakeResponse(io.BytesIO):
    """Just enough of an http response for telegram_api's context manager."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


@contextmanager
def stub_urlopen(body=None, error=None):
    """Replace urllib's opener so no test in this file touches the network."""
    calls = []
    original = urllib.request.urlopen

    def fake(req, timeout=None):
        calls.append({
            "url": req.full_url,
            "method": req.get_method(),
            "body": json.loads(req.data.decode("utf-8")) if req.data else None,
        })
        if error is not None:
            raise error
        return _FakeResponse(json.dumps(body or {"ok": True, "result": {}}).encode("utf-8"))

    urllib.request.urlopen = fake
    try:
        yield calls
    finally:
        urllib.request.urlopen = original


def test_env_editing():
    print("\n.env editing -- the file also holds JWT_SECRET, so nothing may be lost")
    path = with_env()

    env = GUARD.read_server_env()
    check("existing keys are parsed", env.get("JWT_SECRET") == "super-secret-signing-key", str(env))
    check("comment lines are not parsed as keys",
          not any(k.startswith("#") for k in env), str(list(env)))
    check("the AI key is seen too", env.get("GEMINI_API_KEY") == "AQ.example-key")

    GUARD.upsert_server_env(
        {"TELEGRAM_BOT_TOKEN": "111:AAA", "TELEGRAM_CHAT_ID": "-100999"},
        header="# ─── Telegram alerts ───",
    )
    after = path.read_text(encoding="utf-8")
    check("JWT_SECRET survived an append", "JWT_SECRET=super-secret-signing-key" in after)
    check("the admin password survived an append", "ADMIN_PASSWORD=keep-me-please" in after)
    check("existing comments survived", "# Server Configuration" in after)
    check("the new keys landed", "TELEGRAM_BOT_TOKEN=111:AAA" in after and "TELEGRAM_CHAT_ID=-100999" in after)
    check("the header was written once", after.count("Telegram alerts") == 1, str(after.count("Telegram alerts")))

    # Second write with a new value: the line must be replaced, not added again.
    GUARD.upsert_server_env({"TELEGRAM_BOT_TOKEN": "222:BBB"}, header="# ─── Telegram alerts ───")
    after2 = path.read_text(encoding="utf-8")
    check("a re-run rewrites the token in place", "TELEGRAM_BOT_TOKEN=222:BBB" in after2)
    check("and leaves no duplicate line", after2.count("TELEGRAM_BOT_TOKEN=") == 1,
          str(after2.count("TELEGRAM_BOT_TOKEN=")))
    check("no duplicate header on the second pass", after2.count("Telegram alerts") == 1)
    check("the old value is gone", "111:AAA" not in after2)
    check("the chat id was left alone", "TELEGRAM_CHAT_ID=-100999" in after2)
    check("no CRLF was introduced", "\r" not in after2)


def test_missing_env_and_state():
    print("\nA missing .env, and the state the menu badge reads")
    path = with_env(text=None)
    check("a missing file reads as empty rather than crashing", GUARD.read_server_env() == {})
    GUARD.upsert_server_env({"TELEGRAM_BOT_TOKEN": "1:A", "TELEGRAM_CHAT_ID": "7"})
    check("the file is created on first write", path.exists())
    has_token, has_chat, enabled = GUARD.telegram_state()
    check("state sees both halves", has_token and has_chat)
    check("alerts default to enabled", enabled is True)

    GUARD.upsert_server_env({"TELEGRAM_ALERTS": "false"})
    check("TELEGRAM_ALERTS=false reads as muted", GUARD.telegram_state()[2] is False)
    check("muting keeps the credentials", GUARD.telegram_state()[0] and GUARD.telegram_state()[1])

    with_env(text="TELEGRAM_BOT_TOKEN=1:A\n")
    has_token, has_chat, _ = GUARD.telegram_state()
    check("a half-configured channel is reported as such", has_token and not has_chat)
    check("the menu badge shows OFF until both halves exist", "OFF" in GUARD._telegram_tag())


def test_secrecy():
    print("\nSecrecy -- GUARD prints enough to recognise a key, never enough to use it")
    token = "7712345678:AAF-this-is-a-secret-bot-token"
    masked = GUARD.mask_secret(token)
    check("the middle of the token is hidden", "AAF-this-is-a-secret" not in masked, masked)
    check("the mask still identifies the key", masked.startswith("7712") and "chars" in masked, masked)
    check("an empty secret says so plainly", GUARD.mask_secret("") == "(not set)")
    check("a short secret is fully starred", GUARD.mask_secret("abcd") == "****")

    # The failure path, with urlopen stubbed so this stays deterministic and fast
    # (the sandbox has no egress; a real call would just be a slow version of it).
    with stub_urlopen(error=URLError("[Errno -3] Temporary failure in name resolution")):
        ok, _result, detail = GUARD.telegram_api(token, "getMe", timeout=2.0)
    check("an unreachable Telegram returns False instead of raising", ok is False)
    check("the failure text never contains the token", token not in detail, detail)
    check("the failure text never contains the API URL", "api.telegram.org/bot" not in detail, detail)
    check("the failure text names the cause", "cannot reach Telegram" in detail, detail)


def test_api_and_discovery():
    print("\nBot API calls and automatic chat-id discovery")
    token = "1:A"

    with stub_urlopen(body={"ok": True, "result": {"username": "guardian_test_bot"}}) as calls:
        ok, me, _ = GUARD.telegram_api(token, "getMe")
    check("a good answer is unwrapped to result", ok and me.get("username") == "guardian_test_bot", str(me))
    check("the call is a POST", calls[0]["method"] == "POST", calls[0]["method"])
    check("the method name is in the path", calls[0]["url"].endswith("/getMe"), calls[0]["url"])

    with stub_urlopen(body={"ok": False, "description": "Unauthorized"}):
        ok, _me, detail = GUARD.telegram_api(token, "getMe")
    check("Telegram's own ok:false is a failure", ok is False and detail == "Unauthorized", detail)

    with stub_urlopen(error=HTTPError("u", 404, "Not Found", {}, io.BytesIO(b"not json"))):
        ok, _me, detail = GUARD.telegram_api(token, "getMe")
    check("an HTTP error is reported without a traceback", ok is False and "404" in detail, detail)

    updates = {"ok": True, "result": [
        {"message": {"chat": {"id": 555111, "first_name": "Aman", "type": "private"}}},
        {"message": {"chat": {"id": 555111, "first_name": "Aman", "type": "private"}}},
        {"channel_post": {"chat": {"id": -1009876, "title": "Family alerts", "type": "channel"}}},
        {"message": {"text": "no chat here"}},
    ]}
    with stub_urlopen(body=updates):
        found = dict(GUARD.discover_chat_ids(token))
    check("both chats are discovered", set(found) == {"555111", "-1009876"}, str(found))
    check("a repeated chat is listed once", len(found) == 2, str(len(found)))
    check("a private chat is labelled by name", found.get("555111") == "Aman", str(found))
    check("a channel is labelled by title", found.get("-1009876") == "Family alerts", str(found))

    with stub_urlopen(error=URLError("offline")):
        check("discovery on a dead network returns nothing rather than raising",
              GUARD.discover_chat_ids(token) == [])


def test_no_half_configuration():
    print("\nA bad token must not leave the server half-configured")
    path = with_env(text="PORT=3000\n")

    # A token that fails validation must never be written: a saved-but-invalid
    # token means the dashboard boots believing it can alert, and stays silent.
    inputs = iter(["not-a-token"])
    GUARD.input = lambda _prompt="": next(inputs)
    try:
        GUARD.action_telegram(dict(GUARD.DEFAULTS))
    finally:
        del GUARD.input
    check("a malformed token is rejected before any write",
          "TELEGRAM_BOT_TOKEN" not in path.read_text(encoding="utf-8"), path.read_text(encoding="utf-8"))

    inputs = iter(["7712345678:AAF-looks-real-enough"])
    GUARD.input = lambda _prompt="": next(inputs)
    try:
        with stub_urlopen(body={"ok": False, "description": "Unauthorized"}):
            GUARD.action_telegram(dict(GUARD.DEFAULTS))
    finally:
        del GUARD.input
    check("a token Telegram rejects is not written either",
          "TELEGRAM_BOT_TOKEN" not in path.read_text(encoding="utf-8"))

    # Valid token, but the parent has not messaged the bot yet, so no chat id can
    # be found. Saving the token alone would look configured and never deliver.
    inputs = iter(["7712345678:AAF-looks-real-enough", ""])
    GUARD.input = lambda _prompt="": next(inputs)
    try:
        with stub_urlopen(body={"ok": True, "result": []}):
            GUARD.action_telegram(dict(GUARD.DEFAULTS))
    finally:
        del GUARD.input
    check("a token with no discoverable chat is not written",
          "TELEGRAM_BOT_TOKEN" not in path.read_text(encoding="utf-8"))
    check("PORT was never disturbed by any of that",
          "PORT=3000" in path.read_text(encoding="utf-8"))
    check("a test send without credentials fails loudly instead of pretending",
          GUARD._telegram_send_test("", "") is False)


def test_happy_path():
    print("\nThe path a parent actually takes")
    path = with_env(text="PORT=3000\nJWT_SECRET=keep\n")
    inputs = iter(["7712345678:AAF-a-plausible-bot-token", ""])
    GUARD.input = lambda _prompt="": next(inputs)

    # getMe, getUpdates and sendMessage all answer from the same stub; each is
    # shaped so the code takes the branch a real successful setup would.
    def fake_api(token, method, payload=None, timeout=12.0):
        if method == "getMe":
            return True, {"username": "guardian_test_bot"}, ""
        if method == "getUpdates":
            return True, [{"message": {"chat": {"id": 4242, "first_name": "Aman"}}}], ""
        if method == "sendMessage":
            fake_api.sent.append(payload)
            return True, {"message_id": 1}, ""
        return False, {}, "unexpected method"

    fake_api.sent = []
    real_api = GUARD.telegram_api
    GUARD.telegram_api = fake_api
    try:
        GUARD.action_telegram(dict(GUARD.DEFAULTS))
    finally:
        GUARD.telegram_api = real_api
        del GUARD.input

    written = path.read_text(encoding="utf-8")
    check("the token is saved", "TELEGRAM_BOT_TOKEN=7712345678:AAF-a-plausible-bot-token" in written)
    check("the discovered chat id is saved", "TELEGRAM_CHAT_ID=4242" in written, written)
    check("alerts are switched on", "TELEGRAM_ALERTS=true" in written)
    check("a flood ceiling is written so the default is never unbounded",
          "TELEGRAM_MAX_PER_HOUR=20" in written, written)
    check("the pre-existing JWT secret is untouched", "JWT_SECRET=keep" in written)
    check("the saved block explains itself for whoever opens the file later",
          "gitignored" in written, written)
    check("a confirmation message was actually sent", len(fake_api.sent) == 1, str(len(fake_api.sent)))
    check("the confirmation went to the discovered chat",
          fake_api.sent and str(fake_api.sent[0]["chat_id"]) == "4242", str(fake_api.sent))
    check("the badge now reads ON", "ON" in GUARD._telegram_tag(), GUARD._telegram_tag())


def test_menu_and_cli_wiring():
    print("\nWiring -- the option has to be reachable, or none of the above matters")
    source = (Path(__file__).resolve().parents[1] / "GUARD.py").read_text(encoding="utf-8")
    check("the menu lists the Telegram option", "M. Connect Telegram Alerts" in source)
    check("the menu dispatches 'm' to action_telegram",
          'choice.lower() == "m"' in source and "action_telegram(cfg)" in source)
    check("the CLI accepts GUARD.bat telegram",
          '"telegram", "tg", "alerts"' in source)
    check("the CLI help mentions it", "telegram [test]" in source)
    check("no Telegram key was added to DEFAULTS",
          "telegram" not in source.split("DEFAULTS = {")[1].split("}")[0].lower(),
          "a guard_runtime.json key would be silently dropped by load_cfg unless "
          "it is in DEFAULTS -- the credentials deliberately live in .env instead")

    batch = (Path(__file__).resolve().parents[1] / "GUARD.bat").read_text(encoding="utf-8")
    check("GUARD.bat documents the new command", "GUARD.bat telegram" in batch)


def main():
    print("=" * 66)
    print("GUARD Telegram wiring -- offline suite")
    print("=" * 66)
    test_env_editing()
    test_missing_env_and_state()
    test_secrecy()
    test_api_and_discovery()
    test_no_half_configuration()
    test_happy_path()
    test_menu_and_cli_wiring()
    print("\n" + "=" * 66)
    print(f"RESULT: {PASS} passed, {FAIL} failed")
    print("=" * 66)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())







