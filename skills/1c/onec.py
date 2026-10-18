#!/usr/bin/env python3
"""OINOA 1c: the owner's own 1С:Предприятие through its standard OData interface.

  connect [--chat ID --msg ID[,ID]] [--url URL] [--user NAME]   address, user and password on stdin
  status [--refresh]                           reachable, which configuration, what is published
  forget                                       drop the connection
  find TEXT [--limit N]                        goods and counterparties by name, code, article or ИНН
  stock [--item TEXT] [--low N] [--all]        stock per item from the balance register
  sales [--days N | --from DATE --to DATE] [--top N]   posted sales documents for a period
  debts [--top N]                              who owes us and whom we owe, approximate
  invoice COUNTERPARTY ITEM:QTY:PRICE ... [--org TEXT] [--yes]   a draft customer invoice, never posted
  --json on find, stock, sales, debts and invoice prints the raw OData answers.
  --selftest                                   offline test against a local stub server

Python 3 stdlib only. Every answer is a few short lines. A line that starts with ERROR says what
went wrong and what to tell the person. The password never appears in the output, in argv or in
the chat log: it comes in on stdin and lives in one 0600 file.

The interface (checked 2026-10-18):
  https://1cfresh.com/articles/data_odata    1С: URL form, Basic auth, $format=json, $filter functions,
      virtual-table methods (Balance, Turnovers, SliceLast), Post/Unpost, allowedOnly, the
      «Настройки стандартного интерфейса OData» processing (tabs «Авторизация» and «Состав»)
  https://its.1c.ru/db/fresh/content/19956692/1    1С: OData requests to a БП application in 1С:Фреш
  https://1c-dn.com/1c_enterprise/rest_interface/  1С: OData v3, Atom/XML and JSON, 4XX with an internal code
  https://kb.1ci.com/1C_Enterprise_Platform/FAQ/Development/Integration/How_to_use_the_oData_protocol_when_publishing_an_infobase_to_web_server
Object and field names per configuration: the PROFILES table below, and SKILL.md for what is
checked and what is inferred. Nothing here trusts the table blindly: an object is used only when
the base's own $metadata has it.
"""
import argparse
import base64
import io
import json
import os
import re
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone

HOME = os.path.expanduser("~")
# ~/.oinoa first; ~/.config/oinoa-1c when ~/.oinoa is not writable.
STORES = [os.environ["OINOA_1C_DIR"]] if os.environ.get("OINOA_1C_DIR") else [
    os.path.join(HOME, ".oinoa"), os.path.join(HOME, ".config", "oinoa-1c")]
ENV_NAME = "1c.env"
META_NAME = "1c-meta.json"
META_VERSION = 1
TG_DIR = os.environ.get("TELEGRAM_STATE_DIR") or os.path.join(HOME, ".claude", "channels", "telegram")
TG_API = os.environ.get("OINOA_TG_API", "https://api.telegram.org")
TIMEOUT = float(os.environ.get("OINOA_1C_TIMEOUT", "30"))   # seconds, every data request
META_TIMEOUT = max(TIMEOUT, 120.0)   # $metadata of a big configuration is 10-20 MB
ODATA = "/odata/standard.odata/"
NS = "StandardODATA."
ZERO = "00000000-0000-0000-0000-000000000000"
MSK = timezone(timedelta(hours=3), "MSK")
UTC = timezone.utc
NAME_BATCH = 15     # Ref_Key lookups per request: IIS cuts query strings at 2048 bytes
PAGE = 200          # documents per page
MAX_PAGES = 25

# ---- what each known configuration calls things ------------------------------------------------
# V: checked against 1С's own docs or a real base's $metadata (sources in SKILL.md).
# I: inferred from the configuration's usual structure; used only if $metadata has it.
PROFILES = {
    "bp30": {
        "name": "1С:Бухгалтерия предприятия 3.0",
        "signature": ["AccountingRegister_Хозрасчетный", "ChartOfAccounts_Хозрасчетный",
                      "Document_СчетНаОплатуПокупателю", "Document_ПоступлениеНаРасчетныйСчет",
                      "Document_СписаниеСРасчетногоСчета"],
        "goods": [{"entity": "Catalog_Номенклатура"}],                                   # V
        "parties": [{"entity": "Catalog_Контрагенты"}],                                  # V
        "orgs": [{"entity": "Catalog_Организации", "bank": "ОсновнойБанковскийСчет_Key"}],  # V
        "sales": [{"entity": "Document_РеализацияТоваровУслуг", "tables": ["Товары", "Услуги"]}],  # V
        "invoice": [{"entity": "Document_СчетНаОплатуПокупателю", "table": "Товары",      # V
                     "contract": "ДоговорКонтрагента_Key",
                     "party_contract": "ОсновнойДоговорКонтрагента_Key",                    # I
                     # БП keeps the organization's bank account of an invoice in СтруктурнаяЕдиница.
                     "from_org": {"СтруктурнаяЕдиница_Key": "ОсновнойБанковскийСчет_Key"}}],  # V
        "stock": [{"type": "accounting", "entity": "AccountingRegister_Хозрасчетный",       # V
                   "chart": "ChartOfAccounts_Хозрасчетный", "accounts": ["41", "43"],
                   "fallback": ["41.01", "41.02", "41.03", "41.04", "43"],
                   "item_type": "Catalog_Номенклатура", "store_type": "Catalog_Склады"}],
        "debts": [{"type": "accounting", "entity": "AccountingRegister_Хозрасчетный",       # V
                   "chart": "ChartOfAccounts_Хозрасчетный", "receivable": "62", "payable": "60",
                   "fallback": {"receivable": ["62.01", "62.02"], "payable": ["60.01", "60.02"]},
                   "party_type": "Catalog_Контрагенты"}],
    },
    "unf": {
        "name": "1С:Управление нашей фирмой",
        "signature": ["ChartOfAccounts_Управленческий", "Document_РасходнаяНакладная",
                      "Document_ПриходнаяНакладная", "AccumulationRegister_ЗапасыНаСкладах",
                      "Catalog_СтруктурныеЕдиницы", "Document_РасходИзКассы", "Document_ПоступлениеНаСчет"],
        "goods": [{"entity": "Catalog_Номенклатура"}],                                   # V
        "parties": [{"entity": "Catalog_Контрагенты"}],                                  # V
        "orgs": [{"entity": "Catalog_Организации"}],                                     # V
        "sales": [{"entity": "Document_РасходнаяНакладная", "tables": ["Запасы"]}],       # V name, I table
        "invoice": [{"entity": "Document_СчетНаОплату", "table": "Запасы",               # I
                     "contract": "Договор_Key", "party_contract": "ДоговорПоУмолчанию_Key"}],
        "stock": [{"type": "accumulation", "entity": "AccumulationRegister_ЗапасыНаСкладах",  # V
                   "qty": "Количество", "item": "Номенклатура", "store": "СтруктурнаяЕдиница",
                   "store_catalog": "Catalog_СтруктурныеЕдиницы"},
                  {"type": "accumulation", "entity": "AccumulationRegister_Запасы",          # V name
                   "qty": "Количество", "item": "Номенклатура", "store": "СтруктурнаяЕдиница",
                   "store_catalog": "Catalog_СтруктурныеЕдиницы"}],
        "debts": [{"type": "accumulation", "side": "receivable",                          # I
                   "entity": "AccumulationRegister_РасчетыСПокупателями", "party": "Контрагент",
                   "party_catalog": "Catalog_Контрагенты", "plus": ["Сумма"], "minus": []},
                  {"type": "accumulation", "side": "payable",                             # I
                   "entity": "AccumulationRegister_РасчетыСПоставщиками", "party": "Контрагент",
                   "party_catalog": "Catalog_Контрагенты", "plus": ["Сумма"], "minus": []}],
    },
    "ut11": {
        "name": "1С:Управление торговлей 11",
        "signature": ["Catalog_Партнеры", "Document_ЗаказКлиента", "AccumulationRegister_ВыручкаИСебестоимостьПродаж",
                      "Document_ПриобретениеТоваровУслуг", "Catalog_СоглашенияСКлиентами",
                      "AccumulationRegister_РасчетыСКлиентамиПоДокументам"],
        "goods": [{"entity": "Catalog_Номенклатура"}],                                   # V
        "parties": [{"entity": "Catalog_Контрагенты"}],                                  # V
        "orgs": [{"entity": "Catalog_Организации"}],                                     # V
        "sales": [{"entity": "Document_РеализацияТоваровУслуг", "tables": ["Товары"]}],   # V name, I fields
        "invoice": [{"entity": "Document_СчетНаОплатуКлиенту", "table": "Товары",        # I
                     "contract": "Договор_Key"}],
        "stock": [{"type": "accumulation", "entity": "AccumulationRegister_ТоварыНаСкладах",  # V
                   "qty": "ВНаличии", "reserved": "КОтгрузке", "item": "Номенклатура", "store": "Склад",
                   "store_catalog": "Catalog_Склады"}],
        "debts": [{"type": "accumulation", "side": "receivable",                          # V register, I names
                   "entity": "AccumulationRegister_РасчетыСКлиентамиПоДокументам", "party": "АналитикаУчетаПоПартнерам",
                   "party_catalog": "Catalog_КлючиАналитикиУчетаПоПартнерам", "plus": ["Долг"], "minus": ["Предоплата"]},
                  {"type": "accumulation", "side": "receivable",                          # I
                   "entity": "AccumulationRegister_РасчетыСКлиентами", "party": "АналитикаУчетаПоПартнерам",
                   "party_catalog": "Catalog_КлючиАналитикиУчетаПоПартнерам", "plus": ["Сумма"], "minus": []},
                  {"type": "accumulation", "side": "payable",                             # V register, I names
                   "entity": "AccumulationRegister_РасчетыСПоставщиками", "party": "АналитикаУчетаПоПартнерам",
                   "party_catalog": "Catalog_КлючиАналитикиУчетаПоПартнерам", "plus": ["Сумма"], "minus": []},
                  {"type": "accumulation", "side": "payable",                             # I
                   "entity": "AccumulationRegister_РасчетыСПоставщикамиПоДокументам", "party": "АналитикаУчетаПоПартнерам",
                   "party_catalog": "Catalog_КлючиАналитикиУчетаПоПартнерам", "plus": ["Долг"], "minus": ["Предоплата"]}],
    },
}
KINDS = ("goods", "parties", "orgs", "sales", "invoice", "stock", "debts")
KIND_EN = {"goods": "goods catalogue", "parties": "counterparties", "orgs": "organizations",
           "sales": "sales documents", "invoice": "customer invoices", "stock": "stock balance register",
           "debts": "mutual settlements"}
# What to look for when the expected object is missing: prefix, words in the name.
KIND_NEAR = {
    "goods": (("Catalog_",), ("Номенклатур", "Товар")),
    "parties": (("Catalog_",), ("Контрагент", "Партнер", "Клиент")),
    "orgs": (("Catalog_",), ("Организац",)),
    "sales": (("Document_",), ("Реализац", "Продаж", "Расходн", "Отгруз", "Чек")),
    "invoice": (("Document_",), ("Счет",)),
    "stock": (("AccumulationRegister_", "AccountingRegister_"), ("Товар", "Запас", "Остат", "Хозрасч")),
    "debts": (("AccumulationRegister_", "AccountingRegister_"), ("Расчет", "Хозрасч")),
}
# The names the owner and the administrator see in 1С.
SYN = {
    "Catalog_Номенклатура": "Номенклатура", "Catalog_Контрагенты": "Контрагенты",
    "Catalog_Организации": "Организации", "Catalog_Склады": "Склады",
    "Catalog_СтруктурныеЕдиницы": "Структурные единицы", "Catalog_ДоговорыКонтрагентов": "Договоры контрагентов",
    "Catalog_Партнеры": "Партнеры", "Catalog_КлючиАналитикиУчетаПоПартнерам": "Ключи аналитики учета по партнерам",
    "ChartOfAccounts_Хозрасчетный": "План счетов бухгалтерского учета",
    "AccountingRegister_Хозрасчетный": "регистр бухгалтерии Хозрасчетный",
    "Document_РеализацияТоваровУслуг": "Реализация товаров и услуг",
    "Document_СчетНаОплатуПокупателю": "Счет покупателю", "Document_РасходнаяНакладная": "Расходная накладная",
    "Document_СчетНаОплату": "Счет на оплату", "Document_СчетНаОплатуКлиенту": "Счет на оплату клиенту",
    "AccumulationRegister_ТоварыНаСкладах": "Товары на складах",
    "AccumulationRegister_ЗапасыНаСкладах": "Запасы на складах", "AccumulationRegister_Запасы": "Запасы",
    "AccumulationRegister_РасчетыСПокупателями": "Расчеты с покупателями",
    "AccumulationRegister_РасчетыСПоставщиками": "Расчеты с поставщиками",
    "AccumulationRegister_РасчетыСКлиентамиПоДокументам": "Расчеты с клиентами по документам",
    "AccumulationRegister_РасчетыСКлиентами": "Расчеты с клиентами",
    "AccumulationRegister_РасчетыСПоставщикамиПоДокументам": "Расчеты с поставщиками по документам",
}
COMPOSITION = "Администрирование → Синхронизация данных → Настройки стандартного интерфейса OData, вкладка «Состав»"


def out(*lines):
    print("\n".join(str(x) for x in lines if x is not None))


def fail(code, text, extra=()):
    out(f"ERROR {code}: {text}", *extra)
    sys.exit(2)


def human(entity):
    syn = SYN.get(entity)
    return f"«{syn}» ({entity})" if syn else entity


# ---- the stored connection ------------------------------------------------------------------

def store_path(name=ENV_NAME):
    for d in STORES:
        path = os.path.join(d, name)
        if os.path.exists(path):
            return path
    return os.path.join(STORES[0], name)


def load():
    values = {}
    try:
        with open(store_path(), encoding="utf-8") as f:
            for line in f:
                key, sep, value = line.rstrip("\n").partition("=")
                if sep and key.strip() and not key.startswith("#"):
                    values[key.strip()] = value
    except FileNotFoundError:
        pass
    return values


def _write_private(store, name, text):
    os.makedirs(store, mode=0o700, exist_ok=True)
    os.chmod(store, 0o700)
    path = os.path.join(store, name)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def save(values):
    """All values in one 0600 file in a 0700 folder; a copy left in the other place goes."""
    clean = {k: re.sub(r"[\r\n]", " ", str(v)) for k, v in values.items() if v not in (None, "")}
    if not clean.get("ONEC_PASSWORD"):
        for store in STORES:
            for name in (ENV_NAME, META_NAME):
                try:
                    os.remove(os.path.join(store, name))
                except OSError:
                    pass
        return
    text = "# OINOA 1C. Secret: never print, copy or send this file.\n" + \
        "".join(f"{k}={clean[k]}\n" for k in sorted(clean))
    for store in STORES:
        try:
            _write_private(store, ENV_NAME, text)
        except OSError:
            continue
        for other in STORES:
            if other != store:
                for name in (ENV_NAME, META_NAME):
                    try:
                        os.remove(os.path.join(other, name))
                    except OSError:
                        pass
        return
    fail("cannot_save", "1С answered, but the connection could not be kept on this agent. Nothing is "
         "stored; tell them it will be fixed and to try again later.")


def load_meta(cfg):
    try:
        with open(store_path(META_NAME), encoding="utf-8") as f:
            meta = json.load(f)
    except (OSError, ValueError):
        return None
    if meta.get("v") != META_VERSION or meta.get("url") != cfg.get("ONEC_URL"):
        return None
    return meta


def save_meta(meta):
    text = json.dumps(meta, ensure_ascii=False)
    for store in STORES:
        try:
            _write_private(store, META_NAME, text)
            return
        except OSError:
            continue


# ---- Telegram: the message with the password leaves the chat ---------------------------------

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
    scrub_log(set(msgs), [s for s in secrets if s and len(s) >= 6])
    if tried and deleted == tried:
        return "Chat: the message with the password is deleted."
    return "Chat: could not delete the message with the password. Ask them to delete it now."


def scrub_log(msgs, secrets):
    logdir = os.path.join(TG_DIR, "msglog")
    try:
        names = [n for n in os.listdir(logdir) if n.endswith(".jsonl")]
    except OSError:
        return
    for name in names:
        path = os.path.join(logdir, name)
        try:
            with open(path, encoding="utf-8") as f:
                lines = f.readlines()
        except OSError:
            continue
        changed = False
        for i, line in enumerate(lines):
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, dict):
                continue
            text = str(row.get("text", ""))
            hit = str(row.get("message_id")) in msgs and row.get("dir") == "in"
            hit = hit or any(s in text for s in secrets)
            if hit:
                row["text"] = "[deleted: 1C password]"
                lines[i] = json.dumps(row, ensure_ascii=False) + "\n"
                changed = True
        if changed:
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.writelines(lines)
            os.chmod(tmp, os.stat(path).st_mode & 0o777)
            os.replace(tmp, path)


# ---- the address, the user and the password from what the owner pasted -------------------------

URL_RE = re.compile(r"https?://[^\s<>\"'«»]+", re.I)
BARE_URL_RE = re.compile(r"(?<![\w@./-])((?:[\w-]+\.)+[a-zA-Zа-яА-Я]{2,}(?::\d+)?/[^\s<>\"'«»]*)")
L_USER = r"(?:логин|login|пользователь|имя\s+пользователя|user(?:\s*name)?|username)"
L_PASS = r"(?:пароль|password|passwd|pass|pwd)"
L_URL = r"(?:адрес|ссылка|url|address|link|база|сервер|server)"
SEP = r"\s*[:=\-–]?\s*"


def parse_creds(raw, url_arg="", user_arg=""):
    """(url text, user, password). Labeled lines first, then plain lines: user, then password."""
    text = (raw or "").replace("\r", "")
    url = (url_arg or "").strip()
    found = URL_RE.search(text)
    strip_re = URL_RE
    if not found:
        found, strip_re = BARE_URL_RE.search(text), BARE_URL_RE
    if not url and found:
        url = found.group(0)
    user, pw, rest = (user_arg or "").strip(), "", []
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        # Labeled lines are read as they are: a password may look like anything, even an address.
        both = re.match(rf"^{L_USER}{SEP}(\S+)[\s,;]+{L_PASS}{SEP}(.+)$", line, re.I)
        if both:
            user = user or both.group(1).strip()
            pw = pw or both.group(2).strip()
            continue
        m = re.match(rf"^{L_PASS}{SEP}(.*)$", line, re.I)
        if m:
            pw = pw or m.group(1).strip()
            continue
        m = re.match(rf"^{L_USER}{SEP}(.*)$", line, re.I)
        if m:
            user = user or m.group(1).strip()
            continue
        if re.match(rf"^{L_URL}(?![\w])", line, re.I):
            continue   # the address line; the address itself is already taken
        line = strip_re.sub(" ", line).strip()
        if not line or line.endswith((":", "?", "!")) or len(line.split()) >= 4:
            continue  # prose around the data, not the data
        rest.append(line)
    if not user and not pw and len(rest) == 1:
        parts = [p for p in re.split(r"\s*[/;]\s*|\s+", rest[0]) if p]
        if len(parts) == 2:
            return url, parts[0], parts[1]
    if not user and rest:
        user = rest.pop(0)
    if not pw and rest:
        pw = rest.pop(0)
    return url, user, pw


def normalize_url(text):
    """(root, user, password from the URL). Root is the base as the browser opens it, without
    /ru_RU, the web client's tail or an /odata/... path."""
    text = (text or "").strip().strip("<>\"'«»")
    if text and not re.match(r"https?://", text, re.I):
        text = "https://" + text
    m = URL_RE.search(text)
    if not m:
        return None, "", ""
    u = m.group(0).rstrip(".,;)")
    try:
        s = urllib.parse.urlsplit(u)
        port = s.port
    except ValueError:
        return None, "", ""
    if not s.hostname:
        return None, "", ""
    user = urllib.parse.unquote(s.username or "")
    pw = urllib.parse.unquote(s.password or "")
    path = urllib.parse.unquote(s.path)
    cut = path.lower().find("/odata/")
    if cut >= 0:
        path = path[:cut]
    segs = [x for x in path.split("/") if x]
    while segs and (re.fullmatch(r"[a-z]{2}_[A-Z]{2}", segs[-1]) or segs[-1].lower() == "e1cib"
                    or (len(segs) > 1 and segs[-1].lower() in ("ru", "en"))):
        segs.pop()
    netloc = s.hostname + (f":{port}" if port else "")
    root = f"{s.scheme.lower()}://{netloc}" + ("/" + "/".join(segs) if segs else "")
    return root, user, pw


def local_hint(root):
    host = urllib.parse.urlsplit(root).hostname or ""
    private = re.match(r"^(10\.|127\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.|169\.254\.)", host) or \
        "." not in host or host.endswith((".local", ".lan", ".corp", ".internal"))
    if private:
        return ["This looks like an address inside their office network. This agent works from its own "
                "server on the internet, so the 1С publication must be reachable from the internet "
                "(https), or through a VPN to this server."]
    return []


# ---- HTTP -----------------------------------------------------------------------------------

class Http(Exception):
    """A non-2xx answer. explain() turns it into what to tell the person."""

    def __init__(self, code, body, what="", entity="", method="GET"):
        super().__init__(code)
        self.code, self.body, self.what, self.entity, self.method = code, body or "", what, entity, method

    def platform(self):
        """(code, text) from 1С's {"odata.error": {"code": "8", "message": {"value": "…"}}}."""
        try:
            data = json.loads(self.body)
        except ValueError:
            return "", ""
        if not isinstance(data, dict):
            return "", ""
        err = data.get("odata.error") or data.get("error") or {}
        if not isinstance(err, dict):
            return "", ""
        code = err.get("code")
        code = str(code) if isinstance(code, (str, int)) and not isinstance(code, bool) else ""
        msg = err.get("message")
        if isinstance(msg, dict):
            msg = msg.get("value", "")
        return code, re.sub(r"\s+", " ", str(msg or "")).strip()[:300]


class Net(Exception):
    def __init__(self, kind, detail="", what=""):
        super().__init__(kind)
        self.kind, self.detail, self.what = kind, str(detail), what


class Moved(Exception):
    pass


class SameHostRedirect(urllib.request.HTTPRedirectHandler):
    """Follow a redirect only to the same host and only for reads: the password goes nowhere else."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old, new = urllib.parse.urlsplit(req.full_url), urllib.parse.urlsplit(newurl)
        if old.hostname != new.hostname or req.get_method() != "GET":
            raise Moved(new.hostname or newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


OPENER = urllib.request.build_opener(SameHostRedirect)
QSAFE = "'(),:/$=*"


def odata_url(root, path, params=None):
    s = urllib.parse.urlsplit(root)
    base = urllib.parse.quote(s.path.rstrip("/"), safe="/")
    url = f"{s.scheme}://{s.netloc}{base}{ODATA}{urllib.parse.quote(path, safe='/()=,:$*' + chr(39))}"
    if params:
        url += "?" + "&".join(f"{urllib.parse.quote(k, safe='$')}={urllib.parse.quote(str(v), safe=QSAFE)}"
                              for k, v in params.items() if v not in (None, ""))
    return url


def request_raw(cfg, method, path, params=None, body=None, timeout=None, accept="application/json",
                what="", entity=""):
    url = odata_url(cfg["ONEC_URL"], path, params)
    token = base64.b64encode(f"{cfg['ONEC_USER']}:{cfg['ONEC_PASSWORD']}".encode("utf-8")).decode()
    headers = {"Authorization": "Basic " + token, "Accept": accept, "User-Agent": "oinoa-1c/1.0"}
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json;charset=utf-8"
    # A read that times out is asked once more: the first request after a pause wakes 1С up and
    # can take longer than the timeout. A write is never repeated: it may have gone through.
    tries = 2 if method == "GET" else 1
    for attempt in range(tries):
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with OPENER.open(req, timeout=timeout or TIMEOUT) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            raise Http(e.code, e.read(8000).decode("utf-8", "replace"), what, entity, method)
        except Moved as e:
            raise Net("moved", e, what)
        except (socket.timeout, TimeoutError):
            if attempt + 1 < tries:
                continue
            raise Net("timeout", "", what)
        except urllib.error.URLError as e:
            reason = e.reason
            if isinstance(reason, (socket.timeout, TimeoutError)):
                if attempt + 1 < tries:
                    continue
                raise Net("timeout", "", what)
            if isinstance(reason, ssl.SSLError):
                raise Net("tls", getattr(reason, "reason", None) or reason, what)
            raise Net("network", reason, what)
        except (ConnectionError, OSError) as e:
            raise Net("network", e, what)
    raise Net("timeout", "", what)


def request(cfg, method, path, params=None, body=None, what="", entity=""):
    status, raw = request_raw(cfg, method, path, params, body, what=what, entity=entity or path.split("/")[0].split("(")[0])
    if not raw.strip():
        return {}
    try:
        return json.loads(raw.decode("utf-8-sig"))
    except ValueError:
        raise Http(status, raw[:300].decode("utf-8", "replace"), what, entity, method)


def get(cfg, path, params=None, what="", entity=""):
    q = {"$format": "json", "allowedOnly": "true"}
    q.update(params or {})
    return request(cfg, "GET", path, q, what=what, entity=entity)


def get_all(cfg, path, params, what="", order="Ref_Key", page=PAGE):
    """All rows, $top/$skip pages in a stable order. (rows, pages, truncated)."""
    rows, pages, skip = [], [], 0
    for _ in range(MAX_PAGES):
        q = dict(params, **{"$top": str(page), "$skip": str(skip), "$orderby": order})
        r = get(cfg, path, q, what=what)
        got = r.get("value") or []
        rows += got
        pages.append(r)
        if len(got) < page:
            return rows, pages, False
        skip += page
    return rows, pages, True


def explain(e):
    """(code, text) for an Http or Net error, in words the agent can pass on."""
    if isinstance(e, Net):
        if e.kind == "timeout":
            return "timeout", (f"1С did not answer within {int(TIMEOUT)} s (asked twice). A big base can be slow, "
                               "the first request after a pause most of all. Try again in a minute; if it keeps "
                               "happening, ask for less (stock --item, a shorter period).")
        if e.kind == "moved":
            return "moved", (f"the address sends this request on to another server ({e.detail}). Connect again with "
                             "the address the browser ends up at when it opens their 1С.")
        if e.kind == "tls":
            return "tls", (f"the https certificate of their 1С server is not trusted ({e.detail}). The administrator "
                           "puts a proper certificate on the web server (for example a free Let's Encrypt one).")
        return "network", (f"could not reach their 1С ({e.detail[:120]}). The server is off, the address is wrong, "
                           "or it is reachable only inside their office network.")
    pcode, ptext = e.platform()
    code = e.code
    obj = human(e.entity) if e.entity and not e.entity.startswith("$") else "this data"
    if code == 401:
        return "auth", ("1С did not accept the user name or password, or this user may not sign in with a password "
                        "or has no right to the OData interface. Check the user in «Настройки стандартного "
                        "интерфейса OData», tab «Авторизация», then connect again. If it worked before, the "
                        "password was changed.")
    if code == 403:
        doing = "create" if e.method == "POST" else "read"
        return "forbidden", (f"1С does not let the OData user {doing} {obj}. The administrator gives that right "
                             f"({COMPOSITION}{', changing allowed for this document' if doing == 'create' else ''}).")
    if code == 404 and pcode == "8":
        return "not_published", (f"{obj} is not in the OData publication of their 1С. The administrator adds it: "
                                 f"{COMPOSITION} (or УстановитьСоставСтандартногоИнтерфейсаOData), then run "
                                 "status --refresh.")
    if code == 404 and pcode == "9":
        return "not_found", "1С has no such object (it was deleted, or the reference is old)."
    if code in (404, 405, 406) and not pcode:
        return "odata_off", ("the address answers, but 1С's OData interface is not published there. Either the "
                             "address is not the root of the base (use the one the browser opens, without /ru_RU), "
                             "or the administrator republishes it with «Публиковать стандартный интерфейс OData» "
                             "(Конфигуратор → Администрирование → Публикация на веб-сервере). 1С:Фреш has it on "
                             "for every application.")
    if code == 400 and pcode == "6":
        return "no_such_field", (f"their configuration does not have what was asked for in {obj} ({ptext}). It differs "
                                 "from the standard one. Run status --refresh; if it repeats, say this report is not "
                                 "available for their 1С and offer to work from what they paste.")
    if code == 400:
        return "rejected", f"1С did not accept the request ({pcode or code}: {ptext or e.body[:150]})."
    if code >= 500 and (pcode or ptext):
        return "refused", f"1С refused: {ptext or pcode}."
    if code in (502, 503, 504):
        return "unavailable", f"their 1С web server is not answering properly right now ({code}). Try again in a few minutes."
    return "http_error", f"1С answered {code}{': ' + ptext if ptext else ''}."


def guarded(fn, *args, **kw):
    """Run a request; any Http or Net error becomes an ERROR line and exit 2."""
    try:
        return fn(*args, **kw)
    except (Http, Net) as e:
        code, text = explain(e)
        fail(code, text)


# ---- $metadata: what this base publishes --------------------------------------------------------

def short(t):
    t = t or ""
    m = re.match(r"^Collection\((.*)\)$", t)
    if m:
        t = m.group(1)
    return t[len(NS):] if t.startswith(NS) else t.rsplit(".", 1)[-1] if t.startswith("StandardODATA") else t


def candidates():
    names = set()
    for prof in PROFILES.values():
        for kind in KINDS:
            for spec in prof.get(kind, []):
                for k in ("entity", "chart", "store_catalog", "party_catalog", "item_type", "store_type", "party_type"):
                    if spec.get(k):
                        names.add(spec[k])
    names.update(["Catalog_Склады", "Catalog_Партнеры", "Catalog_ДоговорыКонтрагентов"])
    return names


def parse_metadata(raw):
    """A compact index of $metadata: all entity set names; properties of the objects this tool may
    use and of their table rows; the virtual-table methods of those registers and their result fields."""
    import xml.etree.ElementTree as ET
    sets, types, funcs = {}, {}, {}
    tag = lambda el: el.tag.rsplit("}", 1)[-1]
    seen_edmx = False
    try:
        for _, el in ET.iterparse(io.BytesIO(raw), events=("end",)):
            t = tag(el)
            if t in ("EntityType", "ComplexType"):
                types[el.get("Name") or ""] = {p.get("Name") or "": p.get("Type") or ""
                                               for p in el if tag(p) == "Property"}
                el.clear()
            elif t == "EntitySet":
                sets[el.get("Name") or ""] = short(el.get("EntityType"))
            elif t == "FunctionImport":
                params = {p.get("Name"): p.get("Type") for p in el if tag(p) == "Parameter"}
                bound = short(params.get("bindingParameter") or "")
                for tail in ("_RecordType", "_RowType"):
                    if bound.endswith(tail):
                        bound = bound[:-len(tail)]
                if bound:
                    funcs.setdefault(bound, {})[el.get("Name") or ""] = short(el.get("ReturnType") or "")
                el.clear()
            elif t == "EntityContainer":
                seen_edmx = True
    except ET.ParseError:
        raise ValueError("not xml")
    if not seen_edmx and not sets:
        raise ValueError("no entity container")
    want = candidates()
    keep_types, keep_funcs, set_types = {}, {}, {}
    for name in want:
        if name not in sets:
            continue
        tname = sets[name]
        set_types[name] = tname
        props = types.get(tname) or types.get(name) or {}
        keep_types[tname] = props
        for ptype in props.values():
            if ptype.startswith("Collection("):
                row = short(ptype)
                if row in types:
                    keep_types[row] = types[row]
        f = funcs.get(name) or funcs.get(tname) or {}
        if f:
            keep_funcs[name] = f
            for ret in f.values():
                if ret in types:
                    keep_types[ret] = types[ret]
        for tail in ("_Balance", "_Turnover", "_BalanceAndTurnover"):   # by name, if the binding was not read
            if name + tail in types:
                keep_types.setdefault(name + tail, types[name + tail])
    return {"sets": sorted(sets), "set_types": set_types, "types": keep_types, "funcs": keep_funcs}


def detect(sets):
    have = set(sets)
    scores = {pid: sum(1 for s in prof["signature"] if s in have) for pid, prof in PROFILES.items()}
    best = max(scores, key=lambda k: scores[k])
    return best if scores[best] >= 2 else ""


def fetch_meta(cfg):
    status, raw = request_raw(cfg, "GET", "$metadata", timeout=META_TIMEOUT, accept="application/xml",
                              what="the OData description", entity="$metadata")
    meta = parse_metadata(raw)
    meta.update({"v": META_VERSION, "url": cfg["ONEC_URL"], "fetched": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")})
    meta["config"] = detect(meta["sets"])
    return meta


def props_of(meta, entity):
    return meta["types"].get(meta["set_types"].get(entity, entity)) or {}


def row_props(meta, entity, table):
    t = props_of(meta, entity).get(table, "")
    return meta["types"].get(short(t)) or {} if t.startswith("Collection(") else {}


def result_props(meta, entity, func):
    """Fields of a virtual table's answer (Balance -> <register>_Balance), from $metadata."""
    ret = (meta["funcs"].get(entity) or {}).get(func) or entity + "_" + func
    return meta["types"].get(ret) or {}


def specs(meta, kind):
    """Specs of this kind whose object the base publishes: the detected configuration first."""
    order = [meta.get("config")] if meta.get("config") in PROFILES else []
    order += [p for p in PROFILES if p not in order]
    have, found, seen = set(meta["sets"]), [], set()
    for pid in order:
        for spec in PROFILES[pid].get(kind, []):
            key = (spec["entity"], spec.get("side", ""))
            if spec["entity"] in have and key not in seen:
                seen.add(key)
                found.append(dict(spec, profile=pid))
    return found


def missing(meta, kind):
    wanted = []
    for spec in (PROFILES.get(meta.get("config"), {}).get(kind) or
                 [s for p in PROFILES.values() for s in p.get(kind, [])]):
        if spec["entity"] not in wanted:
            wanted.append(spec["entity"])
    prefixes, words = KIND_NEAR[kind]
    near = [s for s in meta["sets"] if s.startswith(prefixes) and any(w in s for w in words)
            and s.count("_") == 1][:8]
    text = (f"their 1С publishes no {KIND_EN[kind]}: none of {', '.join(human(w) for w in wanted[:3])}. "
            + (f"What it has nearby: {', '.join(near)}. " if near else "")
            + f"If the configuration has it, the administrator adds it in {COMPOSITION}, then run status --refresh.")
    fail("not_in_publication", text)


def need(refresh=False):
    cfg = load()
    if not (cfg.get("ONEC_URL") and cfg.get("ONEC_USER") and cfg.get("ONEC_PASSWORD")):
        fail("not_connected", "1С is not connected. Offer to connect it (1c skill, Connect), or ask them to paste "
             "what they see in 1С.")
    meta = None if refresh else load_meta(cfg)
    if meta is None:
        try:
            meta = fetch_meta(cfg)
        except (Http, Net) as e:
            code, text = explain(e)
            fail(code, text)
        except ValueError:
            fail("odata_off", explain(Http(404, "", entity="$metadata"))[1])
        save_meta(meta)
    return cfg, meta


# ---- small helpers --------------------------------------------------------------------------------

def lit(s):
    return "'" + str(s).replace("'", "''") + "'"


def dtlit(d, end=False):
    return f"datetime'{d:%Y-%m-%d}T{'23:59:59' if end else '00:00:00'}'"


def is_guid(s):
    return bool(re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", str(s or "")))


def real(k):
    return is_guid(k) and k != ZERO


def num(x):
    try:
        x = float(x)
    except (TypeError, ValueError):
        return "?"
    if abs(x - round(x)) < 0.005:
        return f"{int(round(x)):,}".replace(",", " ")
    return f"{x:,.2f}".replace(",", " ")


def money(x):
    return f"{num(x)} ₽"


def fnum(x):
    try:
        return float(x or 0)
    except (TypeError, ValueError):
        return 0.0


def many(n, one, more=""):
    return f"{num(n)} {one if n == 1 else (more or one + 's')}"


def clip(text, n=70):
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text if len(text) <= n else text[:n - 1] + "…"


def norm(s):
    return re.sub(r"\s+", " ", str(s or "")).strip().lower().replace("ё", "е")


def key_field(props, name):
    """The field that holds a reference: Name_Key, or Name with Name_Type when it is composite."""
    if not name:
        return ""
    if not props or f"{name}_Key" in props:
        return f"{name}_Key"
    if name in props:
        return name
    return ""


def first(props, names, default=""):
    for n in names:
        if n in props:
            return n
    return default if not props else ""


def names_of(cfg, meta, entity, keys, fields=("Description",)):
    """Ref_Key -> {field: value} for a catalogue, NAME_BATCH keys a request. Names are a nicety:
    an error or an unpublished catalogue leaves them out."""
    keys = sorted({k for k in keys if real(k)})
    if not keys or entity not in meta["sets"]:
        return {}
    props = props_of(meta, entity)
    sel = ["Ref_Key"] + [f for f in fields if not props or f in props]
    found = {}
    for i in range(0, len(keys), NAME_BATCH):
        part = keys[i:i + NAME_BATCH]
        try:
            r = get(cfg, entity, {"$filter": " or ".join(f"Ref_Key eq guid'{k}'" for k in part),
                                  "$select": ",".join(sel)}, what="names")
        except (Http, Net):
            return found
        for row in r.get("value") or []:
            found[row.get("Ref_Key")] = row
    return found


def label(names, key, short_key=True):
    row = names.get(key) or {}
    name = row.get("Description")
    if name:
        return clip(name, 60)
    if not real(key):
        return "(not set)"
    return f"(no name: {key[:8]})" if short_key else key


def tomorrow_lit():
    return dtlit(datetime.now(MSK).date() + timedelta(days=1))


def parse_day(s, label_):
    s = (s or "").strip()
    today = datetime.now(MSK).date()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d.%m.%y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    m = re.fullmatch(r"(\d{1,2})\.(\d{1,2})", s)
    if m:
        try:
            d = date(today.year, int(m.group(2)), int(m.group(1)))
            return d if d <= today else date(today.year - 1, d.month, d.day)
        except ValueError:
            pass
    fail("bad_date", f"{label_} «{s}» is not a date. Use 2026-10-01 or 01.10.2026.")


# ---- connect / status / forget ----------------------------------------------------------------------

def cmd_connect(a):
    raw = "" if sys.stdin.isatty() else sys.stdin.read()
    msgs = [m.strip() for item in (a.msg or []) for m in item.split(",") if m.strip()]
    url_text, user, password = parse_creds(raw, a.url, a.user)
    root, url_user, url_pw = normalize_url(url_text) if url_text else (None, "", "")
    user, password = user or url_user, password or url_pw
    note = forget_message(a.chat, msgs, [raw.strip(), password, url_pw]) if (a.chat or msgs or raw) else ""
    tail = [note] if note else []
    if not url_text:
        fail("no_url", "no address of their 1С came in. Ask for the address their browser opens 1С at "
             "(for 1С:Фреш: https://1cfresh.com/a/…/…), then connect again with it and the user and password.", tail)
    if not root:
        fail("bad_url", "the address could not be read. Ask for the full address from the browser, starting "
             "with https://.", tail)
    if not user:
        fail("no_user", "the user name is missing. Ask for it and connect again with the address, the user and "
             "the password together (or the user with --user).", tail)
    if not password:
        fail("no_password", "the password is missing. Ask for it; the OData user must have a password. Connect "
             "again with the address, the user and the password together.", tail)
    cfg = {"ONEC_URL": root, "ONEC_USER": user, "ONEC_PASSWORD": password}
    t0 = time.time()
    try:
        meta = fetch_meta(cfg)
    except (Http, Net) as e:
        code, text = explain(e)
        fail(code, text + " Nothing is stored.", local_hint(root) + tail if isinstance(e, Net) else tail)
    except ValueError:
        fail("odata_off", explain(Http(404, "", entity="$metadata"))[1] + " Nothing is stored.", tail)
    check_line = probe(cfg, meta)
    if check_line.startswith("ERROR auth"):
        fail("auth", explain(Http(401, ""))[1] + " Nothing is stored.", tail)
    old = load()
    replaced = old.get("ONEC_URL") if old.get("ONEC_PASSWORD") and old.get("ONEC_URL") != root else ""
    save({"ONEC_URL": root, "ONEC_USER": user, "ONEC_PASSWORD": password, "ONEC_CONFIG": meta["config"],
          "ONEC_CONNECTED": meta["fetched"]})
    save_meta(meta)
    conf = PROFILES[meta["config"]]["name"] if meta["config"] else "not recognised (a custom or less common one)"
    lines = [f"OK connected 1С at {root} as «{user}» ({len(meta['sets'])} objects published, "
             f"{time.time() - t0:.0f} s). Configuration: {conf}."]
    lines += coverage(meta)
    if check_line.startswith("ERROR"):
        lines.append("WARNING reading data failed: " + check_line.split(": ", 1)[-1])
    if root.startswith("http://"):
        lines.append("WARNING the address is http, not https: the password travels unencrypted. Ask the "
                     "administrator for https.")
    if replaced:
        lines.append(f"It replaces the connection to {replaced}.")
    out(*lines, *tail)


def coverage(meta):
    have, lack = [], []
    for kind in ("goods", "parties", "sales", "invoice", "stock", "debts"):
        found = specs(meta, kind)
        if found:
            names = []
            for s in found:
                n = s["entity"] + (f" ({s['side']})" if s.get("side") else "")
                if n not in names:
                    names.append(n)
            have.append(f"{KIND_EN[kind]}: {', '.join(names)}")
        else:
            lack.append(kind)
    lines = ["Found: " + "; ".join(have) + "." if have else "Found: none of the objects this tool uses."]
    if lack:
        wanted = []
        for kind in lack:
            for spec in PROFILES.get(meta.get("config"), {}).get(kind) or []:
                wanted.append(human(spec["entity"]))
        lines.append(f"Missing: {', '.join(KIND_EN[k] for k in lack)}."
                     + (f" The administrator adds {', '.join(dict.fromkeys(wanted))} in {COMPOSITION}." if wanted else ""))
    inv = specs(meta, "invoice")
    if inv and not row_props(meta, inv[0]["entity"], inv[0]["table"]):
        lines.append(f"{human(inv[0]['entity'])} has no goods table «{inv[0]['table']}» here: invoices cannot be "
                     "made through this connection.")
    return lines


def cmd_status(a):
    cfg = load()
    if not cfg.get("ONEC_PASSWORD"):
        out("1С: not connected.")
        return
    cfg, meta = need(refresh=a.refresh)
    conf = PROFILES[meta["config"]]["name"] if meta.get("config") else "not recognised"
    lines = [f"1С: connected at {cfg['ONEC_URL']} as «{cfg['ONEC_USER']}». Configuration: {conf}. "
             f"Description of the objects read {meta['fetched'][:16].replace('T', ' ')} UTC"
             + (" (just now)." if a.refresh else "; status --refresh reads it again.")]
    line = probe(cfg, meta)
    lines.append(line)
    if line.startswith("ERROR"):
        out(*lines)
        sys.exit(2)
    out(*lines, *coverage(meta))


def probe(cfg, meta):
    """Read one row of a catalogue: the user can sign in and read data, not only the description."""
    ent = next((s["entity"] for k in ("goods", "parties", "orgs") for s in specs(meta, k)), "")
    ent = ent or next((s for s in meta["sets"] if s.startswith("Catalog_")), "")
    if not ent:
        return "Reachable: yes (no catalogue to read a row from)."
    t0 = time.time()
    try:
        get(cfg, ent, {"$top": "1", "$select": "Ref_Key"}, what="a check", entity=ent)
    except (Http, Net) as e:
        code, text = explain(e)
        return f"ERROR {code}: {text}"
    return f"Reachable: yes, {human(ent)} answered in {time.time() - t0:.1f} s."


def cmd_forget(a):
    cfg = load()
    if not cfg.get("ONEC_PASSWORD"):
        out("1С: not connected.")
        return
    save({})
    out(f"OK forgot 1С at {cfg.get('ONEC_URL')}. This agent can no longer reach it.",
        "To switch the access off in 1С too: in «Настройки стандартного интерфейса OData», tab «Авторизация», "
        f"the administrator changes the password of «{cfg.get('ONEC_USER')}» or switches that user off "
        "(Администрирование → Пользователи, «Вход в программу разрешен»).")


# ---- find --------------------------------------------------------------------------------------------

def search(cfg, meta, kind, text, limit=10):
    """(spec, rows) of goods or counterparties matching the text: exact ИНН, code or article, or a
    part of the name."""
    found = specs(meta, kind)
    if not found:
        return None, []
    spec = found[0]
    ent = spec["entity"]
    props = props_of(meta, ent)
    have = lambda f: f in props if props else f in ("Ref_Key", "Description")
    t = text.strip()
    conds = []
    if kind == "parties" and re.fullmatch(r"\d{10}|\d{12}", t) and have("ИНН"):
        conds.append(f"ИНН eq {lit(t)}")
    for f in ("Code", "Артикул"):
        if have(f) and len(t) <= 50:
            conds.append(f"{f} eq {lit(t)}")
    conds.append(f"substringof({lit(t)}, Description)")
    if kind == "parties" and have("НаименованиеПолное"):
        conds.append(f"substringof({lit(t)}, НаименованиеПолное)")
    filt = "(" + " or ".join(conds) + ")"
    if have("DeletionMark"):
        filt += " and DeletionMark eq false"
    if have("IsFolder"):
        filt += " and IsFolder eq false"
    sel = [f for f in ("Ref_Key", "Code", "Description", "Артикул", "ИНН", "КПП", spec.get("party_contract"),
                       "ОсновнойДоговорКонтрагента_Key", "ДоговорПоУмолчанию_Key") if f and have(f)]
    r = get(cfg, ent, {"$filter": filt, "$select": ",".join(dict.fromkeys(sel)), "$top": str(limit + 1)},
            what=KIND_EN[kind], entity=ent)
    return spec, r.get("value") or []


def show_row(kind, row):
    if kind == "parties":
        extra = [f"ИНН {row['ИНН']}" if row.get("ИНН") else "", f"КПП {row['КПП']}" if row.get("КПП") else ""]
    else:
        extra = [f"code {row['Code']}" if row.get("Code") else "", f"article {row['Артикул']}" if row.get("Артикул") else ""]
    extra = ", ".join(x for x in extra if x)
    return f"{clip(row.get('Description'), 80)}" + (f" | {extra}" if extra else "")


def cmd_find(a):
    cfg, meta = need()
    t = " ".join(a.text).strip()
    if not t:
        fail("no_text", "say what to look for: a name, a code, an article or an ИНН.")
    raw, lines, any_kind = {}, [], False
    for kind, title in (("goods", "Goods"), ("parties", "Counterparties")):
        try:
            spec, rows = search(cfg, meta, kind, t, a.limit)
        except (Http, Net) as e:
            code, text = explain(e)
            lines.append(f"ERROR {code}: {text}")
            raw[kind] = {"error": code}
            continue
        if spec is None:
            lines.append(f"{title}: not in the publication.")
            continue
        any_kind = True
        raw[kind] = rows
        more = len(rows) > a.limit
        rows = rows[:a.limit]
        lines.append(f"{title}: {len(rows)}{'+' if more else ''} found for «{t}»." if rows else f"{title}: nothing for «{t}».")
        lines += [show_row(kind, r) for r in rows]
        if more:
            lines.append(f"... more; give a longer part of the name, or --limit {a.limit * 3}.")
    if a.json:
        print(json.dumps(raw, ensure_ascii=False, indent=1))
    else:
        out(*lines)
    if not any_kind and not a.json:
        missing(meta, "goods")
    if any(l.startswith("ERROR") for l in lines):
        sys.exit(2)


def resolve(cfg, meta, kind, text, what):
    spec, rows = guarded(search, cfg, meta, kind, text, 10)
    if spec is None:
        missing(meta, kind)
    if not rows:
        fail("not_found", f"no {what} matches «{text}». Check the spelling, or look it up with find.")
    t = norm(text)
    exact = [r for r in rows if t in (norm(r.get("Description")), norm(r.get("ИНН")), norm(r.get("Code")),
                                      norm(r.get("Артикул")))]
    if len(exact) == 1:
        return spec, exact[0]
    if len(rows) == 1:
        return spec, rows[0]
    fail("ambiguous", f"«{text}» fits {len(rows)}{'+' if len(rows) > 10 else ''} {what}s; ask the owner which one, "
         "and use its exact name, code or ИНН: " + "; ".join(show_row(kind, r) for r in (exact or rows)[:6]))


# ---- stock -------------------------------------------------------------------------------------------

def cmd_stock(a):
    cfg, meta = need()
    found = specs(meta, "stock")
    if not found:
        missing(meta, "stock")
    spec = found[0]
    keys = None
    if a.item:
        gspec, rows = guarded(search, cfg, meta, "goods", a.item, 30)
        if gspec is None:
            missing(meta, "goods")
        keys = {r["Ref_Key"] for r in rows if r.get("Ref_Key")}
        if not keys:
            fail("not_found", f"no item matches «{a.item}». Look it up with find.")
    try:
        rows, raw, src, note = (stock_accounting if spec["type"] == "accounting" else stock_accumulation)(cfg, meta, spec)
    except (Http, Net) as e:
        code, text = explain(e)
        fail(code, text)
    items = {}
    for r in rows:
        k = r["item"]
        if keys is not None and k not in keys:
            continue
        it = items.setdefault(k, {"qty": 0.0, "where": {}, "reserved": 0.0})
        it["qty"] += r["qty"]
        it["reserved"] += r.get("reserved", 0.0)
        if r.get("store"):
            it["where"][r["store"]] = it["where"].get(r["store"], 0.0) + r["qty"]
    if a.json:
        print(json.dumps(raw, ensure_ascii=False, indent=1))
        return
    if not items:
        what = f" for «{a.item}»" if a.item else ""
        out(f"Stock ({src}): nothing on hand{what}.", note)
        return
    order = sorted(items.items(), key=lambda kv: kv[1]["qty"])
    low = sum(1 for _, v in order if v["qty"] <= a.low)
    neg = sum(1 for _, v in order if v["qty"] < 0)
    if keys is not None or a.all:
        shown = order[:300]
    else:
        shown = order[:max(30, low)]
    gent = (specs(meta, "goods") or [{"entity": "Catalog_Номенклатура"}])[0]["entity"]
    names = names_of(cfg, meta, gent, [k for k, _ in shown], ("Description", "Code", "Артикул"))
    stores = names_of(cfg, meta, spec.get("store_catalog") or spec.get("store_type") or "",
                      {s for _, v in shown for s in v["where"]})
    lines = [f"Stock ({src}): {many(len(items), 'item')}, {low} at or below {num(a.low)}"
             + (f", {neg} below zero" if neg else "") + ". item | quantity | where"]
    for k, v in shown:
        row = names.get(k) or {}
        code = row.get("Артикул") or row.get("Code")
        where = ", ".join(f"{label(stores, s)} {num(q)}" for s, q in sorted(v["where"].items(), key=lambda x: -x[1]) if q)
        flags = [f for f, on in (("BELOW ZERO", v["qty"] < 0), ("LOW", 0 <= v["qty"] <= a.low)) if on]
        lines.append(f"{label(names, k)}{f' ({code})' if code else ''} | {num(v['qty'])}"
                     + (f" | {where}" if where else "")
                     + (f" | {num(v['reserved'])} reserved" if v["reserved"] else "")
                     + (" | " + ", ".join(flags) if flags else ""))
    if len(shown) < len(order):
        lines.append(f"... {len(order) - len(shown)} more with more stock; stock --all lists them.")
    out(*lines, note)


def stock_accumulation(cfg, meta, spec):
    reg = spec["entity"]
    rp = result_props(meta, reg, "Balance")
    qty = spec["qty"] + "Balance"
    if rp:   # the fields are known: the expected one, or the first balance there is
        qty = first(rp, [qty]) or next((p for p in rp if p.endswith("Balance")), "")
    if not qty:
        fail("no_such_field", f"{human(reg)} has no quantity balance here; their configuration differs.")
    item_f = key_field(rp, spec["item"])
    store_f = key_field(rp, spec.get("store"))
    res_f = first(rp, [spec["reserved"] + "Balance"]) if spec.get("reserved") else ""
    if not item_f:
        fail("no_such_field", f"{human(reg)} has no «{spec['item']}» here; their configuration differs.")
    dims = [d for d, f in ((spec["item"], item_f), (spec.get("store"), store_f)) if d and f]
    args = f"Period={tomorrow_lit()}" + (f",Dimensions='{','.join(dims)}'" if dims else "")
    params = {"$select": ",".join(f for f in (item_f, store_f, qty, res_f) if f)} if rp else {}
    r = get(cfg, f"{reg}/Balance({args})", params, what="stock", entity=reg)
    rows = [{"item": x.get(item_f), "store": x.get(store_f) if store_f else "", "qty": fnum(x.get(qty)),
             "reserved": fnum(x.get(res_f)) if res_f else 0.0} for x in r.get("value") or []]
    src = human(reg)
    note = "Quantities are 1С's balance as of now, from posted documents."
    return rows, r, src, note


def chart_codes(cfg, meta, spec, prefixes, fallback):
    """Account codes under the given prefixes, read from the chart of accounts when it is published."""
    chart = spec.get("chart")
    if chart and chart in meta["sets"]:
        try:
            r = get(cfg, chart, {"$filter": " or ".join(f"startswith(Code,{lit(p)})" for p in prefixes),
                                 "$select": "Ref_Key,Code,Description"}, what="chart of accounts", entity=chart)
            codes = sorted({x["Code"] for x in r.get("value") or [] if x.get("Code")})
            if codes:
                return codes[:40]   # the filter must fit in a 2 KB query string
        except (Http, Net):
            pass
    return list(fallback)


def ext_value(row, type_name):
    """The value of the ExtDimension (субконто) whose type is the given catalogue."""
    for i in (1, 2, 3):
        t = str(row.get(f"ExtDimension{i}_Type") or "")
        if t == NS + type_name or t == type_name:
            return row.get(f"ExtDimension{i}")
    return None


def balance_accounting(cfg, meta, spec, codes, fields, what):
    reg = spec["entity"]
    rp = result_props(meta, reg, "Balance")
    sel = ["Account_Key"] + [f"ExtDimension{i}{s}" for i in (1, 2, 3) for s in ("", "_Type")] + fields
    params = {"$filter": " or ".join(f"Account/Code eq {lit(c)}" for c in codes)}
    if rp:
        params["$select"] = ",".join(f for f in sel if f in rp)
    return get(cfg, f"{reg}/Balance(Period={tomorrow_lit()})", params, what=what, entity=reg)


def stock_accounting(cfg, meta, spec):
    codes = chart_codes(cfg, meta, spec, spec["accounts"], spec["fallback"])
    r = balance_accounting(cfg, meta, spec, codes, ["КоличествоBalance", "СуммаBalance"], "stock")
    rows = []
    for x in r.get("value") or []:
        item = ext_value(x, spec["item_type"])
        if not item:
            continue
        rows.append({"item": item, "store": ext_value(x, spec["store_type"]) or "", "qty": fnum(x.get("КоличествоBalance"))})
    src = f"accounting, accounts {', '.join(spec['accounts'])}"
    note = ("From accounting (БП): accounts " + ", ".join(codes[:8]) + ", posted documents only; materials (10) "
            "are not counted. Warehouses show only where their 1С keeps stock by warehouse.")
    return rows, r, src, note


# ---- sales ----------------------------------------------------------------------------------------

def cmd_sales(a):
    cfg, meta = need()
    found = specs(meta, "sales")
    if not found:
        missing(meta, "sales")
    spec = found[0]
    doc = spec["entity"]
    today = datetime.now(MSK).date()
    if a.date_from or a.date_to:
        end = parse_day(a.date_to, "--to") if a.date_to else today
        start = parse_day(a.date_from, "--from") if a.date_from else end - timedelta(days=29)
    else:
        if a.days < 1:
            fail("bad_days", "--days is 1 or more.")
        end, start = today, today - timedelta(days=a.days - 1)
    if start > end:
        fail("bad_period", "the period starts after it ends.")
    if (end - start).days > 366:
        fail("bad_period", "at most a year at once; ask for it in parts.")
    hp = props_of(meta, doc)
    party_f = first(hp, ["Контрагент_Key", "Партнер_Key"], "Контрагент_Key")
    total_f = first(hp, ["СуммаДокумента"], "СуммаДокумента")
    tables = [t for t in spec["tables"] if t in hp] if hp else []
    sel = [f for f in ("Ref_Key", "Number", "Date", party_f, total_f) if f] + tables
    params = {"$filter": f"Posted eq true and Date ge {dtlit(start)} and Date le {dtlit(end, True)}",
              "$select": ",".join(sel)}
    try:
        docs, pages, cut = get_all(cfg, doc, params, what="sales", order="Date")
    except Http as e:
        if e.code != 400 or not tables:
            code, text = explain(e)
            fail(code, text)
        tables = []   # some publications do not give table rows in a list: totals only
        params["$select"] = ",".join(f for f in sel if f not in spec["tables"])
        docs, pages, cut = guarded(get_all, cfg, doc, params, what="sales", order="Date")
    except Net as e:
        code, text = explain(e)
        fail(code, text)
    if a.json:
        print(json.dumps(pages, ensure_ascii=False, indent=1))
        return
    title = f"Sales, {start:%d.%m.%Y} to {end:%d.%m.%Y} ({human(doc)}, posted only)"
    if not docs:
        out(f"{title}: no documents.")
        return
    total, by_party, by_item = 0.0, {}, {}
    for d in docs:
        rows_sum = 0.0
        for t in tables:
            rp = row_props(meta, doc, t)
            item_f = key_field(rp, "Номенклатура")
            amt_f = first(rp, ["СуммаСНДС", "Всего", "Сумма"], "Сумма")
            for row in d.get(t) or []:
                amt = fnum(row.get(amt_f))
                rows_sum += amt
                k = row.get(item_f) if item_f else None
                if real(k):
                    v = by_item.setdefault(k, [0.0, 0.0])
                    v[0] += amt
                    v[1] += fnum(row.get("Количество"))
        amount = fnum(d.get(total_f)) if total_f in d else rows_sum
        total += amount
        p = by_party.setdefault(d.get(party_f) or ZERO, [0.0, 0])
        p[0] += amount
        p[1] += 1
    top_p = sorted(by_party.items(), key=lambda kv: -kv[1][0])[:a.top]
    top_i = sorted(by_item.items(), key=lambda kv: -kv[1][0])[:a.top]
    pent = "Catalog_Партнеры" if party_f == "Партнер_Key" else (specs(meta, "parties") or [{"entity": "Catalog_Контрагенты"}])[0]["entity"]
    pn = names_of(cfg, meta, pent, [k for k, _ in top_p])
    gent = (specs(meta, "goods") or [{"entity": "Catalog_Номенклатура"}])[0]["entity"]
    gn = names_of(cfg, meta, gent, [k for k, _ in top_i])
    lines = [f"{title}: {len(docs)}{'+' if cut else ''} document{'' if len(docs) == 1 else 's'}, {money(total)}."]
    lines.append("Top customers: " + "; ".join(f"{label(pn, k)} {money(v[0])} ({v[1]})" for k, v in top_p))
    if top_i:
        lines.append("Top items: " + "; ".join(f"{label(gn, k)} {money(v[0])} / {num(v[1])} pcs" for k, v in top_i))
    elif spec["tables"]:
        lines.append("Top items: not available (no table rows in this publication).")
    if cut:
        lines.append(f"Only the first {len(docs)} documents were read; ask for a shorter period for the rest.")
    lines.append("Approximate: document totals in each document's currency; returns and corrections are not "
                 "subtracted. Exact figures are in 1С's sales reports.")
    out(*lines)


# ---- debts ----------------------------------------------------------------------------------------

def cmd_debts(a):
    cfg, meta = need()
    found = specs(meta, "debts")
    acc = [s for s in found if s["type"] == "accounting"]
    raw, ours, theirs, sources = {}, {}, {}, []   # ours: they owe us; theirs: we owe them
    party_entity = (specs(meta, "parties") or [{"entity": "Catalog_Контрагенты"}])[0]["entity"]
    try:
        if acc:
            spec = acc[0]
            for side in ("receivable", "payable"):
                codes = chart_codes(cfg, meta, spec, [spec[side]], spec["fallback"][side])
                r = balance_accounting(cfg, meta, spec, codes, ["СуммаBalanceDr", "СуммаBalanceCr"], "debts")
                raw[side] = r
                for x in r.get("value") or []:
                    p = ext_value(x, spec["party_type"]) or ZERO
                    v = fnum(x.get("СуммаBalanceDr")) - fnum(x.get("СуммаBalanceCr"))
                    v = v if side == "receivable" else -v
                    book(ours, theirs, side, p, v)
                sources.append(f"accounts {', '.join(codes[:6])}")
        else:
            if not found:
                missing(meta, "debts")
            for side in ("receivable", "payable"):
                # The first register of this side that has the expected fields in this base.
                spec, rp, party_f, plus, minus, skipped = None, {}, "", [], [], []
                for s in (x for x in found if x.get("side") == side):
                    rp = result_props(meta, s["entity"], "Balance")
                    party_f = key_field(rp, s["party"])
                    plus = [f + "Balance" for f in s["plus"] if not rp or f + "Balance" in rp]
                    minus = [f + "Balance" for f in s["minus"] if not rp or f + "Balance" in rp]
                    if party_f and plus:
                        spec = s
                        break
                    skipped.append(s["entity"])
                if not spec:
                    sources.append(f"no {side} register usable here" + (f" ({', '.join(skipped)} differ from the "
                                                                        "standard ones)" if skipped else ""))
                    continue
                reg = spec["entity"]
                args = f"Period={tomorrow_lit()},Dimensions='{spec['party']}'"
                params = {"$select": ",".join([party_f] + plus + minus)} if rp else {}
                r = get(cfg, f"{reg}/Balance({args})", params, what="debts", entity=reg)
                raw[side] = r
                for x in r.get("value") or []:
                    v = sum(fnum(x.get(f)) for f in plus) - sum(fnum(x.get(f)) for f in minus)
                    book(ours, theirs, side, x.get(party_f) or ZERO, v)
                sources.append(human(reg))
                party_entity = spec.get("party_catalog") or party_entity
    except (Http, Net) as e:
        code, text = explain(e)
        fail(code, text)
    if a.json:
        print(json.dumps(raw, ensure_ascii=False, indent=1))
        return
    top_o = sorted(ours.items(), key=lambda kv: -kv[1][0])
    top_t = sorted(theirs.items(), key=lambda kv: -kv[1][0])
    names = names_of(cfg, meta, party_entity, [k for k, _ in top_o[:a.top] + top_t[:a.top]])

    def block(title, items):
        if not items:
            return [f"{title}: nobody."]
        total = sum(v[0] for _, v in items)
        lines = [f"{title}: {many(len(items), 'counterparty', 'counterparties')}, {money(total)}."]
        for k, v in items[:a.top]:
            lines.append(f"{label(names, k)} | {money(v[0])}" + (f" | {v[1]}" if v[1] else ""))
        if len(items) > a.top:
            lines.append(f"... {len(items) - a.top} more; --top {a.top * 3} lists them.")
        return lines

    out(*block("Owed to us", top_o), *block("We owe", top_t),
        "Approximate: " + "; ".join(sources) + ". Posted documents only; other settlements (loans, staff, "
        "taxes, other debtors) are not counted. Exact figures: 1С's settlement reports or a reconciliation act.")


def book(ours, theirs, side, party, v):
    """A receivable below zero is a customer's prepayment (we owe goods); a payable below zero is our
    prepayment to a supplier (they owe us goods)."""
    if abs(v) < 0.005:
        return
    if side == "receivable":
        target, note = (ours, "") if v > 0 else (theirs, "their prepayment")
    else:
        target, note = (theirs, "") if v > 0 else (ours, "our prepayment")
    cur = target.setdefault(party, [0.0, ""])
    cur[0] += abs(v)
    if note and not cur[1]:
        cur[1] = note


# ---- invoice --------------------------------------------------------------------------------------

def parse_line(s):
    parts = s.rsplit(":", 2)
    if len(parts) != 3 or not parts[0].strip():
        fail("bad_line", f"«{s}» is not ITEM:QTY:PRICE, for example «Кружка белая:10:350».")
    name, q, p = parts[0].strip(), parts[1], parts[2]
    p = re.sub(r"(₽|руб\.?|р\.?|rub)$", "", re.sub(r"\s+", "", p), flags=re.I)
    try:
        qty = float(re.sub(r"\s+", "", q).replace(",", "."))
        price = float(p.replace(",", ".") or "x")
    except ValueError:
        fail("bad_line", f"in «{s}» the quantity and the price must be numbers, like «Кружка:10:350.50».")
    if qty <= 0 or price < 0:
        fail("bad_line", f"in «{s}» the quantity must be above zero and the price not below zero.")
    return name, qty, price


def cmd_invoice(a):
    cfg, meta = need()
    found = specs(meta, "invoice")
    if not found:
        missing(meta, "invoice")
    spec = found[0]
    doc, table = spec["entity"], spec["table"]
    hp, rp = props_of(meta, doc), row_props(meta, doc, table)
    if not rp:
        near = [s for s in meta["sets"] if s.startswith(doc + "_")]
        fail("no_goods_table", f"{human(doc)} has no goods table «{table}» in their 1С"
             + (f" (it has: {', '.join(n[len(doc) + 1:] for n in near[:5])})" if near else "")
             + ". Invoices there are made in 1С itself, usually from a customer order.")
    if len(a.lines) > 50:
        fail("too_many_lines", "at most 50 lines in one invoice here.")
    lines = [parse_line(s) for s in a.lines]
    pspec, party = resolve(cfg, meta, "parties", a.counterparty, "counterparty")
    goods = []
    for name, qty, price in lines:
        gspec, item = resolve(cfg, meta, "goods", name, "item")
        goods.append((item, qty, price))
    org = None
    org_specs = specs(meta, "orgs")
    if "Организация_Key" in hp and org_specs:
        oent = org_specs[0]["entity"]
        oprops = props_of(meta, oent)
        osel = ["Ref_Key", "Description"] + [f for f in (spec.get("from_org") or {}).values() if f in oprops]
        flt = "DeletionMark eq false" if "DeletionMark" in oprops else ""
        if a.org:
            flt = (flt + " and " if flt else "") + f"substringof({lit(a.org)}, Description)"
        r = guarded(get, cfg, oent, {"$filter": flt, "$select": ",".join(osel), "$top": "20"},
                    what="organizations", entity=oent)
        orgs = r.get("value") or []
        if len(orgs) == 1:
            org = orgs[0]
        elif not orgs:
            fail("no_org", f"no organization{' matches «' + a.org + '»' if a.org else ' is published'}. "
                 f"Check --org, or the administrator adds {human(oent)} in {COMPOSITION}.")
        else:
            fail("which_org", "their 1С has several organizations; ask the owner which one issues the invoice "
                 "and add --org NAME: " + "; ".join(clip(o.get("Description"), 50) for o in orgs[:8]))
    item_f = key_field(rp, "Номенклатура")
    gent = (specs(meta, "goods") or [{"entity": "Catalog_Номенклатура"}])[0]["entity"]
    rows, total = [], 0.0
    for i, (item, qty, price) in enumerate(goods, 1):
        amount = round(qty * price, 2)
        total += amount
        row = {"LineNumber": str(i)}   # 1С wants the lines numbered from 1, in order
        if item_f == "Номенклатура_Key":
            row[item_f] = item["Ref_Key"]
        elif item_f:   # a composite field: the reference plus its type
            row[item_f] = item["Ref_Key"]
            row[item_f + "_Type"] = NS + gent
        for f, v in (("Количество", qty), ("Цена", price), ("Сумма", amount)):
            if f in rp:
                row[f] = v
        rows.append(row)
    body = {"Контрагент_Key": party["Ref_Key"], table: rows}
    if "Контрагент_Key" not in hp:
        fail("no_such_field", f"{human(doc)} has no counterparty field here; their configuration differs.")
    if org:
        body["Организация_Key"] = org["Ref_Key"]
        for field, ofield in (spec.get("from_org") or {}).items():
            if field in hp and real(org.get(ofield)):
                body[field] = org[ofield]
    contract = ""
    for cf in (spec.get("party_contract"), "ОсновнойДоговорКонтрагента_Key", "ДоговорПоУмолчанию_Key"):
        if cf and real(party.get(cf)):
            contract = party[cf]
            break
    if spec.get("contract") in hp and contract:
        body[spec["contract"]] = contract
    if "СуммаДокумента" in hp:
        body["СуммаДокумента"] = round(total, 2)
    if "Комментарий" in hp:
        body["Комментарий"] = "Черновик от ИИ-сотрудника OINOA: проверьте и проведите."
    body.pop("Posted", None)   # never posted from here
    who = show_row("parties", party)
    plan = [f"{human(doc)} for {who}" + (f", from {clip(org.get('Description'), 50)}" if org else "")
            + f": {many(len(rows), 'line')}, {money(total)}."]
    for (item, qty, price), row in zip(goods, rows):
        plan.append(f"{row['LineNumber']}. {clip(item.get('Description'), 60)} | {num(qty)} x {money(price)} = {money(qty * price)}")
    vat = any(f in rp for f in ("СтавкаНДС", "СтавкаНДС_Key", "СуммаНДС"))
    caveats = ("VAT is not filled in through this connection: the owner checks the VAT column in 1С. " if vat else "") + \
              ("No contract is set: the owner picks it in 1С. " if spec.get("contract") in hp and not contract else "") + \
              ("No organization is set: the owner picks it in 1С. " if "Организация_Key" in hp and not org else "")
    if not a.yes:
        out("PREVIEW " + plan[0], *plan[1:],
            "Nothing created. It will be a draft (not posted). " + caveats
            + "Show it to the owner; after a clear yes run the same command with --yes.")
        return
    try:
        r = request(cfg, "POST", doc, {"$format": "json"}, body=body, what="the invoice", entity=doc)
    except Net as e:
        code, text = explain(e)
        if code == "timeout":
            fail(code, "1С did not answer in time. The invoice may or may not be created: check the newest "
                 f"{human(doc)} in 1С before trying again.")
        fail(code, text)
    except Http as e:
        code, text = explain(e)
        fail(code, text + " Nothing was created.")
    if a.json:
        print(json.dumps(r, ensure_ascii=False, indent=1))
        return
    number, when = r.get("Number") or "?", str(r.get("Date") or "")[:10]
    out(f"OK created {human(doc)} № {number}" + (f" of {when}" if when else "") + f", {money(total)}, as a draft: "
        "NOT posted.", *plan[1:],
        caveats + "The owner opens it in 1С, checks it, and posts or prints it there.")


# ---- selftest: everything above against a local stub, no network --------------------------------

def selftest():
    import http.server
    import shutil
    import subprocess
    import tempfile
    import threading

    PW = {"bp": "Pa$$ w0rd#Секрет", "unf": "unf-Secret-77", "empty": "p12345678"}
    USERS = {"bp": "Помощник", "unf": "admin", "empty": "u"}
    TG_TOKEN = "123456:TEST-BOT-TOKEN-SECRET"
    G = lambda n: f"{n:08d}-0000-4000-8000-000000000000"
    state = {"log": [], "force": {}, "posts": []}

    def edmx(entities, complexes, funcs):
        x = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<edmx:Edmx xmlns:edmx="http://schemas.microsoft.com/ado/2007/06/edmx" Version="1.0">',
             '<edmx:DataServices xmlns:m="http://schemas.microsoft.com/ado/2007/08/dataservices/metadata" '
             'm:DataServiceVersion="3.0">',
             '<Schema xmlns="http://schemas.microsoft.com/ado/2009/11/edm" Namespace="StandardODATA">']
        for kind, group in (("EntityType", entities), ("ComplexType", complexes)):
            for name, props in group.items():
                x.append(f'<{kind} Name="{name}">' + ('<Key><PropertyRef Name="Ref_Key"/></Key>' if kind == "EntityType" else ""))
                for p, t in props:
                    x.append(f'<Property Name="{p}" Type="{t}" Nullable="true"/>')
                x.append(f"</{kind}>")
        x.append('<EntityContainer Name="EnterpriseV8" m:IsDefaultEntityContainer="true">')
        for name in entities:
            x.append(f'<EntitySet Name="{name}" EntityType="StandardODATA.{name}"/>')
        for fname, bound, ret in funcs:
            x.append(f'<FunctionImport Name="{fname}" ReturnType="Collection(StandardODATA.{ret})" IsBindable="true" '
                     f'IsSideEffecting="false"><Parameter Name="bindingParameter" '
                     f'Type="Collection(StandardODATA.{bound}_RecordType)"/>'
                     '<Parameter Name="Condition" Type="Edm.String"/><Parameter Name="Period" Type="Edm.DateTime"/>'
                     '</FunctionImport>')
        x.append('<FunctionImport Name="Post" IsBindable="true" IsSideEffecting="true"><Parameter '
                 'Name="bindingParameter" Type="StandardODATA.Document_X"/></FunctionImport>')
        x.append("</EntityContainer></Schema></edmx:DataServices></edmx:Edmx>")
        return "\n".join(x).encode("utf-8")

    S, B, D, GU, N, C = "Edm.String", "Edm.Boolean", "Edm.DateTime", "Edm.Guid", "Edm.Double", "Collection(StandardODATA.{})"
    cat = [("Ref_Key", GU), ("DataVersion", S), ("DeletionMark", B), ("IsFolder", B), ("Code", S), ("Description", S)]
    bp_meta = edmx({
        "Catalog_Номенклатура": cat + [("Артикул", S), ("НаименованиеПолное", S)],
        "Catalog_Контрагенты": cat + [("ИНН", S), ("КПП", S), ("НаименованиеПолное", S), ("ОсновнойДоговорКонтрагента_Key", GU)],
        "Catalog_Организации": cat + [("ИНН", S), ("ОсновнойБанковскийСчет_Key", GU)],
        "Catalog_Склады": cat,
        "Catalog_ДоговорыКонтрагентов": cat,
        "ChartOfAccounts_Хозрасчетный": [("Ref_Key", GU), ("Code", S), ("Description", S)],
        "AccountingRegister_Хозрасчетный": [("Period", D), ("Recorder", S), ("AccountDr_Key", GU)],
        "Document_РеализацияТоваровУслуг": [("Ref_Key", GU), ("Number", S), ("Date", D), ("Posted", B),
                                            ("Организация_Key", GU), ("Контрагент_Key", GU), ("ДоговорКонтрагента_Key", GU),
                                            ("СуммаДокумента", N), ("Товары", C.format("Document_РеализацияТоваровУслуг_Товары_RowType")),
                                            ("Услуги", C.format("Document_РеализацияТоваровУслуг_Услуги_RowType"))],
        "Document_РеализацияТоваровУслуг_Товары": [("Ref_Key", GU), ("LineNumber", S)],
        "Document_СчетНаОплатуПокупателю": [("Ref_Key", GU), ("Number", S), ("Date", D), ("Posted", B),
                                            ("Организация_Key", GU), ("Контрагент_Key", GU), ("ДоговорКонтрагента_Key", GU),
                                            ("СтруктурнаяЕдиница_Key", GU), ("СуммаДокумента", N), ("Комментарий", S),
                                            ("Товары", C.format("Document_СчетНаОплатуПокупателю_Товары_RowType"))],
        "Document_ПоступлениеНаРасчетныйСчет": [("Ref_Key", GU)],
    }, {
        "Document_РеализацияТоваровУслуг_Товары_RowType": [("LineNumber", S), ("Номенклатура_Key", GU), ("Количество", N),
                                                           ("Цена", N), ("Сумма", N), ("СтавкаНДС", S), ("СуммаНДС", N)],
        "Document_РеализацияТоваровУслуг_Услуги_RowType": [("LineNumber", S), ("Номенклатура_Key", GU), ("Содержание", S),
                                                           ("Количество", N), ("Сумма", N)],
        "Document_СчетНаОплатуПокупателю_Товары_RowType": [("LineNumber", S), ("Номенклатура", S), ("Номенклатура_Type", S),
                                                           ("Содержание", S), ("Количество", N), ("Цена", N), ("Сумма", N),
                                                           ("СтавкаНДС", S), ("СуммаНДС", N)],
        "AccountingRegister_Хозрасчетный_Balance": [("Account_Key", GU), ("Организация_Key", GU)]
        + [(f"ExtDimension{i}{s}", S) for i in (1, 2, 3) for s in ("", "_Type")]
        + [("СуммаBalance", N), ("СуммаBalanceDr", N), ("СуммаBalanceCr", N), ("КоличествоBalance", N)],
    }, [("Balance", "AccountingRegister_Хозрасчетный", "AccountingRegister_Хозрасчетный_Balance")])

    unf_meta = edmx({
        "Catalog_Номенклатура": cat + [("Артикул", S)],
        "Catalog_Контрагенты": cat + [("ИНН", S), ("КПП", S), ("ДоговорПоУмолчанию_Key", GU)],
        "Catalog_Организации": cat,
        "Catalog_СтруктурныеЕдиницы": cat,
        "ChartOfAccounts_Управленческий": [("Ref_Key", GU), ("Code", S)],
        "Document_ПриходнаяНакладная": [("Ref_Key", GU)],
        "Document_РасходнаяНакладная": [("Ref_Key", GU), ("Number", S), ("Date", D), ("Posted", B), ("Контрагент_Key", GU),
                                        ("Договор_Key", GU), ("СуммаДокумента", N),
                                        ("Запасы", C.format("Document_РасходнаяНакладная_Запасы_RowType"))],
        "Document_СчетНаОплату": [("Ref_Key", GU), ("Number", S), ("Date", D), ("Posted", B), ("Организация_Key", GU),
                                  ("Контрагент_Key", GU), ("Договор_Key", GU), ("СуммаДокумента", N), ("Комментарий", S),
                                  ("Запасы", C.format("Document_СчетНаОплату_Запасы_RowType"))],
        "AccumulationRegister_Запасы": [("Period", D), ("Recorder", S)],
        "AccumulationRegister_РасчетыСПокупателями": [("Period", D)],
        "AccumulationRegister_РасчетыСПоставщиками": [("Period", D)],
    }, {
        "Document_РасходнаяНакладная_Запасы_RowType": [("LineNumber", S), ("Номенклатура_Key", GU), ("Количество", N),
                                                       ("Цена", N), ("Сумма", N), ("Всего", N)],
        "Document_СчетНаОплату_Запасы_RowType": [("LineNumber", S), ("Номенклатура_Key", GU), ("Количество", N),
                                                 ("Цена", N), ("Сумма", N), ("Всего", N)],
        "AccumulationRegister_Запасы_Balance": [("Номенклатура_Key", GU), ("СтруктурнаяЕдиница_Key", GU),
                                                ("КоличествоBalance", N), ("СуммаBalance", N)],
        "AccumulationRegister_РасчетыСПокупателями_Balance": [("Контрагент_Key", GU), ("СуммаBalance", N)],
        "AccumulationRegister_РасчетыСПоставщиками_Balance": [("Контрагент_Key", GU), ("СуммаBalance", N)],
    }, [("Balance", "AccumulationRegister_Запасы", "AccumulationRegister_Запасы_Balance"),
        ("Balance", "AccumulationRegister_РасчетыСПокупателями", "AccumulationRegister_РасчетыСПокупателями_Balance"),
        ("Balance", "AccumulationRegister_РасчетыСПоставщиками", "AccumulationRegister_РасчетыСПоставщиками_Balance")])

    empty_meta = edmx({"Catalog_Номенклатура": cat, "Document_ПоступлениеТоваров": [("Ref_Key", GU)],
                       "Document_СчетФактураПолученный": [("Ref_Key", GU)]}, {}, [])

    # Управление торговлей 11, in-process: detection, the stock register and its Balance fields; and the
    # Balance fields found by name when a base binds its virtual tables in a way this parser does not read.
    ut_reg = "AccumulationRegister_ТоварыНаСкладах"
    ut_ents = {n: [("Ref_Key", GU)] for n in ("Catalog_Партнеры", "Document_ЗаказКлиента", "Catalog_СоглашенияСКлиентами",
                                               "Document_ПриобретениеТоваровУслуг", "Catalog_Номенклатура", ut_reg)}
    ut_bal = {ut_reg + "_Balance": [("Номенклатура_Key", GU), ("Склад_Key", GU), ("ВНаличииBalance", N), ("КОтгрузкеBalance", N)]}
    ut = parse_metadata(edmx(ut_ents, ut_bal, [("Balance", ut_reg, ut_reg + "_Balance")]))
    ut["config"] = detect(ut["sets"])
    st = specs(ut, "stock")
    ut_ok = bool(ut["config"] == "ut11" and st and st[0]["entity"] == ut_reg
                 and "ВНаличииBalance" in result_props(ut, ut_reg, "Balance"))
    ut2 = parse_metadata(edmx(ut_ents, ut_bal, []))
    ut2_ok = "КОтгрузкеBalance" in result_props(ut2, ut_reg, "Balance")

    t_bp = f"StandardODATA.Catalog_"
    data = {
        "bp": {
            "Catalog_Номенклатура": [
                {"Ref_Key": G(1), "Code": "00-001", "Description": "Кружка белая", "Артикул": "KR-1", "IsFolder": False, "DeletionMark": False},
                {"Ref_Key": G(2), "Code": "00-002", "Description": "Кружка синяя", "Артикул": "KR-2", "IsFolder": False, "DeletionMark": False},
                {"Ref_Key": G(3), "Code": "00-003", "Description": "Чайник", "Артикул": "CH-1", "IsFolder": False, "DeletionMark": False}],
            "Catalog_Контрагенты": [
                {"Ref_Key": G(11), "Code": "К-1", "Description": "ООО Ромашка", "ИНН": "7701234567", "КПП": "770101001",
                 "НаименованиеПолное": "Общество с ограниченной ответственностью Ромашка", "ОсновнойДоговорКонтрагента_Key": G(31)},
                {"Ref_Key": G(12), "Code": "К-2", "Description": "ИП Петров", "ИНН": "500100732259", "ОсновнойДоговорКонтрагента_Key": ZERO},
                {"Ref_Key": G(13), "Code": "К-3", "Description": "ООО Поставщик", "ИНН": "7702000000"}],
            "Catalog_Организации": [{"Ref_Key": G(21), "Description": "ООО Наша фирма", "ОсновнойБанковскийСчет_Key": G(41)}],
            "Catalog_Склады": [{"Ref_Key": G(51), "Description": "Основной склад"}],
            "ChartOfAccounts_Хозрасчетный": [
                {"Ref_Key": G(61), "Code": "41"}, {"Ref_Key": G(62), "Code": "41.01"}, {"Ref_Key": G(63), "Code": "43"},
                {"Ref_Key": G(64), "Code": "62"}, {"Ref_Key": G(65), "Code": "62.01"}, {"Ref_Key": G(66), "Code": "62.02"},
                {"Ref_Key": G(67), "Code": "60"}, {"Ref_Key": G(68), "Code": "60.01"}, {"Ref_Key": G(69), "Code": "60.02"}],
            "Document_РеализацияТоваровУслуг": [
                {"Ref_Key": G(71), "Number": "0000-000001", "Date": "2026-10-01T10:00:00", "Контрагент_Key": G(11), "СуммаДокумента": 1000,
                 "Товары": [{"LineNumber": "1", "Номенклатура_Key": G(1), "Количество": 10, "Цена": 100, "Сумма": 1000}], "Услуги": []},
                {"Ref_Key": G(72), "Number": "0000-000002", "Date": "2026-10-02T10:00:00", "Контрагент_Key": G(11), "СуммаДокумента": 500,
                 "Товары": [{"LineNumber": "1", "Номенклатура_Key": G(3), "Количество": 1, "Цена": 500, "Сумма": 500}], "Услуги": []},
                {"Ref_Key": G(73), "Number": "0000-000003", "Date": "2026-10-03T10:00:00", "Контрагент_Key": G(12), "СуммаДокумента": 2300,
                 "Товары": [{"LineNumber": "1", "Номенклатура_Key": G(2), "Количество": 20, "Цена": 100, "Сумма": 2000}],
                 "Услуги": [{"LineNumber": "1", "Номенклатура_Key": G(4), "Содержание": "Доставка", "Количество": 1, "Сумма": 300}]}],
        },
        "unf": {
            "Catalog_Номенклатура": [
                {"Ref_Key": G(101), "Code": "1", "Description": "Футболка", "Артикул": "TS", "IsFolder": False, "DeletionMark": False},
                {"Ref_Key": G(102), "Code": "2", "Description": "Кепка", "Артикул": "CAP", "IsFolder": False, "DeletionMark": False}],
            "Catalog_Контрагенты": [
                {"Ref_Key": G(111), "Code": "1", "Description": "ООО Клиент", "ИНН": "7801111111", "ДоговорПоУмолчанию_Key": G(131)},
                {"Ref_Key": G(112), "Code": "2", "Description": "ООО Фабрика", "ИНН": "7802222222"}],
            "Catalog_Организации": [{"Ref_Key": G(121), "Description": "ИП Иванов"}, {"Ref_Key": G(122), "Description": "ООО Иванов и партнеры"}],
            "Catalog_СтруктурныеЕдиницы": [{"Ref_Key": G(151), "Description": "Склад на Ленина"}],
            "Document_РасходнаяНакладная": [
                {"Ref_Key": G(171), "Number": "УТ-1", "Date": "2026-10-05T12:00:00", "Контрагент_Key": G(111), "СуммаДокумента": 3000,
                 "Запасы": [{"LineNumber": "1", "Номенклатура_Key": G(101), "Количество": 3, "Цена": 1000, "Сумма": 3000, "Всего": 3000}]}],
        },
        "empty": {"Catalog_Номенклатура": []},
    }
    balances = {
        ("bp", "41"): [
            {"Account_Key": G(62), "ExtDimension1": G(1), "ExtDimension1_Type": t_bp + "Номенклатура", "ExtDimension2": G(51),
             "ExtDimension2_Type": t_bp + "Склады", "КоличествоBalance": 3, "СуммаBalance": 300},
            {"Account_Key": G(62), "ExtDimension1": G(2), "ExtDimension1_Type": t_bp + "Номенклатура", "ExtDimension2": G(51),
             "ExtDimension2_Type": t_bp + "Склады", "КоличествоBalance": 40, "СуммаBalance": 4000},
            {"Account_Key": G(62), "ExtDimension1": G(3), "ExtDimension1_Type": t_bp + "Номенклатура", "ExtDimension2": G(51),
             "ExtDimension2_Type": t_bp + "Склады", "КоличествоBalance": -2, "СуммаBalance": -1000}],
        ("bp", "62"): [
            {"Account_Key": G(65), "ExtDimension1": G(11), "ExtDimension1_Type": t_bp + "Контрагенты", "ExtDimension2": G(31),
             "ExtDimension2_Type": t_bp + "ДоговорыКонтрагентов", "СуммаBalanceDr": 12000, "СуммаBalanceCr": 0},
            {"Account_Key": G(66), "ExtDimension1": G(12), "ExtDimension1_Type": t_bp + "Контрагенты",
             "СуммаBalanceDr": 0, "СуммаBalanceCr": 5000}],
        ("bp", "60"): [
            {"Account_Key": G(68), "ExtDimension1": G(13), "ExtDimension1_Type": t_bp + "Контрагенты",
             "СуммаBalanceDr": 0, "СуммаBalanceCr": 8000}],
        ("unf", "AccumulationRegister_Запасы"): [
            {"Номенклатура_Key": G(101), "СтруктурнаяЕдиница_Key": G(151), "КоличествоBalance": 2},
            {"Номенклатура_Key": G(102), "СтруктурнаяЕдиница_Key": G(151), "КоличествоBalance": 15}],
        ("unf", "AccumulationRegister_РасчетыСПокупателями"): [{"Контрагент_Key": G(111), "СуммаBalance": 3000}],
        ("unf", "AccumulationRegister_РасчетыСПоставщиками"): [{"Контрагент_Key": G(112), "СуммаBalance": -700}],
    }

    def match_filter(rows, filt):
        if not filt:
            return rows
        out_ = []
        guids = re.findall(r"Ref_Key eq guid'([0-9a-f-]{36})'", filt)
        subs = [s.replace("''", "'").lower() for s in re.findall(r"substringof\('((?:[^']|'')*)', ?(?:Description|НаименованиеПолное)\)", filt)]
        eqs = re.findall(r"(ИНН|Code|Артикул) eq '((?:[^']|'')*)'", filt)
        starts = re.findall(r"startswith\(Code,'([^']*)'\)", filt)
        for r in rows:
            if guids and r.get("Ref_Key") in guids:
                out_.append(r)
            elif any(s in str(r.get("Description", "")).lower() or s in str(r.get("НаименованиеПолное", "")).lower() for s in subs):
                out_.append(r)
            elif any(str(r.get(f)) == v for f, v in eqs):
                out_.append(r)
            elif any(str(r.get("Code", "")).startswith(p) for p in starts):
                out_.append(r)
            elif "Posted eq true" in filt and "Date ge" in filt:
                out_.append(r)
            elif not (guids or subs or eqs or starts) and "DeletionMark" in filt:
                out_.append(r)
        return out_

    class Stub(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def handle_any(self):
            rawpath, _, rawq = self.path.partition("?")
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(n).decode("utf-8") if n else ""
            path = urllib.parse.unquote(rawpath)
            query = {urllib.parse.unquote(k): urllib.parse.unquote(v) for k, _, v in
                     (p.partition("=") for p in rawq.split("&") if p)}
            entry = {"method": self.command, "rawpath": rawpath, "rawq": rawq, "path": path, "query": query,
                     "body": body, "auth": self.headers.get("Authorization", "")}
            state["log"].append(entry)
            if path.startswith("/bot"):
                return self.reply(200, {"ok": path == f"/bot{TG_TOKEN}/deleteMessage"})
            m = re.match(r"^/(bp|unf|empty|html)(/odata/standard\.odata/)?(.*)$", path)
            if not m:
                return self.reply(404, b"<html>not found</html>", ctype="text/html")
            base, has_odata, res = m.group(1), m.group(2), m.group(3)
            if base == "html" or not has_odata:
                return self.reply(404, b"<html><body>404 Not Found</body></html>", ctype="text/html")
            want = "Basic " + base64.b64encode(f"{USERS[base]}:{PW[base]}".encode("utf-8")).decode()
            if entry["auth"] != want:
                return self.reply(401, b"", ctype="text/plain")
            entity = res.split("/")[0].split("(")[0]
            for key in (res, entity):
                queue = state["force"].get((base, key)) or []
                if queue:
                    item = queue.pop(0)
                    if item[0] == "sleep":
                        time.sleep(item[1])
                        return self.reply(200, {"value": []})
                    return self.reply(item[0], item[1], ctype=item[2] if len(item) > 2 else "application/json")
            if res == "$metadata":
                return self.reply(200, {"bp": bp_meta, "unf": unf_meta, "empty": empty_meta}[base], ctype="application/xml")
            if self.command == "POST":
                state["posts"].append((base, res, body))
                if "/Post" in res:
                    return self.reply(200, b"")
                obj = dict(json.loads(body), Ref_Key=G(999), Number="0000-000123", Date="2026-10-18T12:00:00", Posted=False)
                return self.reply(201, obj)
            filt = query.get("$filter", "")
            if "/Balance(" in res:
                if base == "bp":
                    for code in ("41", "62", "60"):
                        if f"'{code}." in filt or f"'{code}'" in filt:
                            return self.reply(200, {"value": balances[("bp", code)]})
                    return self.reply(200, {"value": []})
                return self.reply(200, {"value": balances.get((base, entity), [])})
            rows = data.get(base, {}).get(entity)
            if rows is None:
                return self.reply(404, {"odata.error": {"code": "8", "message": {"lang": "ru", "value": f"Сущность '{entity}' не найдена"}}})
            rows = match_filter(rows, filt)
            skip, top = int(query.get("$skip") or 0), int(query.get("$top") or 1000)
            return self.reply(200, {"odata.metadata": "x", "value": rows[skip:skip + top]})

        def reply(self, code, payload, ctype="application/json"):
            raw = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode("utf-8")
            try:
                self.send_response(code)
                if code == 401:
                    self.send_header("WWW-Authenticate", 'Basic realm="1C"')
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                pass   # the client gave up (the timeout check)

        do_GET = do_POST = do_PATCH = do_DELETE = handle_any

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Stub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    host = f"127.0.0.1:{srv.server_address[1]}"
    tmp = tempfile.mkdtemp(prefix="onec-selftest-")
    store = os.path.join(tmp, "home", ".oinoa")
    tg = os.path.join(tmp, "tg")
    os.makedirs(os.path.join(tg, "msglog"))
    with open(os.path.join(tg, ".env"), "w") as f:
        f.write(f"TELEGRAM_BOT_TOKEN={TG_TOKEN}\n")
    log_file = os.path.join(tg, "msglog", "2026-10-18.jsonl")
    with open(log_file, "w", encoding="utf-8") as f:
        f.write(json.dumps({"dir": "in", "message_id": 42, "text": f"Адрес: http://{host}/bp/ru_RU/\nЛогин: Помощник\nПароль: {PW['bp']}"},
                           ensure_ascii=False) + "\n")
        f.write(json.dumps({"dir": "in", "message_id": 43, "text": "сколько продали за месяц?"}, ensure_ascii=False) + "\n")
    env = {"PATH": os.environ.get("PATH", ""), "HOME": os.path.join(tmp, "home"), "OINOA_1C_DIR": store,
           "TELEGRAM_STATE_DIR": tg, "OINOA_TG_API": f"http://{host}", "LANG": "C.UTF-8", "PYTHONIOENCODING": "utf-8",
           "OINOA_1C_TIMEOUT": "5"}
    results, seen = [], []

    def run(args, stdin="", extra=None):
        e = dict(env, **(extra or {}))
        p = subprocess.run([sys.executable, "-I", os.path.abspath(__file__)] + args, input=stdin, env=e,
                           capture_output=True, text=True, timeout=120)
        seen.append(p.stdout + p.stderr)
        if os.environ.get("OINOA_1C_SELFTEST_SHOW"):
            print(f"$ onec.py {' '.join(args)}  (exit {p.returncode})\n{p.stdout}{p.stderr}")
        return p.returncode, p.stdout, p.stderr

    def check(cond, label_, detail=""):
        results.append((bool(cond), label_, detail))

    def calls(pred):
        return [x for x in state["log"] if pred(x)]

    envp = os.path.join(store, ENV_NAME)
    metap = os.path.join(store, META_NAME)

    # the address as the owner pastes it: the browser's /ru_RU, an /odata tail, user:password@, no scheme
    check(normalize_url("https://1cfresh.com/a/ea/1234567/ru_RU/")[0] == "https://1cfresh.com/a/ea/1234567",
          "1С:Фреш address without the language code")
    check(normalize_url("https://srv.example.ru/bp/odata/standard.odata/Catalog_X?$top=1")[0] == "https://srv.example.ru/bp",
          "an /odata/... tail is cut")
    check(normalize_url("https://odata:s3cret@srv.example.ru:8443/УТ/ru/")[:3] ==
          ("https://srv.example.ru:8443/УТ", "odata", "s3cret"), "user:password@ in the address is taken out of it")
    check(normalize_url("1cfresh.com/a/sbm/777")[0] == "https://1cfresh.com/a/sbm/777", "no scheme: https assumed")
    check(parse_creds("логин: odata пароль: a.b/c d")[1:] == ("odata", "a.b/c d")
          and parse_creds("https://x.ru/b\nodata / Qwe123")[1:] == ("odata", "Qwe123"), "credential forms")
    check(parse_line("Кружка: белая:2:350 руб.") == ("Кружка: белая", 2.0, 350.0), "a line with a colon in the name and руб.")
    check(ut_ok, "УТ 11: detected, Товары на складах, ВНаличииBalance")
    check(ut2_ok, "Balance fields found by name without the binding")

    # connect БП: labeled lines, a language tail on the address, the password with a space, # and Cyrillic
    n0 = len(state["log"])
    rc, so, se = run(["connect", "--chat", "7", "--msg", "42"],
                     f"Вот доступ к 1С:\nАдрес: http://{host}/bp/ru_RU/\nЛогин: Помощник\nПароль: {PW['bp']}\n")
    check(rc == 0 and "OK connected 1С" in so and "Бухгалтерия предприятия 3.0" in so, "connect bp: detected БП 3.0", so + se)
    check(f"http://{host}/bp as «Помощник»" in so, "address normalized (no /ru_RU)", so)
    check("WARNING the address is http" in so, "http warns", so)
    meta_call = calls(lambda x: x["path"].endswith("$metadata"))
    want_auth = "Basic " + base64.b64encode(f"Помощник:{PW['bp']}".encode("utf-8")).decode()
    check(meta_call and meta_call[-1]["auth"] == want_auth, "Basic auth is UTF-8 user:password")
    check(os.path.exists(envp) and (os.stat(envp).st_mode & 0o777) == 0o600, "env file is 0600")
    check((os.stat(store).st_mode & 0o777) == 0o700, "store folder is 0700")
    check(PW["bp"] in open(envp, encoding="utf-8").read(), "password stored exactly (space, #, Cyrillic)")
    check(os.path.exists(metap) and (os.stat(metap).st_mode & 0o777) == 0o600, "metadata cache is 0600")
    dels = calls(lambda x: x["path"] == f"/bot{TG_TOKEN}/deleteMessage")
    check(dels and "message_id=42" in dels[-1]["body"], "telegram message deleted")
    check("message with the password is deleted" in so, "connect says the message is deleted", so)
    logtext = open(log_file, encoding="utf-8").read()
    check(PW["bp"] not in logtext and "сколько продали" in logtext, "msglog scrubbed, other rows kept")
    check("Found:" in so and "Document_СчетНаОплатуПокупателю" in so and "AccountingRegister_Хозрасчетный" in so,
          "coverage lists found objects", so)

    # a wrong password: 401, nothing replaced; missing password; argv never echoed
    rc, so, se = run(["connect"], f"http://{host}/bp\nПомощник\nwrong-password-1\n")
    check(rc == 2 and "ERROR auth" in so and "Nothing is stored" in so, "wrong password: 401 explained", so + se)
    check(PW["bp"] in open(envp, encoding="utf-8").read(), "wrong password not stored, old kept")
    rc, so, se = run(["connect"], f"адрес http://{host}/bp\nлогин Помощник\n")
    check(rc == 2 and "ERROR no_password" in so, "no password: asks for it", so)
    rc, so, se = run(["connect", "--password-typed-here-xyz"], "")
    check(rc == 2 and "password-typed-here-xyz" not in so + se, "unknown argv is not echoed", so + se)

    rc, so, se = run(["status"])
    check(rc == 0 and "Configuration: 1С:Бухгалтерия предприятия 3.0" in so and "Reachable: yes" in so, "status", so + se)

    # find: goods by part of the name, a counterparty by ИНН; Cyrillic names percent-encoded, no '+'
    rc, so, se = run(["find", "кружка"])
    check(rc == 0 and "Goods: 2 found" in so and "Кружка белая | code 00-001, article KR-1" in so, "find goods", so + se)
    g = calls(lambda x: x["path"].endswith("/Catalog_Номенклатура") and "substringof" in x["query"].get("$filter", ""))
    check(g and "/Catalog_%D0%9D%D0%BE%D0%BC" in g[-1]["rawpath"] and "Номенклатура" not in g[-1]["rawpath"],
          "Cyrillic entity name is percent-encoded in the path", g[-1]["rawpath"] if g else "")
    check(g and "+" not in g[-1]["rawq"] and "%20" in g[-1]["rawq"], "spaces in the query are %20, never +",
          g[-1]["rawq"] if g else "")
    check(g and "allowedOnly=true" in g[-1]["rawq"] and "IsFolder eq false" in g[-1]["query"]["$filter"],
          "allowedOnly and IsFolder sent")
    rc, so, se = run(["find", "7701234567"])
    check(rc == 0 and "ООО Ромашка | ИНН 7701234567, КПП 770101001" in so, "find counterparty by ИНН", so)

    # stock from accounting (41, 43): names, a warehouse, LOW and BELOW ZERO
    rc, so, se = run(["stock", "--low", "5"])
    check(rc == 0 and "Stock (accounting, accounts 41, 43): 3 items, 2 at or below 5, 1 below zero" in so
          and "Чайник (CH-1) | -2 | Основной склад -2 | BELOW ZERO" in so and "Кружка белая (KR-1) | 3" in so
          and "| LOW" in so, "stock bp", so + se)
    b = calls(lambda x: "/AccountingRegister_Хозрасчетный/Balance(Period=datetime'" in x["path"])
    check(b and "Account/Code eq '41.01'" in b[-1]["query"].get("$filter", "") and "КоличествоBalance" in b[-1]["query"].get("$select", ""),
          "stock: Balance with Account/Code filter and $select", b[-1]["query"] if b else "")
    check(b and "%D0%A5" in b[-1]["rawpath"] and "(Period=datetime'" in b[-1]["rawpath"], "virtual table path encoded, syntax kept",
          b[-1]["rawpath"] if b else "")
    rc, so, se = run(["stock", "--item", "синяя"])
    check(rc == 0 and "1 item," in so and "Кружка синяя (KR-2) | 40" in so and "Чайник" not in so, "stock --item", so)

    # sales for a period: count, sum, top customers and items
    rc, so, se = run(["sales", "--from", "2026-10-01", "--to", "31.10.2026"])
    check(rc == 0 and "Sales, 01.10.2026 to 31.10.2026" in so and "3 documents, 3 800 ₽" in so
          and "ИП Петров 2 300 ₽ (1)" in so and "ООО Ромашка 1 500 ₽ (2)" in so
          and "Кружка синяя 2 000 ₽ / 20 pcs" in so and "Approximate" in so, "sales bp", so + se)
    s = calls(lambda x: x["path"].endswith("/Document_РеализацияТоваровУслуг"))
    f = s[-1]["query"].get("$filter", "") if s else ""
    check("Posted eq true" in f and "Date ge datetime'2026-10-01T00:00:00'" in f and "Date le datetime'2026-10-31T23:59:59'" in f
          and "Товары" in s[-1]["query"].get("$select", ""), "sales filter and select", f)
    rc, so, se = run(["sales", "--days", "30", "--json"])
    try:
        check(rc == 0 and isinstance(json.loads(so), list), "--json is valid JSON")
    except ValueError:
        check(False, "--json is valid JSON", so)

    # debts from accounting: 62 Dr, 62.02 Cr (their prepayment), 60 Cr
    rc, so, se = run(["debts"])
    check(rc == 0 and "Owed to us: 1 counterparty, 12 000 ₽" in so and "ООО Ромашка | 12 000 ₽" in so
          and "We owe: 2 counterparties, 13 000 ₽" in so and "ООО Поставщик | 8 000 ₽" in so
          and "ИП Петров | 5 000 ₽ | their prepayment" in so and "Approximate" in so, "debts bp", so + se)

    # invoice: a preview creates nothing; --yes creates a draft, never posts
    before = len(state["posts"])
    rc, so, se = run(["invoice", "Ромашка", "Кружка белая:10:350", "Чайник:1:1 200,50"])
    check(rc == 0 and so.startswith("PREVIEW «Счет покупателю»") and "2 lines, 4 700,50" not in so
          and "4 700.50 ₽" in so and "Nothing created" in so and "VAT is not filled" in so, "invoice preview", so + se)
    check(len(state["posts"]) == before, "preview sends nothing")
    rc, so, se = run(["invoice", "7701234567", "Кружка белая:10:350", "Чайник:1:1 200,50", "--yes"])
    check(rc == 0 and "OK created «Счет покупателю» (Document_СчетНаОплатуПокупателю) № 0000-000123" in so
          and "NOT posted" in so, "invoice --yes", so + se)
    posts = state["posts"][before:]
    body = json.loads(posts[-1][2]) if posts else {}
    rows = body.get("Товары") or [{}]
    check(len(posts) == 1 and posts[-1][1] == "Document_СчетНаОплатуПокупателю", "exactly one POST, to the invoice")
    check(body.get("Контрагент_Key") == G(11) and body.get("Организация_Key") == G(21)
          and body.get("ДоговорКонтрагента_Key") == G(31) and body.get("СтруктурнаяЕдиница_Key") == G(41)
          and body.get("СуммаДокумента") == 4700.5 and "Posted" not in body, "invoice header", json.dumps(body, ensure_ascii=False))
    check(rows[0] == {"LineNumber": "1", "Номенклатура": G(1), "Номенклатура_Type": "StandardODATA.Catalog_Номенклатура",
                      "Количество": 10.0, "Цена": 350.0, "Сумма": 3500.0} and rows[1].get("LineNumber") == "2",
          "invoice rows: composite item field, lines from 1", json.dumps(rows, ensure_ascii=False))
    check(not calls(lambda x: "/Post" in x["path"] or "/Unpost" in x["path"]), "nothing is ever posted")
    rc, so, se = run(["invoice", "Ромашка", "Кружка:1:100", "--yes"])
    check(rc == 2 and "ERROR ambiguous" in so and "Кружка белая" in so and "Кружка синяя" in so
          and len(state["posts"]) == before + 1, "ambiguous item stops the invoice", so)
    rc, so, se = run(["invoice", "Ромашка", "Кружка белая-10-350"])
    check(rc == 2 and "ERROR bad_line" in so, "a bad line is explained", so)

    # errors: an object not in the publication (404, code 8); 400 code 6; network down; timeout
    state["force"][("bp", "Document_РеализацияТоваровУслуг")] = [
        (404, {"odata.error": {"code": "8", "message": {"lang": "ru", "value": "Сущность не найдена"}}})]
    rc, so, se = run(["sales"])
    check(rc == 2 and "ERROR not_published" in so and "«Реализация товаров и услуг»" in so and "«Состав»" in so,
          "404 code 8: says what the administrator adds", so)
    state["force"][("bp", "Catalog_Номенклатура")] = [
        (400, {"odata.error": {"code": "6", "message": {"lang": "ru", "value": "Сегмент пути Артикул не найден!"}}})] * 2
    rc, so, se = run(["find", "кружка"])
    check(rc == 2 and "ERROR no_such_field" in so and "Counterparties" in so, "400 code 6 explained, the other part still answers", so)
    state["force"].pop(("bp", "Catalog_Номенклатура"), None)
    state["force"][("bp", "Catalog_Номенклатура")] = [("sleep", 2.5), ("sleep", 2.5)]
    n1 = len(state["log"])
    rc, so, se = run(["find", "кружка"], extra={"OINOA_1C_TIMEOUT": "1"})
    tries = calls(lambda x: x["path"].endswith("/Catalog_Номенклатура"))
    check(rc == 2 and "ERROR timeout" in so and len([x for x in state["log"][n1:] if x["path"].endswith("/Catalog_Номенклатура")]) == 2,
          "a read that times out is asked twice, then explained", so)
    state["force"].pop(("bp", "Catalog_Номенклатура"), None)
    time.sleep(3)

    # connect УНФ: plain lines, no labels; it replaces БП
    rc, so, se = run(["connect", "--chat", "7", "--msg", "44"], f"http://{host}/unf\nadmin\n{PW['unf']}\n")
    check(rc == 0 and "Управление нашей фирмой" in so and f"It replaces the connection to http://{host}/bp" in so,
          "connect unf: detected, replaces bp", so + se)
    rc, so, se = run(["stock"])
    check(rc == 0 and "Stock («Запасы» (AccumulationRegister_Запасы))" in so and "Футболка (TS) | 2 | Склад на Ленина 2 | LOW" in so,
          "stock unf: falls back to the second register", so + se)
    b = calls(lambda x: "/AccumulationRegister_Запасы/Balance(" in x["path"])
    check(b and "Dimensions='Номенклатура,СтруктурнаяЕдиница'" in b[-1]["path"] and "%D0%97" in b[-1]["rawpath"],
          "accumulation Balance with Dimensions, encoded", b[-1]["path"] if b else "")
    rc, so, se = run(["sales", "--days", "400"])
    check(rc == 2 and "ERROR bad_period" in so, "a period over a year is refused", so)
    rc, so, se = run(["sales", "--from", "01.10.2026", "--to", "2026-10-31"])
    check(rc == 0 and "1 document, 3 000 ₽" in so and "Футболка 3 000 ₽ / 3 pcs" in so, "sales unf", so + se)
    rc, so, se = run(["debts"])
    check(rc == 0 and "ООО Клиент | 3 000 ₽" in so and "ООО Фабрика | 700 ₽ | our prepayment" in so, "debts unf", so + se)
    rc, so, se = run(["invoice", "ООО Клиент", "Футболка:2:1500"])
    check(rc == 2 and "ERROR which_org" in so and "ИП Иванов" in so, "several organizations: asks which", so)
    before = len(state["posts"])
    rc, so, se = run(["invoice", "ООО Клиент", "Футболка:2:1500", "Кепка:1:500", "--org", "ИП Иванов", "--yes"])
    body = json.loads(state["posts"][-1][2]) if len(state["posts"]) > before else {}
    check(rc == 0 and "OK created «Счет на оплату» (Document_СчетНаОплату)" in so
          and body.get("Запасы", [{}])[0] == {"LineNumber": "1", "Номенклатура_Key": G(101), "Количество": 2.0, "Цена": 1500.0, "Сумма": 3000.0}
          and body.get("Договор_Key") == G(131) and body.get("Организация_Key") == G(121) and "Posted" not in body,
          "invoice unf: _Key item field, contract by default", so + json.dumps(body, ensure_ascii=False))

    # a base with none of the known objects: says what is missing and what there is
    rc, so, se = run(["connect"], f"Адрес: http://{host}/empty\nЛогин: u\nПароль: {PW['empty']}\n")
    check(rc == 0 and "not recognised" in so and "Missing:" in so, "unknown configuration connects, lists what is missing", so)
    rc, so, se = run(["invoice", "x", "y:1:1"])
    check(rc == 2 and "ERROR not_in_publication" in so and "Document_СчетФактураПолученный" in so,
          "missing object: names what the base has nearby", so)
    rc, so, se = run(["connect"], f"http://{host}/html/base\nu\np12345678\n")
    check(rc == 2 and "ERROR odata_off" in so, "no OData at the address: explained", so)
    dead = socket.socket()
    dead.bind(("127.0.0.1", 0))
    port = dead.getsockname()[1]
    dead.close()
    rc, so, se = run(["connect"], f"http://127.0.0.1:{port}/base\nu\np12345678\n")
    check(rc == 2 and "ERROR network" in so and "office network" in so, "unreachable local address explained", so)

    rc, so, se = run(["forget"])
    check(rc == 0 and "OK forgot 1С" in so and not os.path.exists(envp) and not os.path.exists(metap), "forget removes everything", so)
    rc, so, se = run(["stock"])
    check(rc == 2 and "ERROR not_connected" in so, "not connected after forget", so)

    b64s = [base64.b64encode(f"{USERS[k]}:{PW[k]}".encode("utf-8")).decode() for k in PW]
    secrets = list(PW.values()) + b64s + [TG_TOKEN]
    leaks = [x for x in secrets if any(x in text for text in seen)]
    check(not leaks, "no password, auth header or bot token in any stdout or stderr", f"{len(leaks)} leaked")
    srv.shutdown()
    bad = [r for r in results if not r[0]]
    for ok, label_, detail in results:
        print(f"{'ok  ' if ok else 'FAIL'} {label_}" + ("" if ok else f"\n     {clip(detail, 1500)}"))
    print(f"SELFTEST {'OK' if not bad else 'FAILED'}: {len(results) - len(bad)}/{len(results)} checks passed")
    shutil.rmtree(tmp, ignore_errors=True)
    sys.exit(1 if bad else 0)


# ---- command line -------------------------------------------------------------------------------

class Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse quotes the bad value; a password typed in the wrong place must not be printed back.
        fail("bad_args", f"the command line is not right ({re.sub(chr(39) + '[^' + chr(39) + ']*' + chr(39), '...', message)}). "
             "The password goes on stdin in the <<'EOF' block, never as an argument.")


def main():
    if "--selftest" in sys.argv[1:]:
        selftest()
    p = Parser(prog="onec.py", description="The owner's own 1С:Предприятие over OData.")
    sub = p.add_subparsers(dest="cmd", required=True, parser_class=Parser)
    c = sub.add_parser("connect")
    c.add_argument("--chat", help="chat_id of the message with the password")
    c.add_argument("--msg", action="append", help="message_id(s) of the message(s) with the password")
    c.add_argument("--url", help="the address, if it came separately (not secret)")
    c.add_argument("--user", help="the user name, if it came separately (not secret)")
    st = sub.add_parser("status")
    st.add_argument("--refresh", action="store_true", help="read the description of the objects again")
    sub.add_parser("forget")
    f = sub.add_parser("find")
    f.add_argument("text", nargs="+")
    f.add_argument("--limit", type=int, default=10)
    f.add_argument("--json", action="store_true")
    s = sub.add_parser("stock")
    s.add_argument("--item")
    s.add_argument("--low", type=float, default=5)
    s.add_argument("--all", action="store_true")
    s.add_argument("--json", action="store_true")
    sa = sub.add_parser("sales")
    sa.add_argument("--days", type=int, default=30)
    sa.add_argument("--from", dest="date_from")
    sa.add_argument("--to", dest="date_to")
    sa.add_argument("--top", type=int, default=5)
    sa.add_argument("--json", action="store_true")
    d = sub.add_parser("debts")
    d.add_argument("--top", type=int, default=10)
    d.add_argument("--json", action="store_true")
    i = sub.add_parser("invoice")
    i.add_argument("counterparty")
    i.add_argument("lines", nargs="+", metavar="ITEM:QTY:PRICE")
    i.add_argument("--org")
    i.add_argument("--yes", action="store_true", help="create it; without this only a preview")
    i.add_argument("--json", action="store_true")
    a, extra = p.parse_known_args()
    if extra:
        # Never echo unknown arguments: a password typed on the command line must not be printed back.
        fail("bad_args", "unexpected words on the command line (not shown). The password goes on stdin in the "
             "<<'EOF' block, never as an argument; if one was typed there, ask them to change it in 1С.")
    if getattr(a, "limit", 1) < 1 or getattr(a, "top", 1) < 1:
        fail("bad_args", "--limit and --top are 1 or more.")
    {"connect": cmd_connect, "status": cmd_status, "forget": cmd_forget, "find": cmd_find, "stock": cmd_stock,
     "sales": cmd_sales, "debts": cmd_debts, "invoice": cmd_invoice}[a.cmd](a)


if __name__ == "__main__":
    main()
