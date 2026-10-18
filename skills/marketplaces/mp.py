#!/usr/bin/env python3
"""OINOA marketplaces: the seller's own Ozon and Wildberries shops through their seller APIs.

  connect ozon|wb [--chat ID --msg ID[,ID]]      the key on stdin (Ozon: Client ID and API key)
  status | forget ozon|wb
  orders  [ozon|wb]                              orders to pack and hand over, with deadlines
  stock   [ozon|wb] [--low N] [--all]            stock per product, low ones flagged
  sales   [ozon|wb] [--days N]                   sales for the last N days, approximate
  reviews [ozon|wb] [--limit N]                  unanswered reviews and buyer questions
  reply ID [-]                                   the answer text on stdin
  price ID NEW_PRICE [--market ozon|wb] [--old-price N] [--discount N] [--yes]
  --json on orders, stock, sales and reviews prints the raw API answers.
  --selftest                                     offline test against a local stub server

Python 3 stdlib only. Every answer is a few short lines. A line that starts with ERROR
names what went wrong and what to tell the person. Keys never appear in the output,
in argv or in the chat log: they come in on stdin and live in one 0600 file.

APIs (checked against the official docs, 2026-10-18):
  Ozon Seller API  https://docs.ozon.ru/api/seller/  host api-seller.ozon.ru, headers Client-Id + Api-Key
  Wildberries API  https://dev.wildberries.ru/openapi/  one host per category, header Authorization
"""
import argparse
import base64
import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone

HOME = os.path.expanduser("~")
# ~/.oinoa first; ~/.config/oinoa-marketplaces when ~/.oinoa is not writable.
STORES = [os.environ["OINOA_MP_DIR"]] if os.environ.get("OINOA_MP_DIR") else [
    os.path.join(HOME, ".oinoa"), os.path.join(HOME, ".config", "oinoa-marketplaces")]
ENV_NAME = "marketplaces.env"
TG_DIR = os.environ.get("TELEGRAM_STATE_DIR") or os.path.join(HOME, ".claude", "channels", "telegram")
TG_API = os.environ.get("OINOA_TG_API", "https://api.telegram.org")
TIMEOUT = 30          # seconds, every request
RETRY_CAP = 30        # a 429 that asks for a longer wait is reported, not waited out
POLL = float(os.environ.get("OINOA_MP_POLL", "2"))  # pause between WB price status checks

OZON_API = os.environ.get("OINOA_OZON_API", "https://api-seller.ozon.ru")
# Wildberries: each token category has its own host. OINOA_WB_API points all of them at one place (tests).
WB_HOSTS = {
    "marketplace": "https://marketplace-api.wildberries.ru",
    "statistics": "https://statistics-api.wildberries.ru",
    "analytics": "https://seller-analytics-api.wildberries.ru",
    "feedbacks": "https://feedbacks-api.wildberries.ru",
    "prices": "https://discounts-prices-api.wildberries.ru",
}
# The category names the owner ticks when making a WB token.
WB_CAT_RU = {"marketplace": "Маркетплейс", "statistics": "Статистика", "analytics": "Аналитика",
             "feedbacks": "Вопросы и отзывы", "prices": "Цены и скидки"}
# Bits of the "s" claim in a WB token (dev.wildberries.ru/openapi/api-information, Token fields).
WB_BITS = {1: "content", 2: "analytics", 3: "prices", 4: "marketplace", 5: "statistics", 6: "promotion",
           7: "feedbacks", 9: "chat", 10: "supplies", 11: "returns", 12: "documents", 13: "finance", 16: "users"}
WB_READONLY_BIT = 30
WB_KINDS = {1: "base", 2: "test", 3: "personal", 4: "service"}
NAMES = {"ozon": "Ozon", "wb": "Wildberries"}
REVOKE = {"ozon": "Ozon: seller.ozon.ru, Настройки, Seller API (https://seller.ozon.ru/app/settings/api-keys)",
          "wb": "Wildberries: seller.wildberries.ru, профиль, «Интеграции по API» (старое меню: Настройки, Доступ к API)"}

UTC = timezone.utc
MSK = timezone(timedelta(hours=3), "MSK")


def out(*lines):
    print("\n".join(str(x) for x in lines if x is not None))


def fail(code, text, extra=()):
    out(f"ERROR {code}: {text}", *extra)
    sys.exit(2)


# ---- the stored keys ----------------------------------------------------

def store_path():
    for d in STORES:
        path = os.path.join(d, ENV_NAME)
        if os.path.exists(path):
            return path
    return os.path.join(STORES[0], ENV_NAME)


def load():
    values = {}
    try:
        with open(store_path()) as f:
            for line in f:
                key, sep, value = line.rstrip("\n").partition("=")
                if sep and key.strip():
                    values[key.strip()] = value
    except FileNotFoundError:
        pass
    return values


def save(values):
    """Write all values to one 0600 file in a 0700 folder; drop a copy left in the other place."""
    clean = {k: re.sub(r"[\r\n]", " ", str(v)).strip() for k, v in values.items() if v not in (None, "")}
    has_secret = any(k in clean for k in ("OZON_API_KEY", "WB_TOKEN"))
    for store in STORES:
        path = os.path.join(store, ENV_NAME)
        try:
            if not has_secret:
                if os.path.exists(path):
                    os.remove(path)
                continue
            os.makedirs(store, mode=0o700, exist_ok=True)
            os.chmod(store, 0o700)
            tmp = path + ".tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write("# OINOA marketplaces. Secret: never print, copy or send this file.\n")
                for k in sorted(clean):
                    f.write(f"{k}={clean[k]}\n")
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except OSError:
            continue
        for other in STORES:
            stale = os.path.join(other, ENV_NAME)
            if other != store and os.path.exists(stale):
                try:
                    os.remove(stale)
                except OSError:
                    pass
        return
    if has_secret:
        fail("cannot_save", "the key works, but it could not be kept on this agent. Nothing is stored; "
             "tell them it will be fixed and to try again later.")


def connected(cfg):
    return [m for m, k in (("ozon", "OZON_API_KEY"), ("wb", "WB_TOKEN")) if cfg.get(k)]


def days_left(iso):
    dt = parse_ts(iso)
    return None if dt is None else (dt - datetime.now(UTC)).days


# ---- Telegram: the message with the key leaves the chat ----------------

def forget_message(chat, msgs, secrets):
    deleted, tried = 0, 0
    token = ""
    try:
        with open(os.path.join(TG_DIR, ".env")) as f:
            for line in f:
                if line.startswith("TELEGRAM_BOT_TOKEN="):
                    token = line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    for msg in msgs:
        if not (token and chat and msg):
            continue
        tried += 1
        try:
            data = urllib.parse.urlencode({"chat_id": chat, "message_id": msg}).encode()
            with urllib.request.urlopen(f"{TG_API}/bot{token}/deleteMessage", data, timeout=15) as r:
                deleted += json.load(r).get("ok") is True
        except Exception:
            pass
    scrub_log(set(msgs), [s for s in secrets if s and len(s) >= 8])
    if tried and deleted == tried:
        return "Chat: the message with the key is deleted."
    return "Chat: could not delete the message with the key. Ask them to delete it now."


def scrub_log(msgs, secrets):
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
            hit = str(row.get("message_id")) in msgs and row.get("dir") == "in"
            hit = hit or any(s in text for s in secrets)
            if hit:
                row["text"] = "[deleted: marketplace key]"
                lines[i] = json.dumps(row, ensure_ascii=False) + "\n"
                changed = True
        if changed:
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                f.writelines(lines)
            os.chmod(tmp, os.stat(path).st_mode & 0o777)
            os.replace(tmp, path)


# ---- HTTP -----------------------------------------------------------------

class Http(Exception):
    """A non-2xx answer. explain() turns it into what to tell the person."""

    def __init__(self, market, cat, code, body, headers, what):
        super().__init__(code)
        self.market, self.cat, self.code, self.what = market, cat, code, what
        self.headers = headers or {}
        self.body = body or ""

    def message(self):
        try:
            j = json.loads(self.body)
        except ValueError:
            return re.sub(r"\s+", " ", self.body)[:200]
        if isinstance(j, dict):
            parts = [j.get(k) for k in ("message", "detail", "errorText", "title")]
            text = next((str(p) for p in parts if p), "")
            return text[:200]
        return str(j)[:200]

    def retry_after(self):
        for h in ("Retry-After", "X-Ratelimit-Retry"):
            try:
                return float(self.headers.get(h))
            except (TypeError, ValueError):
                continue
        return 2.0


class Net(Exception):
    def __init__(self, market, err):
        super().__init__(str(err))
        self.market, self.err = market, err


def request(market, cat, method, url, headers, body=None, params=None, what="", retry=True):
    """One call with a timeout. A 429 waits once (the server's own hint, else 2 s) and retries."""
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = None
    headers = dict(headers, **{"Accept": "application/json", "User-Agent": "oinoa-marketplaces/1.0"})
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    for attempt in (1, 2):
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                raw, status = r.read(), r.status
            if not raw.strip():
                return {}
            try:
                return json.loads(raw)
            except ValueError:
                raise Http(market, cat, status, raw[:300].decode("utf-8", "replace"), {}, what)
        except urllib.error.HTTPError as e:
            err = Http(market, cat, e.code, e.read(4000).decode("utf-8", "replace"), e.headers, what)
            if e.code == 429 and attempt == 1 and retry:
                wait = err.retry_after()
                if wait <= RETRY_CAP:
                    time.sleep(max(1.0, wait))
                    continue
            raise err
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as e:
            raise Net(market, getattr(e, "reason", e))


def ozon(cfg, path, body, what="", retry=True):
    return request("ozon", "", "POST", OZON_API + path,
                   {"Client-Id": cfg["OZON_CLIENT_ID"], "Api-Key": cfg["OZON_API_KEY"]}, body=body,
                   what=what, retry=retry)


def wb_base(cat):
    return os.environ.get(f"OINOA_WB_{cat.upper()}_API") or os.environ.get("OINOA_WB_API") or WB_HOSTS[cat]


def wb(cfg, cat, method, path, params=None, body=None, what="", retry=True):
    return request("wb", cat, method, wb_base(cat) + path, {"Authorization": cfg["WB_TOKEN"]},
                   body=body, params=params, what=what, retry=retry)


def explain(e):
    """(code, text) for an Http or Net error, in words the agent can pass on."""
    name = NAMES[e.market]
    if isinstance(e, Net):
        return "network", f"could not reach {name} ({str(e.err)[:120]}). Check again in a minute; if it keeps failing, say {name} is not answering from this server."
    msg, code = e.message(), e.code
    low = msg.lower()
    if code == 429:
        wait = e.retry_after()
        when = f"about {int(wait)} seconds" if wait < 120 else f"about {int(wait // 60)} minutes"
        hint = ""
        if e.market == "wb":
            hint = " A WB base token gets only a few such requests per hour; a personal token gets far more."
        return "rate_limited", f"{name} allows this request again in {when}. Tell them and try later.{hint}"
    if code >= 500 or "circle is open" in low:
        return f"{e.market}_down", f"{name} is not answering properly right now ({code}). Try again in a few minutes."
    if e.market == "ozon":
        if "deactivated" in low:
            return "ozon_key_expired", ("the Ozon key is switched off or expired (Ozon keys live 3 months). "
                                        "They make a new one in Настройки, Seller API, and you connect it again.")
        if "invalid api-key" in low or "client-id" in low or code == 401:
            return "ozon_key", (f"Ozon did not accept the key ({msg}). Check that the Client ID and the key are "
                                "from the same shop; or make a new key in Настройки, Seller API, and connect again.")
        if "required role" in low:
            return "ozon_role", (f"the Ozon key has no role for {e.what or 'this'}. They make a key with the Admin "
                                 "role (Admin read only cannot change prices or reply) and you connect it again.")
        if "specific ip" in low:
            return "ozon_ip", ("the Ozon key works only from certain IP addresses. In Настройки, Seller API, "
                               "they remove that limit from the key, or make a key without it.")
        if "offer not signed" in low:
            return "ozon_offer", "Ozon wants the seller to accept the current offer in the seller cabinet first."
        if code == 403 and e.what in ("reviews", "questions", "replies"):
            return "ozon_plan", plan_text(e.what, msg)
        if code == 403:
            return "ozon_forbidden", (f"Ozon refused ({msg}): the key lacks a role for {e.what or 'this'}, "
                                      "or the shop's plan does not include it.")
        return "ozon_rejected", f"Ozon did not accept the request ({code}: {msg})."
    cat = WB_CAT_RU.get(e.cat, e.cat)
    if code == 401:
        return "wb_token", (f"Wildberries did not accept the token for «{cat}» ({msg}). The token is wrong, expired "
                            f"(WB tokens live 180 days) or lacks the «{cat}» category. They make a new one and you connect it again.")
    if code == 403:
        return "wb_forbidden", (f"Wildberries refused «{cat}» ({msg}): the token lacks this category, is read-only "
                                "for a change, or its type cannot use this method. A personal token with «Чтение и запись» covers all of it.")
    if code == 402:
        return "wb_payment", f"Wildberries answered 402 ({msg}). This happens only with service tokens; use a personal token."
    return "wb_rejected", f"Wildberries did not accept the request ({code}: {msg})."


def plan_text(what, msg):
    if what == "questions":
        return (f"Ozon gives buyer questions through the API only with the Premium Plus subscription ({msg}). "
                "Tell them; they can paste a question here and you draft the answer.")
    return (f"Ozon gives reviews through the API only with the «Управление отзывами» or Premium Pro subscription ({msg}). "
            "Tell them; they can paste a review here and you draft the answer.")


# ---- small helpers ----------------------------------------------------------

def parse_ts(s):
    """ISO time from either API. No zone means Moscow time (WB statistics)."""
    if not s:
        return None
    m = re.match(r"(\d{4}-\d{2}-\d{2})(?:[T ](\d{2}:\d{2}(?::\d{2})?))?(?:\.\d+)?(Z|[+-]\d{2}:?\d{2})?", str(s))
    if not m:
        return None
    clock = (m.group(2) or "00:00:00")
    clock = clock if clock.count(":") == 2 else clock + ":00"
    dt = datetime.strptime(m.group(1) + "T" + clock, "%Y-%m-%dT%H:%M:%S")
    tz = m.group(3)
    if not tz:
        return dt.replace(tzinfo=MSK)
    if tz == "Z":
        return dt.replace(tzinfo=UTC)
    sign = 1 if tz[0] == "+" else -1
    hh, mm = int(tz[1:3]), int(tz[-2:])
    return dt.replace(tzinfo=timezone(sign * timedelta(hours=hh, minutes=mm)))


def msk(dt):
    return dt.astimezone(MSK).strftime("%d.%m %H:%M MSK") if dt else "?"


def ago(dt):
    if not dt:
        return "?"
    mins = int((datetime.now(UTC) - dt).total_seconds() // 60)
    if mins < 60:
        return f"{max(mins, 0)} min ago"
    if mins < 48 * 60:
        return f"{mins // 60} h ago"
    return f"{mins // 1440} d ago"


def num(x):
    try:
        x = float(x)
    except (TypeError, ValueError):
        return "?"
    if abs(x - round(x)) < 0.005:
        return f"{int(round(x)):,}".replace(",", " ")
    return f"{x:,.2f}".replace(",", " ")


def money(x, cur="RUB"):
    return f"{num(x)} {'₽' if cur in ('RUB', '', None) else cur}"


def clip(text, n=300):
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text if len(text) <= n else text[:n - 1] + "…"


def parse_price(text):
    t = str(text).replace(" ", "").replace(" ", "").replace(",", ".").rstrip("₽руб.").strip()
    try:
        v = float(t)
    except ValueError:
        fail("bad_price", f"«{text}» is not a price. Give a number, like 1290 or 1290.50.")
    if v <= 0:
        fail("bad_price", "the price must be above zero.")
    return v


def jwt_claims(token):
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        claims = json.loads(base64.urlsafe_b64decode(part))
        return claims if isinstance(claims, dict) else {}
    except Exception:
        return {}


def wb_scopes(claims):
    try:
        s = int(claims.get("s"))
    except (TypeError, ValueError):
        return None, None
    return [name for bit, name in WB_BITS.items() if s & (1 << bit)], bool(s & (1 << WB_READONLY_BIT))


def pick(a, cfg):
    have = connected(cfg)
    if getattr(a, "market", None):
        if a.market not in have:
            fail("not_connected", f"{NAMES[a.market]} is not connected. Offer to connect it (marketplaces skill, Connect).")
        return [a.market]
    if not have:
        fail("not_connected", "no shop is connected. Offer to connect Ozon or Wildberries (marketplaces skill, Connect), "
             "or ask them to paste what they need help with.")
    return have


def run(a, sections):
    """Run one section per shop. One shop failing does not hide the other's answer."""
    cfg = load()
    lines, raw, failed = [], {}, False
    for m in pick(a, cfg):
        try:
            part, data = sections[m](a, cfg)
            lines += part
            raw[m] = data
        except (Http, Net) as e:
            code, text = explain(e)
            lines.append(f"ERROR {code}: {text}")
            raw[m] = {"error": code, "text": text}
            failed = True
    if getattr(a, "json", False):
        print(json.dumps(raw, ensure_ascii=False, indent=1))
    else:
        out(*lines)
    if failed:
        sys.exit(2)


def chunks(seq, n):
    seq = list(seq)
    return [seq[i:i + n] for i in range(0, len(seq), n)]


def ozon_names(cfg, key, ids):
    """product_id or sku -> product name, 1000 per call (/v3/product/info/list)."""
    names = {}
    for part in chunks(sorted({str(i) for i in ids if i}), 1000):
        try:
            r = ozon(cfg, "/v3/product/info/list", {key: part}, what="product names")
        except Http:
            return names  # names are a nicety; the numbers still go out
        for it in r.get("items") or []:
            ident = it.get("id") if key == "product_id" else it.get("sku")
            names[str(ident)] = it.get("name") or ""
    return names


# ---- connect / status / forget ----------------------------------------------

UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
JWT = re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")


def cmd_connect(a):
    raw = "" if sys.stdin.isatty() else sys.stdin.read()
    msgs = [m.strip() for item in (a.msg or []) for m in item.split(",") if m.strip()]
    if a.market == "ozon":
        found = UUID.search(raw)
        key = found.group(0) if found else ""
        rest = UUID.sub(" ", raw)
        cid = (a.client_id or "").strip()
        if not cid:
            digits = re.findall(r"(?<![\w-])\d{3,12}(?![\w-])", rest)
            cid = digits[0] if digits else ""
    else:
        found = JWT.search(raw)
        key = found.group(0) if found else max(re.findall(r"\S{40,}", raw) or [""], key=len)
        cid = ""
    note = forget_message(a.chat, msgs, [raw.strip(), key]) if (a.chat or msgs or raw) else ""
    tail = [note] if note else []
    if a.market == "ozon":
        return connect_ozon(cid, key, tail)
    return connect_wb(key, tail)


def connect_ozon(cid, key, tail):
    if not key:
        fail("no_key", "no Ozon API key came in on stdin. It looks like 8-4-4-4-12 letters and digits; "
             "run connect with their message in the <<'EOF' block.", tail)
    if not cid.isdigit():
        fail("no_client_id", "the Client ID is missing: the number shown on the Seller API page next to the keys. "
             "Ask for it and run connect again with the key and the Client ID together.", tail)
    cfg = {"OZON_CLIENT_ID": cid, "OZON_API_KEY": key}
    try:
        roles = ozon(cfg, "/v1/roles", {}, what="checking the key")
    except (Http, Net) as e:
        code, text = explain(e)
        fail(code, text + " Nothing is stored.", tail)
    names = [r.get("name", "") for r in roles.get("roles") or [] if r.get("name")]
    shop = ""
    try:
        info = ozon(cfg, "/v1/seller/info", {}, what="shop name")
        shop = ((info.get("company") or {}).get("name") or "").strip()
    except (Http, Net):
        pass
    values = load()
    replaced = values.get("OZON_SHOP") if values.get("OZON_API_KEY") and values.get("OZON_CLIENT_ID") != cid else ""
    values.update({"OZON_CLIENT_ID": cid, "OZON_API_KEY": key, "OZON_EXPIRES": roles.get("expires_at", ""),
                   "OZON_ROLES": ",".join(names), "OZON_SHOP": shop})
    save(values)
    left = days_left(roles.get("expires_at"))
    until = f" Key valid until {roles['expires_at'][:10]} ({left} days)." if left is not None else ""
    lines = [f"OK connected Ozon{' «' + shop + '»' if shop else ''}. Roles: {', '.join(names) or 'none listed'}.{until}"]
    if replaced:
        lines.append(f"It replaces the shop «{replaced}».")
    if names and all("read" in n.lower() for n in names):
        lines.append("The key is read-only: reports work, price changes and replies will not.")
    out(*lines, *tail)


def connect_wb(token, tail):
    if not token:
        fail("no_token", "no Wildberries token came in on stdin. It is one long line that starts with eyJ; "
             "run connect with their message in the <<'EOF' block.", tail)
    claims = jwt_claims(token)
    kind = WB_KINDS.get(claims.get("acc"), "")
    if kind == "test":
        fail("test_token", "that is a test token: it sees only the WB sandbox, not the shop. "
             "They make a Personal token (Ручная интеграция, Персональный) and send it.", tail)
    scopes, readonly = wb_scopes(claims)
    cfg = {"WB_TOKEN": token}
    works, missing, last = [], [], None
    for cat in ("marketplace", "statistics", "analytics", "feedbacks", "prices"):
        if scopes is not None and cat not in scopes:
            missing.append(cat)
            continue
        try:
            wb(cfg, cat, "GET", "/ping", what="checking the token")
            works.append(cat)
        except Http as e:
            last = e
            if e.code in (401, 403):
                missing.append(cat)
            else:
                works.append(cat)  # the token passed; the host had a hiccup
        except Net as e:
            code, text = explain(e)
            fail(code, text + " Nothing is stored.", tail)
    if not works:
        detail = f" ({last.message()})" if last is not None else ""
        fail("wb_token", f"Wildberries did not accept the token{detail}. It is wrong, expired or has none of the needed "
             "categories. They make a new one and send it. Nothing is stored.", tail)
    exp = ""
    if isinstance(claims.get("exp"), (int, float)):
        exp = datetime.fromtimestamp(claims["exp"], UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    values = load()
    values.update({"WB_TOKEN": token, "WB_EXPIRES": exp, "WB_KIND": kind, "WB_SCOPES": ",".join(works),
                   "WB_READONLY": "1" if readonly else ""})
    save(values)
    left = days_left(exp)
    lines = [f"OK connected Wildberries ({kind or 'unknown type'} token"
             + (f", valid until {exp[:10]}, {left} days" if left is not None else "") + ")."
             + f" Works: {', '.join(WB_CAT_RU[c] for c in works)}."]
    if missing:
        lines.append(f"Missing categories: {', '.join(WB_CAT_RU[c] for c in missing)}. "
                     "Those commands will not work until they make a token with them.")
    if readonly:
        lines.append("The token is read-only: reports work, price changes and replies will not.")
    if kind == "base":
        lines.append("Base token: WB gives it very few requests (sales once in 2 hours, reviews 5 an hour). "
                     "A personal token is better.")
    out(*lines, *tail)


def cmd_status(a):
    cfg = load()
    lines = []
    if cfg.get("OZON_API_KEY"):
        left = days_left(cfg.get("OZON_EXPIRES"))
        shop = f" «{cfg['OZON_SHOP']}»" if cfg.get("OZON_SHOP") else ""
        lines.append(f"Ozon: connected{shop}. Roles: {cfg.get('OZON_ROLES') or '?'}."
                     + (f" Key valid {left} more days." if left is not None else ""))
        if left is not None and left < 14:
            lines.append("Ozon key ends soon: they make a new one (Настройки, Seller API) and you connect it again.")
    else:
        lines.append("Ozon: not connected.")
    if cfg.get("WB_TOKEN"):
        left = days_left(cfg.get("WB_EXPIRES"))
        scopes = [WB_CAT_RU.get(c, c) for c in (cfg.get("WB_SCOPES") or "").split(",") if c]
        lines.append(f"Wildberries: connected ({cfg.get('WB_KIND') or '?'} token). Works: {', '.join(scopes) or '?'}."
                     + (" Read-only." if cfg.get("WB_READONLY") else "")
                     + (f" Token valid {left} more days." if left is not None else ""))
        if left is not None and left < 14:
            lines.append("WB token ends soon: they make a new one (Интеграции по API) and you connect it again.")
    else:
        lines.append("Wildberries: not connected.")
    out(*lines)


def cmd_forget(a):
    values = load()
    keys = [k for k in values if k.startswith("OZON_" if a.market == "ozon" else "WB_")]
    if not any(k in ("OZON_API_KEY", "WB_TOKEN") for k in keys):
        out(f"{NAMES[a.market]}: not connected.")
        return
    for k in keys:
        values.pop(k, None)
    save(values)
    out(f"OK forgot {NAMES[a.market]}. This agent can no longer reach that shop.",
        f"To switch the key off everywhere, they delete it here: {REVOKE[a.market]}")


# ---- orders ------------------------------------------------------------------

OZON_STATUS = {"awaiting_packaging": "to pack", "awaiting_deliver": "packed, to hand over"}


def orders_ozon(a, cfg):
    # /v4/posting/fbs/unfulfilled/list: FBS postings waiting for the seller; shipment_date is the ship-by time.
    t = datetime.now(UTC)
    body = {"filter": {"cutoff_from": (t - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                       "cutoff_to": (t + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                       "statuses": list(OZON_STATUS)},
            "limit": 100, "sort_dir": "ASC", "cursor": ""}
    postings, pages = [], []
    for _ in range(5):
        r = ozon(cfg, "/v4/posting/fbs/unfulfilled/list", body, what="orders")
        pages.append(r)
        postings += r.get("postings") or []
        if not r.get("has_next") or not r.get("cursor"):
            break
        body["cursor"] = r["cursor"]
    if not postings:
        return ["Ozon: no orders waiting to be packed or handed over (FBS)."], pages
    postings.sort(key=lambda p: p.get("shipment_date") or "")
    late = sum(1 for p in postings if (parse_ts(p.get("shipment_date")) or t) < t)
    items = sum(int(x.get("quantity") or 0) for p in postings for x in p.get("products") or [])
    lines = [f"Ozon: {len(postings)} orders to ship ({items} items)" + (f", {late} overdue" if late else "")
             + ". posting | status | ship by | items"]
    for p in postings:
        when = parse_ts(p.get("shipment_date"))
        flag = ", OVERDUE" if when and when < t else ""
        goods = "; ".join(f"{clip(x.get('name'), 60)} x{x.get('quantity')} ({x.get('offer_id')})"
                          for x in p.get("products") or [])
        lines.append(f"{p.get('posting_number')} | {OZON_STATUS.get(p.get('status'), p.get('status'))} | "
                     f"ship by {msk(when)}{flag} | {goods}")
    return lines, pages


def orders_wb(a, cfg):
    # /api/v3/orders/new: FBS assembly orders not yet put into a supply. One order is one unit.
    r = wb(cfg, "marketplace", "GET", "/api/v3/orders/new", what="orders")
    orders = r.get("orders") or []
    if not orders:
        return ["Wildberries: no new orders (FBS)."], r
    groups = {}
    for o in orders:
        key = (o.get("article") or "?", o.get("nmId"))
        groups.setdefault(key, []).append(o)
    lines = [f"Wildberries: {len(orders)} new orders, not yet in a supply. WB sets no ship-by time in the API; "
             "times are when the order came in. article (nmID) | how many | oldest"]
    for (article, nm), group in sorted(groups.items(), key=lambda kv: min(o.get("createdAt") or "" for o in kv[1])):
        first = min((parse_ts(o.get("createdAt")) for o in group if o.get("createdAt")), default=None)
        odc = sorted({o.get("sellerDate") for o in group if o.get("sellerDate")})
        lines.append(f"{article} (nmID {nm}) | x{len(group)} | came in {msk(first)} ({ago(first)})"
                     + (f" | oversized: bring to WB by {odc[0]}" if odc else ""))
    return lines, r


# ---- stock ---------------------------------------------------------------------

def stock_ozon(a, cfg):
    # /v4/product/info/stocks: present and reserved per warehouse type (fbo = Ozon's, fbs/rfbs = the seller's).
    body = {"filter": {"visibility": "ALL"}, "limit": 1000, "cursor": ""}
    items, pages = [], []
    for _ in range(5):
        r = ozon(cfg, "/v4/product/info/stocks", body, what="stock")
        pages.append(r)
        got = r.get("items") or []
        items += got
        if len(got) < 1000 or not r.get("cursor"):
            break
        body["cursor"] = r["cursor"]
    if not items:
        return ["Ozon: no products found."], pages
    names = ozon_names(cfg, "product_id", [i.get("product_id") for i in items])
    rows = []
    for it in items:
        per, reserved = {}, 0
        for s in it.get("stocks") or []:
            free = max(0, int(s.get("present") or 0) - int(s.get("reserved") or 0))
            kind = (s.get("type") or "?").upper()
            per[kind] = per.get(kind, 0) + free
            reserved += int(s.get("reserved") or 0)
        total = sum(per.values())
        where = ", ".join(f"{k} {v}" for k, v in sorted(per.items())) or "none"
        name = names.get(str(it.get("product_id"))) or it.get("offer_id")
        rows.append((total, f"{clip(name, 70)} ({it.get('offer_id')}) | {total} available | {where}"
                            + (f" | {reserved} reserved" if reserved else "")
                            + (" | LOW" if total <= a.low else "")))
    rows.sort(key=lambda x: x[0])
    low = sum(1 for t, _ in rows if t <= a.low)
    shown = rows if a.all else rows[:max(30, low)]
    lines = [f"Ozon: {len(rows)} products, {low} at or below {a.low} units. product (article) | available | where"]
    lines += [line for _, line in shown]
    if len(shown) < len(rows):
        lines.append(f"... {len(rows) - len(shown)} more with more stock; stock ozon --all lists them.")
    lines.append("Available is on hand minus reserved. FBO is Ozon's warehouses, FBS your own.")
    return lines, pages


def stock_wb(a, cfg):
    # /api/v2/stocks-report/products/products: WB's stock report, all warehouses, refreshed every 2 hours.
    today = datetime.now(MSK).date()
    body = {"currentPeriod": {"start": (today - timedelta(days=6)).isoformat(), "end": today.isoformat()},
            "stockType": "", "skipDeletedNm": True, "orderBy": {"field": "stockCount", "mode": "asc"},
            "availabilityFilters": ["deficient", "actual", "balanced", "nonActual", "nonLiquid", "invalidData"],
            "limit": 1000, "offset": 0}
    items, pages = [], []
    for _ in range(5):
        r = wb(cfg, "analytics", "POST", "/api/v2/stocks-report/products/products", body=body, what="stock")
        pages.append(r)
        got = ((r.get("data") or {}).get("items")) or []
        items += got
        if len(got) < body["limit"]:
            break
        body["offset"] += body["limit"]
    if not items:
        return ["Wildberries: no products in the stock report."], pages
    rows = []
    for it in items:
        m = it.get("metrics") or {}
        total = int(m.get("stockCount") or 0)
        rate = m.get("saleRate") or {}
        lasts = f" | lasts ~{rate['days']} d" if isinstance(rate.get("days"), int) and rate.get("hours", 0) >= 0 else ""
        flags = [f for f, on in (("LOW", total <= a.low), ("WB: low stock", m.get("availability") == "deficient")) if on]
        avg = m.get("avgOrders")
        rows.append((total, f"{clip(it.get('name'), 70)} ({it.get('vendorCode')}, nmID {it.get('nmID')}) | {total} in stock"
                            + (f" | ~{num(avg)} orders/day" if avg else "") + lasts
                            + (" | " + ", ".join(flags) if flags else "")))
    rows.sort(key=lambda x: x[0])
    low = sum(1 for t, _ in rows if t <= a.low)
    shown = rows if a.all else rows[:max(30, low)]
    lines = [f"Wildberries: {len(rows)} products, {low} at or below {a.low} units (WB refreshes this every 2 hours, "
             "WB and your own warehouses together)."]
    lines += [line for _, line in shown]
    if len(shown) < len(rows):
        lines.append(f"... {len(rows) - len(shown)} more with more stock; stock wb --all lists them.")
    return lines, pages


# ---- sales -----------------------------------------------------------------------

def sales_ozon(a, cfg):
    days = min(a.days, 90)  # without Premium Plus Ozon analytics covers the last 3 months
    today = datetime.now(MSK).date()
    start = today - timedelta(days=days - 1)
    lines = [f"Ozon, last {days} days ({start:%d.%m} to {today:%d.%m}), approximate:"]
    raw = {}
    # /v1/analytics/data: «Аналитика, Продажи моих товаров». Ordered, not paid: cancellations and returns come later.
    r = ozon(cfg, "/v1/analytics/data", {
        "date_from": start.isoformat(), "date_to": today.isoformat(), "metrics": ["revenue", "ordered_units"],
        "dimension": ["sku"], "sort": [{"key": "revenue", "order": "DESC"}], "limit": 1000, "offset": 0},
        what="sales analytics", retry=False)
    raw["analytics"] = r
    res = r.get("result") or {}
    totals = res.get("totals") or [0, 0]
    lines.append(f"Ordered: {money(totals[0])}, {num(totals[1] if len(totals) > 1 else 0)} units "
                 "(includes orders later cancelled or returned).")
    top = []
    for row in (res.get("data") or [])[:5]:
        dims, met = row.get("dimensions") or [{}], row.get("metrics") or [0, 0]
        top.append(f"{clip(dims[0].get('name') or dims[0].get('id'), 50)} {money(met[0])} / {num(met[1] if len(met) > 1 else 0)} pcs")
    if top:
        lines.append("Top: " + "; ".join(top))
    # /v1/finance/balance (beta): «Финансы, Баланс». At most 30 days per call, so longer periods go in parts.
    sums = {"sales": 0.0, "paid": 0.0, "fee": 0.0, "returns": 0.0, "services": 0.0, "accrued": 0.0}
    cur, parts = "RUB", []
    try:
        d = start
        while d <= today:
            end = min(d + timedelta(days=29), today)
            b = ozon(cfg, "/v1/finance/balance", {"date_from": d.isoformat(), "date_to": end.isoformat()},
                     what="finance balance", retry=False)
            parts.append(b)
            cf = b.get("cashflows") or {}
            sales = cf.get("sales") or {}
            sums["sales"] += float((sales.get("amount") or {}).get("value") or 0)
            sums["paid"] += float(((sales.get("amount_details") or {}).get("revenue") or {}).get("value") or 0)
            sums["fee"] += float((sales.get("fee") or {}).get("value") or 0)
            sums["returns"] += float(((cf.get("returns") or {}).get("amount") or {}).get("value") or 0)
            sums["services"] += sum(float((s.get("amount") or {}).get("value") or 0) for s in cf.get("services") or [])
            sums["accrued"] += float((((b.get("total") or {}).get("accrued")) or {}).get("value") or 0)
            cur = (((b.get("total") or {}).get("accrued")) or {}).get("currency_code") or cur
            d = end + timedelta(days=1)
        lines.append(f"Finance, Balance: sales {money(sums['sales'], cur)} (buyers paid {money(sums['paid'], cur)}), "
                     f"Ozon fee {money(sums['fee'], cur)}, returns {money(sums['returns'], cur)}, "
                     f"other services {money(sums['services'], cur)}; accrued in total {money(sums['accrued'], cur)}.")
    except Http as e:
        if e.code in (401,) or "api-key" in e.message().lower():
            raise
        lines.append(f"Finance, Balance: not available through the API right now ({e.code}: {e.message()}). "
                     "Ozon marks this method beta.")
    raw["balance"] = parts
    lines.append("Approximate: Ozon counts ordered goods on the order day; money is final only in the "
                 "monthly realization report in the Ozon cabinet.")
    return lines, raw


def sales_wb(a, cfg):
    days = min(a.days, 90)  # WB keeps these rows 90 days
    today = datetime.now(MSK).date()
    start = today - timedelta(days=days - 1)
    lines = [f"Wildberries, last {days} days ({start:%d.%m} to {today:%d.%m}, Moscow time), approximate:"]
    # /api/v1/supplier/orders and /sales: WB's quick statistics; flag=0 returns rows changed since dateFrom.
    orders = wb(cfg, "statistics", "GET", "/api/v1/supplier/orders", params={"dateFrom": start.isoformat(), "flag": 0},
                what="orders statistics", retry=False) or []
    rows = [o for o in orders if str(o.get("date", ""))[:10] >= start.isoformat()]
    placed = [o for o in rows if not o.get("isCancel")]
    ordered = sum(float(o.get("priceWithDisc") or 0) for o in placed)
    lines.append(f"Orders: {len(placed)} for {money(ordered)} at your price, {len(rows) - len(placed)} cancelled.")
    top = {}
    for o in placed:
        k = f"{o.get('supplierArticle') or o.get('nmId')}"
        n, s = top.get(k, (0, 0.0))
        top[k] = (n + 1, s + float(o.get("priceWithDisc") or 0))
    if top:
        best = sorted(top.items(), key=lambda kv: -kv[1][1])[:5]
        lines.append("Top: " + "; ".join(f"{k} x{n} ({money(s)})" for k, (n, s) in best))
    raw = {"orders": orders}
    try:
        sales = wb(cfg, "statistics", "GET", "/api/v1/supplier/sales", params={"dateFrom": start.isoformat(), "flag": 0},
                   what="sales statistics", retry=False) or []
        srows = [s for s in sales if str(s.get("date", ""))[:10] >= start.isoformat()]
        sold = [s for s in srows if str(s.get("saleID", "")).startswith("S")]
        back = [s for s in srows if str(s.get("saleID", "")).startswith("R")]
        pay = sum(float(s.get("forPay") or 0) for s in sold) - sum(abs(float(s.get("forPay") or 0)) for s in back)
        lines.append(f"Bought out: {len(sold)} for {money(sum(float(s.get('priceWithDisc') or 0) for s in sold))}, "
                     f"returns {len(back)}. To be paid to you, about {money(pay)}.")
        raw["sales"] = sales
        if len(orders) >= 80000 or len(sales) >= 80000:
            lines.append("Only the first 80 000 rows were read; the real totals are higher.")
    except Http as e:
        if e.code != 429:
            raise
        lines.append("Bought out: WB allows the sales report again later (rate limit); orders above are complete.")
    lines.append("Approximate: WB's quick statistics, updated every 30 minutes; payouts can show 0 for up to 24 hours. "
                 "Exact money is in WB's weekly realization report.")
    return lines, raw


# ---- reviews and questions ---------------------------------------------------------

def reviews_ozon(a, cfg):
    lines, raw = [], {}
    try:
        revs = []
        for status in ("NEW", "VIEWED"):  # unprocessed; PROCESSED ones are answered or closed
            r = ozon(cfg, "/v2/review/list", {"filters": {"status": status}, "limit": 100, "sort_dir": "DESC"},
                     what="reviews")
            revs += r.get("reviews") or []
        revs = list({x.get("id"): x for x in revs}.values())
        revs.sort(key=lambda x: x.get("published_at") or "", reverse=True)
        raw["reviews"] = revs
        names = ozon_names(cfg, "sku", [x.get("sku") for x in revs[:a.limit]])
        lines.append(f"Ozon: {len(revs)} reviews not processed yet" + (f", newest {a.limit}" if len(revs) > a.limit else "")
                     + ". ID | stars | date | product | text")
        for x in revs[:a.limit]:
            when = parse_ts(x.get("published_at"))
            lines.append(f"oz:r:{x.get('id')} | {x.get('rating')}/5 | {msk(when)} | "
                         f"{clip(names.get(str(x.get('sku'))) or x.get('sku'), 50)} | {clip(x.get('text')) or '(no text)'}"
                         + (f" | {x.get('comments_amount')} comments" if x.get("comments_amount") else ""))
    except Http as e:
        if e.code != 403 or "role" in e.message().lower() or "api-key" in e.message().lower():
            raise
        lines.append("Ozon reviews: " + plan_text("reviews", e.message()))
    try:
        r = ozon(cfg, "/v1/question/list", {"filter": {"status": "UNPROCESSED"}, "limit": 100, "sort_dir": "DESC"},
                 what="questions")
        qs = r.get("questions") or []
        raw["questions"] = qs
        names = ozon_names(cfg, "sku", [q.get("sku") for q in qs[:a.limit]])
        lines.append(f"Ozon: {len(qs)} buyer questions without an answer" + (" (more on the next page)" if r.get("has_next") else "")
                     + ". ID | date | product | question")
        for q in qs[:a.limit]:
            lines.append(f"oz:q:{q.get('id')}:{q.get('sku')} | {msk(parse_ts(q.get('published_at')))} | "
                         f"{clip(names.get(str(q.get('sku'))) or q.get('sku'), 50)} | {clip(q.get('text'))}")
    except Http as e:
        if e.code != 403 or "role" in e.message().lower() or "api-key" in e.message().lower():
            raise
        lines.append("Ozon questions: " + plan_text("questions", e.message()))
    return lines, raw


def reviews_wb(a, cfg):
    q = {"isAnswered": "false", "take": 100, "skip": 0, "order": "dateDesc"}
    fb = wb(cfg, "feedbacks", "GET", "/api/v1/feedbacks", params=q, what="reviews")
    data = fb.get("data") or {}
    revs = data.get("feedbacks") or []
    count = data.get("countUnanswered", len(revs))
    lines = [f"Wildberries: {count} reviews without an answer" + (f", newest {min(a.limit, len(revs))}" if count > a.limit else "")
             + ". ID | stars | date | product | text"]
    for x in revs[:a.limit]:
        p = x.get("productDetails") or {}
        text = " ".join(t for t in (x.get("text"), f"Плюсы: {x['pros']}" if x.get("pros") else "",
                                    f"Минусы: {x['cons']}" if x.get("cons") else "") if t)
        lines.append(f"wb:r:{x.get('id')} | {x.get('productValuation')}/5 | {msk(parse_ts(x.get('createdDate')))} | "
                     f"{clip(p.get('productName'), 50)} ({p.get('supplierArticle')}) | {clip(text) or '(no text)'}")
    qs = wb(cfg, "feedbacks", "GET", "/api/v1/questions", params=q, what="questions")
    qd = qs.get("data") or {}
    items = qd.get("questions") or []
    qcount = qd.get("countUnanswered", len(items))
    lines.append(f"Wildberries: {qcount} buyer questions without an answer. ID | date | product | question")
    for x in items[:a.limit]:
        p = x.get("productDetails") or {}
        lines.append(f"wb:q:{x.get('id')} | {msk(parse_ts(x.get('createdDate')))} | "
                     f"{clip(p.get('productName'), 50)} ({p.get('supplierArticle')}) | {clip(x.get('text'))}")
    return lines, {"feedbacks": fb, "questions": qs}


def cmd_reply(a):
    text = "" if sys.stdin.isatty() else sys.stdin.read().strip()
    m = re.fullmatch(r"(oz|wb):(r|q):(.+)", a.id.strip())
    if not m:
        fail("bad_id", "the ID is the first field of a line from reviews, like oz:r:..., oz:q:...:SKU, wb:r:... or wb:q:...")
    if len(text) < 2:
        fail("no_text", "the answer goes in the <<'EOF' block, at least 2 characters.")
    market = "ozon" if m.group(1) == "oz" else "wb"
    cfg = load()
    if market not in connected(cfg):
        fail("not_connected", f"{NAMES[market]} is not connected.")
    kind, ident = m.group(2), m.group(3)
    try:
        if market == "ozon" and kind == "r":
            if len(text) > 5000:
                fail("too_long", "keep the answer under 5000 characters.")
            r = ozon(cfg, "/v1/review/comment/create",
                     {"review_id": ident, "text": text, "mark_review_as_processed": True}, what="replies")
            out(f"OK answered the Ozon review {ident} (comment {r.get('comment_id', '?')}). The review is marked processed.")
        elif market == "ozon":
            qid, _, sku = ident.rpartition(":")
            if not qid or not sku.isdigit():
                fail("bad_id", "an Ozon question ID looks like oz:q:QUESTION:SKU, exactly as reviews printed it.")
            if len(text) > 3000:
                fail("too_long", "Ozon takes answers to questions up to 3000 characters.")
            r = ozon(cfg, "/v1/question/answer/create", {"question_id": qid, "sku": int(sku), "text": text},
                     what="replies")
            out(f"OK answered the Ozon question {qid} (answer {r.get('answer_id', '?')}).")
        elif kind == "r":
            if len(text) > 5000:
                fail("too_long", "WB takes answers up to 5000 characters.")
            # WB does not check the ID when answering, so look the review up first.
            got = (wb(cfg, "feedbacks", "GET", "/api/v1/feedback", params={"id": ident}, what="replies").get("data") or {})
            if not got.get("id"):
                fail("not_found", f"no Wildberries review {ident}. Run reviews again and use an ID from it.")
            if (got.get("answer") or {}).get("text"):
                fail("already_answered", "that review already has an answer. Editing one is done in the WB cabinet.")
            wb(cfg, "feedbacks", "POST", "/api/v1/feedbacks/answer", body={"id": ident, "text": text}, what="replies")
            out(f"OK answered the Wildberries review {ident}. WB checks answers before they appear on the site.")
        else:
            got = (wb(cfg, "feedbacks", "GET", "/api/v1/question", params={"id": ident}, what="replies").get("data") or {})
            if not got.get("id"):
                fail("not_found", f"no Wildberries question {ident}. Run reviews again and use an ID from it.")
            if (got.get("answer") or {}).get("text"):
                fail("already_answered", "that question already has an answer. Editing one is done in the WB cabinet.")
            wb(cfg, "feedbacks", "PATCH", "/api/v1/questions",
               body={"id": ident, "answer": {"text": text}, "state": "wbRu"}, what="replies")
            out(f"OK answered the Wildberries question {ident}. WB checks answers before they appear on the site.")
    except (Http, Net) as e:
        code, text_ = explain(e)
        fail(code, text_ + " Nothing was sent.")


# ---- price -----------------------------------------------------------------------------

def cmd_price(a):
    cfg = load()
    have = connected(cfg)
    if not have:
        fail("not_connected", "no shop is connected. Offer to connect Ozon or Wildberries (marketplaces skill, Connect).")
    if a.market:
        market = a.market
        if market not in have:
            fail("not_connected", f"{NAMES[market]} is not connected.")
    elif len(have) == 1:
        market = have[0]
    elif not a.id.isdigit():
        market = "ozon"
    else:
        fail("which_market", "this number could be an Ozon article or a Wildberries nmID. "
             "Run it again with --market ozon or --market wb.")
    new = parse_price(a.new_price)
    try:
        (price_ozon if market == "ozon" else price_wb)(a, cfg, new)
    except (Http, Net) as e:
        code, text = explain(e)
        fail(code, text + " The price did not change.")


def change_note(old, new):
    try:
        old = float(old)
    except (TypeError, ValueError):
        return None
    if old > 0 and abs(new - old) / old > 0.5:
        return f"WARNING: that is {abs(new - old) / old:.0%} {'lower' if new < old else 'higher'} than now. Make sure the owner meant it."
    return None


def price_ozon(a, cfg, new):
    # /v5/product/info/prices to read, /v1/product/import/prices to write (at most 10 changes per product per hour).
    r = ozon(cfg, "/v5/product/info/prices", {"filter": {"offer_id": [a.id], "visibility": "ALL"}, "limit": 1, "cursor": ""},
             what="prices")
    items = r.get("items") or []
    if not items and a.id.isdigit():
        r = ozon(cfg, "/v5/product/info/prices", {"filter": {"product_id": [a.id], "visibility": "ALL"}, "limit": 1,
                                                  "cursor": ""}, what="prices")
        items = r.get("items") or []
    if not items:
        fail("not_found", f"no Ozon product with the article {a.id}. Use the seller's article (offer_id) from stock ozon.")
    it = items[0]
    p = it.get("price") or {}
    old, cur = p.get("price"), p.get("currency_code") or "RUB"
    crossed = float(p.get("old_price") or 0)
    # The crossed-out price (old_price) must stay above the price. It is sent only when the owner sets it.
    if a.old_price is None:
        keep = crossed
        crossed_txt = f"crossed-out {num(crossed)} stays" if crossed else "no crossed-out price"
    elif str(a.old_price).strip() in ("0", "0.0", "0,0"):
        keep = 0.0
        crossed_txt = "crossed-out price removed"
    else:
        keep = parse_price(a.old_price)
        crossed_txt = f"crossed-out {num(crossed)} → {num(keep)}"
    if keep and new >= keep:
        fail("old_price_too_low", f"Ozon needs the crossed-out price ({num(keep)}) to be above the new price. "
             "Ask the owner: raise it with --old-price N, or drop it with --old-price 0.")
    offer = it.get("offer_id") or a.id
    plan = f"Ozon {offer}: price {num(old)} → {num(new)} {cur}, {crossed_txt}."
    warn = change_note(old, new)
    if not a.yes:
        out(f"PREVIEW {plan}", warn,
            "Nothing changed. This is the price before Ozon promotions; buyers can see less during promotions. "
            "Show it to the owner; after a clear yes run the same command with --yes.")
        return
    row = {"offer_id": offer, "price": num(new).replace(" ", ""), "currency_code": cur}
    if a.old_price is not None:
        row["old_price"] = num(keep).replace(" ", "") if keep else "0"
    res = ozon(cfg, "/v1/product/import/prices", {"prices": [row]}, what="prices").get("result") or [{}]
    done = res[0]
    if not done.get("updated"):
        errs = "; ".join(f"{x.get('code')}: {x.get('message')}" for x in done.get("errors") or []) or "no reason given"
        fail("not_updated", f"Ozon did not change the price ({errs}).")
    out(f"OK {plan}", warn, "Ozon shows it on the site within a few minutes.")


def price_wb(a, cfg, new):
    # /api/v2/list/goods/filter to read, /api/v2/upload/task to write, /api/v2/history/tasks for the result.
    if not a.id.isdigit():
        fail("bad_id", "for Wildberries give the nmID (the WB item number, digits), as stock wb shows it.")
    if abs(new - round(new)) > 0.001:
        fail("whole_rubles", f"WB takes whole rubles. Ask the owner whether {int(new)} or {int(new) + 1} is right.")
    new = int(round(new))
    r = wb(cfg, "prices", "GET", "/api/v2/list/goods/filter", params={"limit": 1, "filterNmID": a.id}, what="prices")
    goods = ((r.get("data") or {}).get("listGoods")) or []
    if not goods:
        fail("not_found", f"no Wildberries item with nmID {a.id}.")
    g = goods[0]
    sizes = g.get("sizes") or [{}]
    if len({s.get("price") for s in sizes}) > 1:
        fail("size_prices", "this item has a different price per size. Change it in the WB cabinet, "
             "Цены и скидки, where each size is shown.")
    old, cur = sizes[0].get("price"), g.get("currencyIsoCode4217") or "RUB"
    disc_old = int(g.get("discount") or 0)
    disc = disc_old if a.discount is None else a.discount
    if not 0 <= disc < 100:
        fail("bad_discount", "the discount is a whole percent from 0 to 99.")
    final_old = sizes[0].get("discountedPrice")
    final_new = round(new * (100 - disc) / 100)
    plan = (f"Wildberries nmID {a.id} ({g.get('vendorCode')}): price before discount {num(old)} → {num(new)} {cur}, "
            f"your discount {disc_old}%" + (f" → {disc}%" if disc != disc_old else "")
            + f", price with your discount {num(final_old)} → {num(final_new)}.")
    warn = change_note(final_old, final_new)
    if not a.yes:
        out(f"PREVIEW {plan}", warn,
            "Nothing changed. Buyers may see less: WB adds its own discount on top. A price several times lower "
            "goes to WB's price quarantine and does not change. Show it to the owner; after a clear yes run the "
            "same command with --yes.")
        return
    up = wb(cfg, "prices", "POST", "/api/v2/upload/task", body={"data": [{"nmID": int(a.id), "price": new, "discount": disc}]},
            what="prices")
    upload = (up.get("data") or {}).get("id")
    if up.get("error") and not (up.get("data") or {}).get("alreadyExists"):
        fail("not_updated", f"Wildberries did not take the change ({up.get('errorText')}).")
    # The change is applied in the background; look at the result a few times.
    for _ in range(3):
        time.sleep(POLL)
        try:
            h = wb(cfg, "prices", "GET", "/api/v2/history/tasks", params={"uploadID": upload}, what="prices", retry=False)
        except Http:
            continue
        st = (h.get("data") or {}).get("status")
        if st == 3:
            out(f"OK {plan}", warn, "WB applied it.")
            return
        if st in (4, 5, 6):
            err = ""
            try:
                d = wb(cfg, "prices", "GET", "/api/v2/history/goods/task", params={"uploadID": upload, "limit": 10},
                       what="prices", retry=False)
                err = "; ".join(x.get("errorText") or "" for x in (d.get("data") or {}).get("historyGoods") or [])
            except Http:
                pass
            fail("not_updated", f"Wildberries did not change the price ({err or 'upload status ' + str(st)}).")
    out(f"OK {plan}", warn, f"WB accepted it (upload {upload}) and is still applying it; it shows within a few minutes.")


# ---- selftest: everything above against a local stub, no network ------------------------

def selftest():
    import http.server
    import subprocess
    import tempfile
    import threading

    OZ_KEY = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    OZ_BAD = "ffffffff-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    exp = int(time.time()) + 170 * 86400
    claims = {"acc": 3, "ent": 1, "exp": exp, "for": "self", "id": "11111111-2222-3333-4444-555555555555",
              "s": (1 << 2) | (1 << 3) | (1 << 4) | (1 << 5) | (1 << 7), "sid": "x", "t": False, "uid": 1}

    def b64(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")
    WB_TOKEN = b64({"alg": "ES256", "kid": "k", "typ": "JWT"}) + "." + b64(claims) + ".c2lnbmF0dXJlLXNlY3JldC1wYXJ0LXh5ejEyMw"
    TG_TOKEN = "123456:TEST-BOT-TOKEN-SECRET"
    secrets = [OZ_KEY, OZ_BAD, WB_TOKEN, WB_TOKEN.split(".")[2], TG_TOKEN]
    now = datetime.now(UTC)
    iso = lambda dt: dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    today = datetime.now(MSK).date().isoformat()
    state = {"log": [], "force": {}}

    routes = {
        ("POST", "/v1/roles"): {"expires_at": iso(now + timedelta(days=80)), "roles": [{"name": "Admin", "methods": []}]},
        ("POST", "/v1/seller/info"): {"company": {"name": "Тест Шоп"}},
        ("POST", "/v4/posting/fbs/unfulfilled/list"): {"postings": [
            {"posting_number": "111-0001-1", "status": "awaiting_packaging", "shipment_date": iso(now - timedelta(hours=2)),
             "products": [{"name": "Кружка белая", "offer_id": "ART-1", "quantity": 2, "sku": 501}]},
            {"posting_number": "111-0002-1", "status": "awaiting_deliver", "shipment_date": iso(now + timedelta(hours=20)),
             "products": [{"name": "Чайник", "offer_id": "ART-2", "quantity": 1, "sku": 502}]}],
            "has_next": False, "cursor": ""},
        ("POST", "/v4/product/info/stocks"): {"items": [
            {"offer_id": "ART-1", "product_id": 1, "stocks": [{"type": "fbs", "present": 3, "reserved": 1, "sku": 501},
                                                              {"type": "fbo", "present": 0, "reserved": 0, "sku": 501}]},
            {"offer_id": "ART-2", "product_id": 2, "stocks": [{"type": "fbo", "present": 40, "reserved": 0, "sku": 502}]}],
            "cursor": "", "total_items": 2},
        ("POST", "/v3/product/info/list"): {"items": [
            {"id": 1, "sku": 501, "offer_id": "ART-1", "name": "Кружка белая"},
            {"id": 2, "sku": 502, "offer_id": "ART-2", "name": "Чайник"}]},
        ("POST", "/v1/analytics/data"): {"result": {"data": [
            {"dimensions": [{"id": "501", "name": "Кружка белая"}], "metrics": [12000, 20]}], "totals": [15500, 26]},
            "timestamp": "x"},
        ("POST", "/v1/finance/balance"): {"cashflows": {
            "sales": {"amount": {"currency_code": "RUB", "value": 14000}, "fee": {"currency_code": "RUB", "value": -2100},
                      "amount_details": {"revenue": {"currency_code": "RUB", "value": 13000}}},
            "returns": {"amount": {"currency_code": "RUB", "value": -900}},
            "services": [{"name": "logistics", "amount": {"currency_code": "RUB", "value": -700}}]},
            "total": {"accrued": {"currency_code": "RUB", "value": 10300}}},
        ("POST", "/v2/review/list"): {"reviews": [
            {"id": "rev-1", "sku": 501, "rating": 2, "text": "Пришла со сколом", "published_at": iso(now), "status": "NEW",
             "comments_amount": 0}], "has_next": False},
        ("POST", "/v1/question/list"): {"questions": [
            {"id": "q-1", "sku": 502, "text": "Какой объём?", "published_at": iso(now), "status": "NEW"}], "has_next": False},
        ("POST", "/v1/review/comment/create"): {"comment_id": "c-1"},
        ("POST", "/v1/question/answer/create"): {"answer_id": "a-1"},
        ("POST", "/v5/product/info/prices"): {"items": [
            {"offer_id": "ART-1", "product_id": 1, "price": {"price": 1490, "old_price": 1990, "currency_code": "RUB"}}]},
        ("POST", "/v1/product/import/prices"): {"result": [{"offer_id": "ART-1", "product_id": 1, "updated": True, "errors": []}]},
        ("GET", "/ping"): {"TS": "now", "Status": "OK"},
        ("GET", "/api/v3/orders/new"): {"orders": [
            {"id": 1, "article": "TS-1", "nmId": 111, "createdAt": iso(now - timedelta(hours=3)), "skus": ["1"]},
            {"id": 2, "article": "TS-1", "nmId": 111, "createdAt": iso(now - timedelta(hours=1)), "skus": ["1"]},
            {"id": 3, "article": "BIG-1", "nmId": 222, "createdAt": iso(now), "sellerDate": "20.10.2026"}]},
        ("POST", "/api/v2/stocks-report/products/products"): {"data": {"items": [
            {"nmID": 111, "name": "Футболка", "vendorCode": "TS-1", "metrics": {
                "stockCount": 3, "avgOrders": 1.5, "saleRate": {"days": 2, "hours": 0}, "availability": "deficient"}},
            {"nmID": 222, "name": "Палатка", "vendorCode": "BIG-1", "metrics": {
                "stockCount": 50, "avgOrders": 0.2, "saleRate": {"days": 250, "hours": 0}, "availability": "actual"}}]}},
        ("GET", "/api/v1/supplier/orders"): [
            {"date": today + "T10:00:00", "supplierArticle": "TS-1", "nmId": 111, "priceWithDisc": 700, "isCancel": False},
            {"date": today + "T11:00:00", "supplierArticle": "TS-1", "nmId": 111, "priceWithDisc": 700, "isCancel": True},
            {"date": "2020-01-01T11:00:00", "supplierArticle": "OLD", "nmId": 1, "priceWithDisc": 99, "isCancel": False}],
        ("GET", "/api/v1/supplier/sales"): [
            {"date": today + "T12:00:00", "saleID": "S1", "priceWithDisc": 700, "forPay": 560},
            {"date": today + "T13:00:00", "saleID": "R1", "priceWithDisc": -700, "forPay": -560},
            {"date": today + "T14:00:00", "saleID": "S2", "priceWithDisc": 700, "forPay": 560}],
        ("GET", "/api/v1/feedbacks"): {"data": {"countUnanswered": 1, "feedbacks": [
            {"id": "wbf-1", "text": "Маломерит", "pros": "", "cons": "размер", "productValuation": 3, "createdDate": iso(now),
             "productDetails": {"productName": "Футболка", "supplierArticle": "TS-1", "nmId": 111}}]}},
        ("GET", "/api/v1/questions"): {"data": {"countUnanswered": 1, "questions": [
            {"id": "wbq-1", "text": "Есть размер XL?", "createdDate": iso(now),
             "productDetails": {"productName": "Футболка", "supplierArticle": "TS-1"}}]}},
        ("GET", "/api/v1/feedback"): {"data": {"id": "wbf-1", "text": "Маломерит", "answer": None}},
        ("GET", "/api/v1/question"): {"data": {"id": "wbq-1", "text": "Есть размер XL?", "answer": None}},
        ("POST", "/api/v1/feedbacks/answer"): None,
        ("PATCH", "/api/v1/questions"): {"data": None, "error": False, "errorText": ""},
        ("GET", "/api/v2/list/goods/filter"): {"data": {"listGoods": [
            {"nmID": 111, "vendorCode": "TS-1", "discount": 30, "currencyIsoCode4217": "RUB",
             "sizes": [{"sizeID": 1, "price": 1000, "discountedPrice": 700, "techSizeName": "M"}]}]}},
        ("POST", "/api/v2/upload/task"): {"data": {"id": 777, "alreadyExists": False}, "error": False, "errorText": ""},
        ("GET", "/api/v2/history/tasks"): {"data": {"uploadID": 777, "status": 3}},
    }

    class Stub(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def handle_any(self):
            url = urllib.parse.urlparse(self.path)
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(n).decode() if n else ""
            entry = {"method": self.command, "path": url.path, "query": url.query, "body": body,
                     "headers": {k: v for k, v in self.headers.items()}}
            state["log"].append(entry)
            if url.path.startswith("/bot"):
                return self.reply(200, {"ok": url.path == f"/bot{TG_TOKEN}/deleteMessage"})
            queue = state["force"].get(url.path) or []
            if queue:
                code, payload, hdrs = queue.pop(0)
                return self.reply(code, payload, hdrs)
            if url.path.startswith("/v"):
                if self.headers.get("Api-Key") != OZ_KEY or self.headers.get("Client-Id") != "123456":
                    return self.reply(401, {"code": 16, "message": "Invalid Api-Key, please check the key and try again"})
            elif self.headers.get("Authorization") != WB_TOKEN:
                return self.reply(401, {"title": "unauthorized", "detail": "token is malformed", "status": 401})
            key = (self.command, url.path)
            if key not in routes:
                return self.reply(404, {"message": "no route"})
            if routes[key] is None:
                return self.reply(204, None)
            return self.reply(200, routes[key])

        def reply(self, code, payload, headers=None):
            raw = b"" if payload is None else json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(code)
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        do_GET = do_POST = do_PATCH = handle_any

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Stub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    tmp = tempfile.mkdtemp(prefix="mp-selftest-")
    store = os.path.join(tmp, "home", ".oinoa")
    tg = os.path.join(tmp, "tg")
    os.makedirs(os.path.join(tg, "msglog"))
    with open(os.path.join(tg, ".env"), "w") as f:
        f.write(f"TELEGRAM_BOT_TOKEN={TG_TOKEN}\n")
    log_file = os.path.join(tg, "msglog", "2026-10-18.jsonl")
    with open(log_file, "w") as f:
        f.write(json.dumps({"dir": "in", "message_id": 42, "text": f"Client-Id 123456\n{OZ_KEY}"}) + "\n")
        f.write(json.dumps({"dir": "in", "message_id": 43, "text": WB_TOKEN}) + "\n")
        f.write(json.dumps({"dir": "in", "message_id": 44, "text": "привет, что с заказами?"}, ensure_ascii=False) + "\n")
    env = {"PATH": os.environ.get("PATH", ""), "HOME": os.path.join(tmp, "home"), "OINOA_MP_DIR": store,
           "TELEGRAM_STATE_DIR": tg, "OINOA_TG_API": base, "OINOA_OZON_API": base, "OINOA_WB_API": base,
           "OINOA_MP_POLL": "0.05", "LANG": "C.UTF-8", "PYTHONIOENCODING": "utf-8"}
    results, seen = [], []

    def mp(args, stdin="", extra=None):
        e = dict(env, **(extra or {}))
        p = subprocess.run([sys.executable, "-I", os.path.abspath(__file__)] + args, input=stdin, env=e,
                           capture_output=True, text=True, timeout=60)
        seen.append(p.stdout + p.stderr)
        if os.environ.get("OINOA_MP_SELFTEST_SHOW"):  # print each answer, to read what the agent will see
            print(f"$ mp.py {' '.join(args)}  (exit {p.returncode})\n{p.stdout}{p.stderr}")
        return p.returncode, p.stdout, p.stderr

    def check(cond, label, detail=""):
        results.append((bool(cond), label, detail))

    def calls(path, method=None):
        return [x for x in state["log"] if x["path"] == path and (method is None or x["method"] == method)]

    # connect ozon: the key on stdin, Client ID and key in one message
    rc, so, se = mp(["connect", "ozon", "--chat", "7", "--msg", "42"], f"Client-Id: 123456\nApi-Key: {OZ_KEY}\n")
    check(rc == 0 and "OK connected Ozon «Тест Шоп»" in so, "connect ozon", so + se)
    check(calls("/v1/roles") and calls("/v1/roles")[-1]["headers"].get("Api-Key") == OZ_KEY, "ozon headers sent")
    path = os.path.join(store, ENV_NAME)
    check(os.path.exists(path) and (os.stat(path).st_mode & 0o777) == 0o600, "store file is 0600")
    check((os.stat(store).st_mode & 0o777) == 0o700, "store folder is 0700")
    check(OZ_KEY in open(path).read(), "ozon key stored")
    check(calls(f"/bot{TG_TOKEN}/deleteMessage") and "message_id=42" in calls(f"/bot{TG_TOKEN}/deleteMessage")[-1]["body"],
          "telegram message deleted")
    check("message with the key is deleted" in so, "connect says the message is deleted")
    check(OZ_KEY not in open(log_file).read() and "привет" in open(log_file).read(), "msglog scrubbed, other rows kept")

    # a wrong key: refused, nothing replaced
    rc, so, se = mp(["connect", "ozon", "--client-id", "123456"], f"{OZ_BAD}\n")
    check(rc == 2 and "ERROR ozon_key" in so and "Nothing is stored" in so, "wrong ozon key refused", so)
    check(OZ_BAD not in open(path).read() and OZ_KEY in open(path).read(), "wrong key not stored, old kept")
    rc, so, se = mp(["connect", "ozon"], "just words, no key\n")
    check(rc == 2 and "ERROR no_key" in so, "no key on stdin", so)
    rc, so, se = mp(["connect", "ozon", "--extra-secret-arg"], "")
    check(rc == 2 and "extra-secret-arg" not in so + se, "unknown argv is not echoed", so + se)

    # connect wb
    rc, so, se = mp(["connect", "wb", "--chat", "7", "--msg", "43"], f"вот токен:\n{WB_TOKEN}\n")
    check(rc == 0 and "OK connected Wildberries (personal token" in so and "Цены и скидки" in so, "connect wb", so + se)
    check(len(calls("/ping")) == 5 and calls("/ping")[-1]["headers"].get("Authorization") == WB_TOKEN, "wb pings with token")
    check(WB_TOKEN in open(path).read() and OZ_KEY in open(path).read(), "both keys stored")
    check(WB_TOKEN not in open(log_file).read(), "wb token scrubbed from msglog")

    rc, so, se = mp(["status"])
    check(rc == 0 and "Ozon: connected «Тест Шоп»" in so and "Wildberries: connected (personal token)" in so, "status", so)

    rc, so, se = mp(["orders"])
    check(rc == 0 and "Ozon: 2 orders to ship (3 items), 1 overdue" in so and "OVERDUE" in so
          and "Кружка белая x2 (ART-1)" in so, "orders ozon", so + se)
    check("Wildberries: 3 new orders" in so and "TS-1 (nmID 111) | x2" in so and "bring to WB by 20.10.2026" in so,
          "orders wb", so)
    sent = json.loads(calls("/v4/posting/fbs/unfulfilled/list")[-1]["body"])
    check(sent["filter"]["statuses"] == ["awaiting_packaging", "awaiting_deliver"] and sent["limit"] == 100, "ozon orders body")

    rc, so, se = mp(["stock", "--low", "5"])
    check(rc == 0 and "Ozon: 2 products, 1 at or below 5 units" in so and "Кружка белая (ART-1) | 2 available | FBO 0, FBS 2" in so
          and "LOW" in so, "stock ozon", so + se)
    check("Футболка (TS-1, nmID 111) | 3 in stock" in so and "WB: low stock" in so, "stock wb", so)

    rc, so, se = mp(["sales", "--days", "7"])
    check(rc == 0 and "Ordered: 15 500 ₽, 26 units" in so and "Ozon fee -2 100 ₽" in so, "sales ozon", so + se)
    check("Orders: 1 for 700 ₽ at your price, 1 cancelled" in so and "Bought out: 2 for 1 400 ₽, returns 1" in so
          and "about 560 ₽" in so and "approximate" in so, "sales wb", so)

    rc, so, se = mp(["reviews"])
    check(rc == 0 and "oz:r:rev-1 | 2/5" in so and "oz:q:q-1:502" in so and "wb:r:wbf-1 | 3/5" in so
          and "wb:q:wbq-1" in so and "Минусы: размер" in so, "reviews", so + se)

    rc, so, se = mp(["reply", "oz:r:rev-1", "-"], "Спасибо, что написали! Заменим кружку.")
    sent = json.loads(calls("/v1/review/comment/create")[-1]["body"])
    check(rc == 0 and sent == {"review_id": "rev-1", "text": "Спасибо, что написали! Заменим кружку.",
                               "mark_review_as_processed": True}, "reply ozon review", so + se)
    rc, so, se = mp(["reply", "oz:q:q-1:502"], "Объём 1,7 литра.")
    check(rc == 0 and json.loads(calls("/v1/question/answer/create")[-1]["body"])["sku"] == 502, "reply ozon question", so)
    rc, so, se = mp(["reply", "wb:r:wbf-1", "-"], "Спасибо за отзыв!")
    check(rc == 0 and json.loads(calls("/api/v1/feedbacks/answer")[-1]["body"]) == {"id": "wbf-1", "text": "Спасибо за отзыв!"},
          "reply wb review", so + se)
    rc, so, se = mp(["reply", "wb:q:wbq-1"], "Да, XL есть.")
    sent = json.loads(calls("/api/v1/questions", "PATCH")[-1]["body"])
    check(rc == 0 and sent == {"id": "wbq-1", "answer": {"text": "Да, XL есть."}, "state": "wbRu"}, "reply wb question", so)

    # price: a preview changes nothing, --yes changes it
    before = len(calls("/v1/product/import/prices"))
    rc, so, se = mp(["price", "ART-1", "1290"])
    check(rc == 0 and "PREVIEW Ozon ART-1: price 1 490 → 1 290 RUB" in so and len(calls("/v1/product/import/prices")) == before,
          "price preview changes nothing", so + se)
    rc, so, se = mp(["price", "ART-1", "1290", "--yes"])
    sent = json.loads(calls("/v1/product/import/prices")[-1]["body"])
    check(rc == 0 and "OK Ozon ART-1: price 1 490 → 1 290 RUB" in so
          and sent == {"prices": [{"offer_id": "ART-1", "price": "1290", "currency_code": "RUB"}]}, "price ozon", so + se)
    rc, so, se = mp(["price", "ART-1", "2500"])
    check(rc == 2 and "ERROR old_price_too_low" in so, "ozon crossed-out price guard", so)
    rc, so, se = mp(["price", "111", "1200"])
    check(rc == 2 and "ERROR which_market" in so, "ambiguous number asks for the market", so)
    rc, so, se = mp(["price", "111", "1200", "--market", "wb", "--yes"])
    sent = json.loads(calls("/api/v2/upload/task")[-1]["body"])
    check(rc == 0 and "1 000 → 1 200 RUB" in so and "700 → 840" in so and "WB applied it" in so
          and sent == {"data": [{"nmID": 111, "price": 1200, "discount": 30}]}, "price wb", so + se)

    # errors: 403 role on Ozon does not hide WB; 401 on WB; 429 retried once; 429 twice reported; network down
    state["force"]["/v4/posting/fbs/unfulfilled/list"] = [(403, {"code": 7, "message": "Api-Key is missing a required role for a method"}, {})]
    state["force"]["/api/v3/orders/new"] = [(401, {"title": "unauthorized", "detail": "token scope not allowed"}, {})]
    rc, so, se = mp(["orders"])
    check(rc == 2 and "ERROR ozon_role" in so and "ERROR wb_token" in so and "Маркетплейс" in so, "401/403 explained", so)
    rc, so, se = mp(["orders", "wb"], extra={})
    check(rc == 0, "wb works again after a 401")
    state["force"]["/api/v3/orders/new"] = [(429, {"title": "too many requests"}, {"X-Ratelimit-Retry": "1"})]
    n0 = len(calls("/api/v3/orders/new"))
    rc, so, se = mp(["orders", "wb"])
    check(rc == 0 and len(calls("/api/v3/orders/new")) == n0 + 2 and "3 new orders" in so, "429 waited and retried once", so)
    state["force"]["/api/v3/orders/new"] = [(429, {}, {"X-Ratelimit-Retry": "1"}), (429, {}, {"X-Ratelimit-Retry": "1"})]
    rc, so, se = mp(["orders", "wb"])
    check(rc == 2 and "ERROR rate_limited" in so, "429 twice is reported", so)
    state["force"]["/v2/review/list"] = [(403, {"code": 7, "message": "subscription required"}, {})]
    rc, so, se = mp(["reviews", "ozon"])
    check(rc == 0 and "Управление отзывами" in so and "oz:q:q-1:502" in so, "ozon plan without reviews explained", so + se)
    dead = socket.socket()
    dead.bind(("127.0.0.1", 0))
    port = dead.getsockname()[1]
    dead.close()
    rc, so, se = mp(["stock", "ozon"], extra={"OINOA_OZON_API": f"http://127.0.0.1:{port}"})
    check(rc == 2 and "ERROR network" in so, "network error explained", so)

    rc, so, se = mp(["orders", "--json"])
    try:
        parsed = json.loads(so)
        check("ozon" in parsed and "wb" in parsed, "--json is valid JSON")
    except ValueError:
        check(False, "--json is valid JSON", so)

    rc, so, se = mp(["forget", "wb"])
    check(rc == 0 and "OK forgot Wildberries" in so and WB_TOKEN not in open(path).read() and OZ_KEY in open(path).read(),
          "forget wb", so)
    rc, so, se = mp(["forget", "ozon"])
    check(rc == 0 and not os.path.exists(path), "forget ozon removes the file", so)

    leaks = [s for s in secrets if any(s in text for text in seen)]
    check(not leaks, "no key, token or bot token in any stdout or stderr", f"{len(leaks)} leaked")
    srv.shutdown()
    bad = [r for r in results if not r[0]]
    for ok, label, detail in results:
        print(f"{'ok  ' if ok else 'FAIL'} {label}" + ("" if ok else f"\n     {clip(detail, 900)}"))
    print(f"SELFTEST {'OK' if not bad else 'FAILED'}: {len(results) - len(bad)}/{len(results)} checks passed")
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)
    sys.exit(1 if bad else 0)


# ---- command line -------------------------------------------------------------------------

class Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse quotes the bad value; a key typed in the wrong place must not be printed back.
        fail("bad_args", f"the command line is not right ({re.sub(chr(39) + '[^' + chr(39) + ']*' + chr(39), '...', message)}). "
             "Keys go on stdin in the <<'EOF' block, never as arguments.")


def main():
    if "--selftest" in sys.argv[1:]:
        selftest()
    p = Parser(prog="mp.py", description="The seller's own Ozon and Wildberries shops.")
    sub = p.add_subparsers(dest="cmd", required=True, parser_class=Parser)
    c = sub.add_parser("connect")
    c.add_argument("market", choices=["ozon", "wb"])
    c.add_argument("--chat", help="chat_id of the message with the key")
    c.add_argument("--msg", action="append", help="message_id(s) of the message(s) with the key")
    c.add_argument("--client-id", help="the Ozon Client ID, if it came separately (not secret)")
    sub.add_parser("status")
    fg = sub.add_parser("forget")
    fg.add_argument("market", choices=["ozon", "wb"])
    for name in ("orders", "stock", "sales", "reviews"):
        r = sub.add_parser(name)
        r.add_argument("market", nargs="?", choices=["ozon", "wb"])
        r.add_argument("--json", action="store_true")
        if name == "stock":
            r.add_argument("--low", type=int, default=5, help="flag products at or below this many units")
            r.add_argument("--all", action="store_true")
        if name == "sales":
            r.add_argument("--days", type=int, default=7)
        if name == "reviews":
            r.add_argument("--limit", type=int, default=20)
    rp = sub.add_parser("reply")
    rp.add_argument("id")
    rp.add_argument("dash", nargs="?", choices=["-"])
    pr = sub.add_parser("price")
    pr.add_argument("id")
    pr.add_argument("new_price")
    pr.add_argument("--market", choices=["ozon", "wb"])
    pr.add_argument("--old-price", help="Ozon: the crossed-out price; 0 removes it")
    pr.add_argument("--discount", type=int, help="Wildberries: your discount in percent")
    pr.add_argument("--yes", action="store_true", help="change it; without this only a preview")
    a, extra = p.parse_known_args()
    if extra:
        # Never echo unknown arguments: a key typed on the command line must not be printed back.
        fail("bad_args", "unexpected words on the command line (not shown). Keys go on stdin in the <<'EOF' block, "
             "never as arguments; if a key was typed there, ask for a new one.")
    if a.cmd in ("sales",) and a.days < 1:
        fail("bad_days", "--days is 1 or more.")
    socket.setdefaulttimeout(TIMEOUT)
    sections = {
        "orders": {"ozon": orders_ozon, "wb": orders_wb},
        "stock": {"ozon": stock_ozon, "wb": stock_wb},
        "sales": {"ozon": sales_ozon, "wb": sales_wb},
        "reviews": {"ozon": reviews_ozon, "wb": reviews_wb},
    }
    if a.cmd in sections:
        return run(a, sections[a.cmd])
    {"connect": cmd_connect, "status": cmd_status, "forget": cmd_forget, "reply": cmd_reply,
     "price": cmd_price}[a.cmd](a)


if __name__ == "__main__":
    main()
