#!/usr/bin/env python3
"""OINOA mail: the person's own mailbox over IMAP/SMTP with an app password, or Microsoft's sign-in for Outlook.

  connect ADDRESS [--provider P] [--chat ID --msg ID]   app password on stdin (Outlook: none, it prints a code)
  finish                                                Outlook: after they entered the code at Microsoft
  status | list [N] [--unread] | search WORDS | read ID [--save]
  reply ID | send TO SUBJECT                            body on stdin
  disconnect

Python 3 stdlib only. Every answer is a few short lines. A line that starts with
ERROR names what went wrong and what to tell the person.
"""
import argparse
import base64
import email
import email.policy
import email.utils
import imaplib
import json
import os
import re
import smtplib
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from email.header import decode_header, make_header
from email.message import EmailMessage
from html.parser import HTMLParser

HOME = os.path.expanduser("~")
# ~/.config first; ~/.oinoa-mail when ~/.config is not the agent's own (seen root-owned on client boxes).
STORES = [os.environ["OINOA_MAIL_DIR"]] if os.environ.get("OINOA_MAIL_DIR") else [
    os.path.join(HOME, ".config", "oinoa-mail"), os.path.join(HOME, ".oinoa-mail")]
ACCOUNT = next((os.path.join(d, "account.json") for d in STORES if os.path.exists(os.path.join(d, "account.json"))),
               os.path.join(STORES[0], "account.json"))
TG_DIR = os.environ.get("TELEGRAM_STATE_DIR") or os.path.join(HOME, ".claude", "channels", "telegram")
TG_API = os.environ.get("OINOA_TG_API", "https://api.telegram.org")
INSECURE = os.environ.get("OINOA_MAIL_INSECURE") == "1"  # plain sockets, tests only
TIMEOUT = 30
BODY_LIMIT = 6000

# SMTP on 587 with STARTTLS: many servers block outbound 25 and 465.
PROVIDERS = {
    "gmail": ("Gmail", ("imap.gmail.com", 993), ("smtp.gmail.com", 587), "https://myaccount.google.com/apppasswords"),
    "yandex": ("Yandex", ("imap.yandex.ru", 993), ("smtp.yandex.ru", 587), "https://id.yandex.ru/security/app-passwords"),
    "mailru": ("Mail.ru", ("imap.mail.ru", 993), ("smtp.mail.ru", 587), "https://account.mail.ru/user/2-step-auth/passwords/"),
    "icloud": ("iCloud", ("imap.mail.me.com", 993), ("smtp.mail.me.com", 587), "https://account.apple.com"),
}
DOMAINS = {
    "gmail.com": "gmail", "googlemail.com": "gmail",
    "yandex.ru": "yandex", "ya.ru": "yandex", "yandex.com": "yandex", "yandex.by": "yandex",
    "yandex.kz": "yandex", "yandex.ua": "yandex", "narod.ru": "yandex",
    "mail.ru": "mailru", "inbox.ru": "mailru", "list.ru": "mailru", "bk.ru": "mailru", "internet.ru": "mailru",
    "icloud.com": "icloud", "me.com": "icloud", "mac.com": "icloud",
}
MICROSOFT = re.compile(r"^(outlook|hotmail|live|msn|windowslive|passport)\.[a-z.]+$")

# DIVE-7145: Outlook.com and Microsoft 365 by Microsoft's own sign-in, never a password in the chat: OINOA's Entra app (a public
# client, so its id is no secret) and the device-code flow. They open the link, type the code, sign in and allow OINOA; the
# agent then reads and sends over IMAP/SMTP with the token (XOAUTH2). No app id yet: Outlook stays "not yet", as before.
MS_CLIENT_ID = os.environ.get("OINOA_MS_CLIENT_ID", "")
MS_AUTHORITY = os.environ.get("OINOA_MS_AUTHORITY", "https://login.microsoftonline.com/common").rstrip("/")
MS_SCOPE = "https://outlook.office.com/IMAP.AccessAsUser.All https://outlook.office.com/SMTP.Send offline_access openid email"
PROVIDERS["outlook"] = ("Outlook", ("outlook.office365.com", 993), ("smtp-mail.outlook.com", 587), "https://account.live.com/consent/Manage")
PROVIDERS["microsoft365"] = ("Microsoft 365", ("outlook.office365.com", 993), ("smtp.office365.com", 587), "https://myapps.microsoft.com")
OAUTH = ("outlook", "microsoft365")


def out(*lines):
    print("\n".join(lines))


def fail(code, text, extra=()):
    out(f"ERROR {code}: {text}", *extra)
    sys.exit(2)


def hdr(value):
    if not value:
        return ""
    try:
        return str(make_header(decode_header(str(value)))).replace("\n", " ").strip()
    except Exception:
        return str(value).replace("\n", " ").strip()


def tls():
    return ssl.create_default_context()


def hostport(text, default_port):
    host, _, port = text.partition(":")
    return (host, int(port) if port else default_port)


# ---- the stored account -------------------------------------------------

def load():
    try:
        with open(ACCOUNT) as f:
            return json.load(f)
    except FileNotFoundError:
        fail("not_connected", "no mailbox is connected. Offer to connect it (mail skill, Connect), "
             "or ask them to forward the letter or paste it here.")


def save(acct):
    global ACCOUNT
    for store in STORES:
        try:
            os.makedirs(store, mode=0o700, exist_ok=True)
            os.chmod(store, 0o700)
            path = os.path.join(store, "account.json")
            tmp = path + ".tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(acct, f)
            os.replace(tmp, path)
        except OSError:
            continue
        for other in STORES:  # one mailbox per agent: drop a copy left in the other place
            stale = os.path.join(other, "account.json")
            if other != store and os.path.exists(stale):
                try:
                    os.remove(stale)
                except OSError:
                    pass
        ACCOUNT = path
        return
    fail("cannot_save", "the mailbox works, but it could not be kept on this agent. Nothing is stored; "
         "tell them it will be fixed and to forward or paste the letter meanwhile.")


# ---- Microsoft sign-in ------------------------------------------------------

def ms_post(path, fields):
    """-> (json, error code or None). Microsoft answers an OAuth error as 400 with {"error": ...}."""
    data = urllib.parse.urlencode(fields).encode()
    try:
        with urllib.request.urlopen(f"{MS_AUTHORITY}/oauth2/v2.0/{path}", data, timeout=TIMEOUT) as r:
            return json.load(r), None
    except urllib.error.HTTPError as e:
        try:
            body = json.load(e)
        except ValueError:
            body = {}
        return body, str(body.get("error") or f"http_{e.code}")
    except (OSError, ValueError) as e:
        fail("server", f"Microsoft's sign-in did not answer ({short(e)}). Try again in a minute.")


def ms_token(acct):
    """A live access token for this mailbox: the kept one, or a new one from the refresh token (kept again)."""
    o = acct["oauth"]
    if o.get("access_token") and o.get("expires_at", 0) - 120 > time.time():
        return o["access_token"]
    body, err = ms_post("token", {"client_id": o["client_id"], "grant_type": "refresh_token",
                                  "refresh_token": o["refresh_token"], "scope": MS_SCOPE})
    if err:
        fail("signin_expired", f"Microsoft wants them to sign in again ({err}). Run connect again: it gives a new code, "
             "no password.")
    o["access_token"] = body["access_token"]
    o["expires_at"] = int(time.time()) + int(body.get("expires_in", 3600))
    if body.get("refresh_token"):
        o["refresh_token"] = body["refresh_token"]
    if os.path.exists(ACCOUNT):
        save(acct)
    return o["access_token"]


def pending_path():
    """Where the waiting sign-in is: the store that has one, else the first one this agent can write."""
    for store in STORES:
        if os.path.exists(os.path.join(store, "pending.json")):
            return os.path.join(store, "pending.json")
    for store in STORES:
        try:
            os.makedirs(store, mode=0o700, exist_ok=True)
            if os.access(store, os.W_OK):
                return os.path.join(store, "pending.json")
        except OSError:
            continue
    fail("cannot_save", "the sign-in could not be kept on this agent. Tell them it will be fixed and to forward the letter meanwhile.")


def xoauth2(user, token):
    return f"user={user}\x01auth=Bearer {token}\x01\x01"


def signed_in_as(body):
    """The address Microsoft signed them in with, from the id_token (only read, never trusted for access)."""
    try:
        part = body.get("id_token", "").split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        return str(claims.get("preferred_username") or claims.get("email") or "").lower()
    except (IndexError, ValueError):
        return ""


# ---- servers --------------------------------------------------------------

def imap_open(acct, user=None):
    host, port = acct["imap"]
    if port == 993 and not INSECURE:
        m = imaplib.IMAP4_SSL(host, port, ssl_context=tls(), timeout=TIMEOUT)
    else:
        m = imaplib.IMAP4(host, port, timeout=TIMEOUT)
        if not INSECURE:
            m.starttls(ssl_context=tls())
    if acct.get("oauth"):
        auth = xoauth2(user or acct.get("user") or acct["address"], ms_token(acct))
        m.authenticate("XOAUTH2", lambda _: auth.encode())
    else:
        m.login(user or acct.get("user") or acct["address"], acct["password"])
    return m


def smtp_open(acct):
    host, port = acct["smtp"]
    if port == 465 and not INSECURE:
        s = smtplib.SMTP_SSL(host, port, context=tls(), timeout=TIMEOUT)
    else:
        s = smtplib.SMTP(host, port, timeout=TIMEOUT)
        s.ehlo()
        if not INSECURE:
            s.starttls(context=tls())
            s.ehlo()
    if acct.get("oauth"):
        auth = xoauth2(acct.get("user") or acct["address"], ms_token(acct))
        s.auth("XOAUTH2", lambda challenge=None: auth if challenge is None else "")
    else:
        s.login(acct.get("user") or acct["address"], acct["password"])
    return s


def mailbox(acct):
    try:
        m = imap_open(acct)
        m.select("INBOX", readonly=True)
        return m
    except OSError as e:  # a timeout or a refused connection is the server or the network, never the password
        fail("server", f"the mail server did not answer ({short(e)}). That is the server or the network, not the password: "
             "try again later, and do not ask the person for a new password because of it.")
    except imaplib.IMAP4.error as e:
        if acct.get("oauth"):
            fail("refused", f"the mailbox refused Microsoft's sign-in ({short(e)}). Run connect again: a new code, no password.")
        fail("refused", f"the mailbox refused ({short(e)}). If it keeps refusing, connect it again with a new app password.")


def short(e):
    text = e.args[0] if getattr(e, "args", None) else e
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    return str(text)[:160]


# ---- Telegram: the message with the password leaves the chat ------------

def forget_message(chat, msg, secrets):
    deleted = False
    token = ""
    try:
        with open(os.path.join(TG_DIR, ".env")) as f:
            for line in f:
                if line.startswith("TELEGRAM_BOT_TOKEN="):
                    token = line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    if token and chat and msg:
        try:
            data = urllib.parse.urlencode({"chat_id": chat, "message_id": msg}).encode()
            with urllib.request.urlopen(f"{TG_API}/bot{token}/deleteMessage", data, timeout=15) as r:
                deleted = json.load(r).get("ok") is True
        except Exception:
            deleted = False
    scrub_log(str(msg or ""), [s for s in secrets if len(s) >= 8])
    if deleted:
        return "Chat: the message with the password is deleted."
    return "Chat: could not delete the message with the password. Ask them to delete it now."


def scrub_log(msg, secrets):
    logdir = os.path.join(TG_DIR, "msglog")
    try:
        names = [n for n in os.listdir(logdir) if n.endswith(".jsonl")]
    except OSError:
        return
    for name in names:
        path = os.path.join(logdir, name)
        try:
            with open(path) as f:
                lines = f.readlines()
        except OSError:
            continue
        changed = False
        for i, line in enumerate(lines):
            try:
                row = json.loads(line)
            except ValueError:
                continue
            text = str(row.get("text", ""))
            hit = (msg and str(row.get("message_id")) == msg and row.get("dir") == "in")
            hit = hit or any(s in text for s in secrets)
            if hit:
                row["text"] = "[deleted: mail password]"
                lines[i] = json.dumps(row, ensure_ascii=False) + "\n"
                changed = True
        if changed:
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                f.writelines(lines)
            os.chmod(tmp, os.stat(path).st_mode & 0o777)
            os.replace(tmp, path)


# ---- commands --------------------------------------------------------------

def cmd_connect(a):
    raw = "" if sys.stdin.isatty() else sys.stdin.read()
    address = a.address.strip().lower()
    domain = address.rpartition("@")[2]
    provider = a.provider or DOMAINS.get(domain, "")
    password = raw.strip()
    if provider == "gmail":
        joined = re.sub(r"\s+", "", password)
        found = re.search(r"(?<![A-Za-z])[A-Za-z]{4}(?:\s?[A-Za-z]{4}){3}(?![A-Za-z])", password)
        if re.fullmatch(r"[A-Za-z]{16}", joined):
            password = joined
        elif found:
            password = re.sub(r"\s+", "", found.group(0))
    note = forget_message(a.chat, a.msg, [raw.strip(), password]) if (a.chat or a.msg or raw) else ""
    tail = [note] if note else []

    if "@" not in address:
        fail("bad_address", "that is not an email address. Ask for the full address.", tail)
    if MICROSOFT.match(domain) and provider not in OAUTH:
        provider = "outlook"
    if provider in OAUTH:
        return ms_start(address, provider, tail)
    if not provider and not (a.imap and a.smtp):
        fail("unknown_provider", "ask where this mail lives: Gmail (Google Workspace), Yandex, Mail.ru, iCloud, or Outlook / "
             "Microsoft 365. Then run connect again with --provider gmail|yandex|mailru|icloud|outlook|microsoft365.", tail)
    if not password:
        fail("no_password", "no app password came in on stdin. Run connect with the password in the <<'EOF' block.", tail)

    name, imap, smtp, revoke = PROVIDERS.get(provider, ("the mailbox", None, None, ""))
    acct = {
        "address": address, "provider": provider or "custom", "name": name,
        "imap": list(hostport(a.imap, 993) if a.imap else imap),
        "smtp": list(hostport(a.smtp, 587) if a.smtp else smtp),
        "password": password, "revoke": revoke,
    }
    users = [address] + ([address.split("@")[0]] if provider == "icloud" else [])
    m, err = None, None
    for user in users:
        try:
            m = imap_open(acct, user)
            acct["user"] = user
            break
        except imaplib.IMAP4.error as e:
            err = short(e)
        except OSError as e:
            fail("server", f"could not reach {name} ({short(e)}). Try again in a minute.", tail)
    if m is None:
        if "application-specific password" in err.lower() or "webalert" in err.lower():
            fail("app_password_needed", "that is their main password, not an app password. "
                 f"The app password comes from {revoke} and needs 2-Step Verification on.", tail)
        hint = " In Yandex Mail settings, Mail clients, IMAP must be on." if provider == "yandex" else ""
        fail("login_failed", f"{name} did not accept the password. Ask them to create a new app password"
             f"{' at ' + revoke if revoke else ''} and send it.{hint}", tail)
    total = unseen = "?"
    try:
        typ, data = m.status("INBOX", "(MESSAGES UNSEEN)")
        found = re.search(rb"MESSAGES (\d+).*UNSEEN (\d+)", data[0] or b"")
        if found:
            total, unseen = found.group(1).decode(), found.group(2).decode()
        m.logout()
    except (OSError, imaplib.IMAP4.error):
        pass
    try:
        smtp_open(acct).quit()
        sending = "Sending: works."
    except (OSError, smtplib.SMTPException) as e:
        sending = f"Sending: not available ({short(e)}). Reading works."
    before = ""
    if os.path.exists(ACCOUNT):
        try:
            with open(ACCOUNT) as f:
                old = json.load(f).get("address", "")
            if old and old != address:
                before = f" It replaces {old}."
        except (OSError, ValueError):
            pass
    save(acct)
    out(f"OK connected {address} ({name}). Inbox: {total} letters, {unseen} unread.{before}", sending, *tail)


def ms_start(address, provider, tail):
    """Outlook / Microsoft 365, step 1: Microsoft's code for them to enter; the waiting sign-in is kept for finish."""
    if not MS_CLIENT_ID:
        fail("outlook_not_yet", "Outlook cannot be connected yet. Ask them to forward the letter or paste it here.", tail)
    body, err = ms_post("devicecode", {"client_id": MS_CLIENT_ID, "scope": MS_SCOPE})
    if err:
        fail("server", f"Microsoft did not start the sign-in ({err}). Try again in a minute.", tail)
    pending = {"address": address, "provider": provider, "device_code": body["device_code"], "client_id": MS_CLIENT_ID,
               "interval": int(body.get("interval", 5)), "expires_at": int(time.time()) + int(body.get("expires_in", 900))}
    path = pending_path()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(pending, f)
    link, code, mins = body.get("verification_uri", "https://microsoft.com/devicelogin"), body["user_code"], int(body.get("expires_in", 900)) // 60
    out(f"OK sign-in started for {address}. Send them, in one message: open {link}, enter the code {code}, sign in as "
        f"{address} and allow OINOA. No password in this chat. The code works for {mins} minutes.",
        "When they say it is done, run: mail.py finish", *tail)


def cmd_finish(a):
    """Outlook / Microsoft 365, step 2: wait up to about 90 s for their sign-in, then check the mailbox and keep it."""
    path = pending_path()
    try:
        with open(path) as f:
            p = json.load(f)
    except (OSError, ValueError):
        fail("no_signin", "no Microsoft sign-in is waiting. Run connect with their address first.")
    interval, deadline = max(1, p.get("interval", 5)), min(p["expires_at"], time.time() + 90)
    while True:
        body, err = ms_post("token", {"client_id": p["client_id"], "device_code": p["device_code"],
                                      "grant_type": "urn:ietf:params:oauth:grant-type:device_code"})
        if not err:
            break
        if err in ("authorization_pending", "slow_down"):
            interval += 5 if err == "slow_down" else 0
            if time.time() + interval > deadline:
                if time.time() + interval > p["expires_at"]:
                    os.remove(path)
                    fail("expired", "the code expired before they signed in. Run connect again for a new code.")
                out("PENDING: they have not finished signing in at Microsoft yet. Ask them to finish, then run finish again.")
                return
            time.sleep(interval)
            continue
        os.remove(path)
        if err in ("authorization_declined", "access_denied"):
            fail("declined", "they did not allow OINOA at Microsoft. Nothing is connected. Run connect again if they want to.")
        if err == "expired_token":
            fail("expired", "the code expired before they signed in. Run connect again for a new code.")
        fail("signin_failed", f"Microsoft did not finish the sign-in ({err}). Run connect again for a new code.")
    os.remove(path)
    address, provider = p["address"], p["provider"]
    who, other = signed_in_as(body), ""
    if "@" in who and who != address:  # the mailbox is the account they signed in with (a work alias resolves to it too)
        other = (f"They signed in at Microsoft as {who}, so that is the mailbox connected, not {address}. If it is the wrong one, "
                 "run connect again and ask them to sign in with the right account.")
        address = who
    name, imap, smtp, revoke = PROVIDERS[provider]
    acct = {"address": address, "provider": provider, "name": name, "imap": list(imap), "smtp": list(smtp), "revoke": revoke,
            "oauth": {"client_id": p["client_id"], "refresh_token": body.get("refresh_token", ""),
                      "access_token": body["access_token"],
                      "expires_at": int(time.time()) + int(body.get("expires_in", 3600))}}
    try:
        m = imap_open(acct)
    except imaplib.IMAP4.error as e:
        fail("login_failed", f"{name} did not let the sign-in read the mailbox ({short(e)}). A work account may need its "
             "administrator to allow IMAP for it. Ask them to forward the letter meanwhile.")
    except OSError as e:
        fail("server", f"could not reach {name} ({short(e)}). Run connect again in a minute.")
    total = unseen = "?"
    try:
        typ, data = m.status("INBOX", "(MESSAGES UNSEEN)")
        found = re.search(rb"MESSAGES (\d+).*UNSEEN (\d+)", data[0] or b"")
        if found:
            total, unseen = found.group(1).decode(), found.group(2).decode()
        m.logout()
    except (OSError, imaplib.IMAP4.error):
        pass
    try:
        smtp_open(acct).quit()
        sending = "Sending: works."
    except (OSError, smtplib.SMTPException) as e:
        sending = (f"Sending: not available ({short(e)}). Reading works. A work account may need its administrator to "
                   "allow SMTP sending for this mailbox.")
    before = ""
    if os.path.exists(ACCOUNT):
        try:
            with open(ACCOUNT) as f:
                old = json.load(f).get("address", "")
            if old and old != address:
                before = f" It replaces {old}."
        except (OSError, ValueError):
            pass
    save(acct)
    out(f"OK connected {address} ({name}). Inbox: {total} letters, {unseen} unread.{before}", sending, *([other] if other else []))


def cmd_status(a):
    if not os.path.exists(ACCOUNT):
        out("Not connected.")
        return
    acct = load()
    out(f"Connected: {acct['address']} ({acct.get('name', 'mail')}).")


def headers_for(m, uids):
    if not uids:
        return []
    typ, data = m.uid("fetch", b",".join(uids), "(UID FLAGS BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
    rows = []
    for item in data:
        if not isinstance(item, tuple):
            continue
        meta = item[0].decode("utf-8", "replace")
        uid = re.search(r"UID (\d+)", meta)
        if not uid:
            continue
        msg = email.message_from_bytes(item[1], policy=email.policy.compat32)
        name, addr = email.utils.parseaddr(hdr(msg.get("From")))
        when = ""
        try:
            when = email.utils.parsedate_to_datetime(msg.get("Date")).strftime("%Y-%m-%d %H:%M")
        except Exception:
            pass
        new = "" if "\\Seen" in meta else " | unread"
        rows.append((int(uid.group(1)), f"{uid.group(1)} | {when} | {name or addr} | {hdr(msg.get('Subject')) or '(no subject)'}{new}"))
    return [line for _, line in sorted(rows, reverse=True)]


def cmd_list(a):
    acct = load()
    m = mailbox(acct)
    typ, data = m.uid("search", None, "UNSEEN" if a.unread else "ALL")
    uids = data[0].split()
    n = max(1, min(a.n, 50))
    rows = headers_for(m, uids[-n:])
    m.logout()
    if not rows:
        out("No unread letters." if a.unread else "The inbox is empty.")
        return
    kind = "unread" if a.unread else "newest"
    out(f"Inbox {acct['address']}: {len(rows)} {kind} of {len(uids)}. ID | date | from | subject", *rows)


def cmd_search(a):
    acct = load()
    m = mailbox(acct)
    q = " ".join(a.words).strip()
    if not q:
        fail("no_words", "say what to search for.")
    # One key per search, merged: some servers match Cyrillic in FROM and SUBJECT but not in TEXT.
    found = set()
    for key in ("FROM", "SUBJECT", "TEXT"):
        try:
            if q.isascii():
                quoted = '"' + q.replace("\\", "\\\\").replace('"', '\\"') + '"'
                typ, data = m.uid("search", None, key, quoted)
            else:
                m.literal = q.encode("utf-8")
                typ, data = m.uid("search", "CHARSET", "UTF-8", key)
            found.update(data[0].split())
        except imaplib.IMAP4.error:
            continue
    uids = sorted(found, key=int)
    rows = headers_for(m, uids[-10:])
    m.logout()
    if not rows:
        out(f"Nothing in the inbox matches: {q}")
        return
    out(f"Found {len(uids)}, newest {len(rows)}. ID | date | from | subject", *rows)


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "head"):
            self.skip += 1
        if tag in ("br", "p", "div", "tr", "li", "h1", "h2", "h3", "table"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "head") and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def text_of(part):
    try:
        body = part.get_content()
    except Exception:
        body = (part.get_payload(decode=True) or b"").decode("utf-8", "replace")
    if part.get_content_type() == "text/html":
        p = _Text()
        p.feed(body)
        body = "".join(p.parts)
    body = re.sub(r"[ \t\r\f\v]+\n", "\n", body)
    return re.sub(r"\n{3,}", "\n\n", body).strip()


def fetch(m, uid):
    if not uid.isdigit():
        fail("bad_id", "the ID is the number at the start of a line from list or search.")
    typ, data = m.uid("fetch", uid, "(BODY.PEEK[])")
    raw = next((item[1] for item in data if isinstance(item, tuple)), None)
    if raw is None:
        fail("not_found", f"no letter with ID {uid} in the inbox. Run list or search again.")
    return email.message_from_bytes(raw, policy=email.policy.default)


def cmd_read(a):
    acct = load()
    m = mailbox(acct)
    msg = fetch(m, a.id)
    m.logout()
    lines = [f"From: {hdr(msg['From'])}", f"To: {hdr(msg['To'])}"]
    if msg["Cc"]:
        lines.append(f"Cc: {hdr(msg['Cc'])}")
    lines += [f"Date: {hdr(msg['Date'])}", f"Subject: {hdr(msg['Subject'])}", ""]
    part = msg.get_body(preferencelist=("plain", "html"))
    body = text_of(part) if part is not None else ""
    if len(body) > BODY_LIMIT:
        body = body[:BODY_LIMIT] + f"\n[... {len(body) - BODY_LIMIT} more characters cut]"
    lines.append(body or "(no text)")
    files = list(msg.iter_attachments())
    if files:
        lines.append("")
        folder = os.path.join(HOME, "mail", a.id)
        for i, f in enumerate(files, 1):
            fname = os.path.basename(hdr(f.get_filename()) or f"attachment-{i}") or f"attachment-{i}"
            payload = f.get_payload(decode=True) or b""
            if a.save:
                os.makedirs(folder, exist_ok=True)
                path = os.path.join(folder, fname)
                with open(path, "wb") as fh:
                    fh.write(payload)
                lines.append(f"Attachment saved: {path}")
            else:
                lines.append(f"Attachment: {fname} ({len(payload) // 1024 + 1} KB). read {a.id} --save keeps it.")
    out(*lines)


def deliver(acct, to, subject, body, extra=None):
    msg = EmailMessage()
    msg["From"] = acct["address"]
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = email.utils.formatdate(localtime=True)
    msg["Message-ID"] = email.utils.make_msgid(domain=acct["address"].rpartition("@")[2])
    for key, value in (extra or {}).items():
        if value:
            msg[key] = value
    msg.set_content(body)
    try:
        s = smtp_open(acct)
        s.send_message(msg)
        s.quit()
    except smtplib.SMTPAuthenticationError as e:
        fail("not_sent", f"the letter was not sent: the mailbox refused the login ({short(e)}). "
             + ("Run connect again: a new Microsoft code, no password." if acct.get("oauth") else "Connect it again with a new app password."))
    except smtplib.SMTPException as e:  # an OSError too, so before the network case
        fail("not_sent", f"the letter was not sent ({short(e)}). Nothing left the mailbox.")
    except OSError as e:
        fail("not_sent", f"the letter was not sent: the mail server did not answer ({short(e)}). That is the server or the network, "
             "not the password; nothing left the mailbox. Try again later.")
    out(f"OK sent to {to}: {subject}")


def body_from_stdin():
    body = "" if sys.stdin.isatty() else sys.stdin.read().strip()
    if not body:
        fail("no_text", "the letter text goes in the <<'EOF' block.")
    return body + "\n"


def cmd_reply(a):
    acct = load()
    body = body_from_stdin()
    m = mailbox(acct)
    orig = fetch(m, a.id)
    m.logout()
    to = hdr(orig["Reply-To"]) or hdr(orig["From"])
    subject = hdr(orig["Subject"])
    if not re.match(r"(?i)^(re|ответ)\s*:", subject):
        subject = "Re: " + subject
    ref = orig["Message-ID"]
    refs = " ".join(x for x in (hdr(orig["References"]), ref) if x)
    deliver(acct, to, subject, body, {"In-Reply-To": ref, "References": refs})


def cmd_send(a):
    acct = load()
    if "@" not in a.to:
        fail("bad_address", "the first word after send is the address.")
    deliver(acct, a.to, a.subject, body_from_stdin())


def cmd_disconnect(a):
    if not os.path.exists(ACCOUNT):
        out("Not connected.")
        return
    acct = load()
    os.remove(ACCOUNT)
    revoke = acct.get("revoke")
    off = "OINOA's access" if acct.get("oauth") else "the password"
    out(f"OK disconnected {acct['address']}. This agent can no longer read it.",
        *([f"To switch {off} off everywhere, they remove it here: {revoke}"] if revoke else []))


def main():
    p = argparse.ArgumentParser(prog="mail.py", description="The person's own mailbox.")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("connect")
    c.add_argument("address")
    c.add_argument("--provider", choices=sorted(PROVIDERS))
    c.add_argument("--chat", help="chat_id of the message with the password")
    c.add_argument("--msg", help="message_id of the message with the password")
    c.add_argument("--imap", help="host[:port], a mailbox outside the known providers")
    c.add_argument("--smtp", help="host[:port]")
    sub.add_parser("finish")
    sub.add_parser("status")
    ls = sub.add_parser("list")
    ls.add_argument("n", nargs="?", type=int, default=10)
    ls.add_argument("--unread", action="store_true")
    s = sub.add_parser("search")
    s.add_argument("words", nargs="+")
    r = sub.add_parser("read")
    r.add_argument("id")
    r.add_argument("--save", action="store_true")
    rp = sub.add_parser("reply")
    rp.add_argument("id")
    sd = sub.add_parser("send")
    sd.add_argument("to")
    sd.add_argument("subject")
    sub.add_parser("disconnect")
    a = p.parse_args()
    socket.setdefaulttimeout(TIMEOUT)
    {"connect": cmd_connect, "finish": cmd_finish, "status": cmd_status, "list": cmd_list, "search": cmd_search,
     "read": cmd_read, "reply": cmd_reply, "send": cmd_send, "disconnect": cmd_disconnect}[a.cmd](a)


if __name__ == "__main__":
    main()
