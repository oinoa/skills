#!/usr/bin/env python3
"""OINOA yookassa: the owner's own ЮKassa (YooKassa) shop through ЮKassa API v3.

  connect [--shop-id N] [--chat ID --msg ID[,ID]]   shopId and secret key on stdin
  status | forget
  payments [--days N] [--status S] [--limit N]      payments since N days ago (1 = today), with totals
  payment ID                                        one payment in full: why it was canceled, its refunds
  link AMOUNT DESCRIPTION... [--email E | --phone P] [--vat N] [--days N] [--return-url URL] [--yes]
  refund PAYMENT_ID [AMOUNT] [--reason TEXT] [--email E | --phone P] [--vat N] [--yes]
  capture PAYMENT_ID [AMOUNT] [--yes]               a two-stage payment: take the frozen money
  cancel PAYMENT_ID [--yes]                         a two-stage payment: give the frozen money back
  --json prints the raw API answer.   --selftest runs offline against a local stub server.

Python 3 stdlib only. Every answer is a few short lines. A line that starts with ERROR names
what went wrong and what to tell the person. The secret key never appears in the output, in
argv or in the chat log: it comes in on stdin and lives in one 0600 file. Links, refunds,
captures and cancels only preview without --yes.

API (checked against the official reference and its OpenAPI spec, 2026-10-18):
  https://yookassa.ru/developers/api  https://yookassa.ru/developers/api/yookassa-openapi-specification.yaml
  host api.yookassa.ru/v3, HTTP Basic shopId:secret key, Idempotence-Key (<= 64 chars) on every POST,
  kept by ЮKassa for 24 h. Lists: limit <= 100, next_cursor. Errors: {type, id, code, description,
  parameter, retry_after (ms)}; 500 means the result is unknown.
"""
import argparse
import base64
import hashlib
import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

HOME = os.path.expanduser("~")
# ~/.oinoa first; ~/.config/oinoa-yookassa when ~/.oinoa is not writable.
STORES = [os.environ["OINOA_YK_DIR"]] if os.environ.get("OINOA_YK_DIR") else [
    os.path.join(HOME, ".oinoa"), os.path.join(HOME, ".config", "oinoa-yookassa")]
ENV_NAME = "yookassa.env"
PENDING_NAME = "yookassa-pending.json"
TG_DIR = os.environ.get("TELEGRAM_STATE_DIR") or os.path.join(HOME, ".claude", "channels", "telegram")
TG_API = os.environ.get("OINOA_TG_API", "https://api.telegram.org")
API = os.environ.get("OINOA_YOOKASSA_API", "https://api.yookassa.ru/v3").rstrip("/")
TIMEOUT = 30          # seconds, every request
RETRY_CAP = 30        # a 429 that asks for a longer wait is reported, not waited out
WAIT = float(os.environ.get("OINOA_YK_WAIT", "1"))  # scales every pause (tests make it tiny)
MAX_PAGES = 30        # 30 x 100 payments per listing
KEY_TTL = 23 * 3600   # ЮKassa keeps an Idempotence-Key 24 h; reuse ours a little less

CABINET = "https://yookassa.ru/my"
KEYS_PAGE = "https://yookassa.ru/my/merchant/integration/api-keys"
SHOP_PAGE = "https://yookassa.ru/my/shop-settings"
DEFAULT_RETURN = "https://yookassa.ru/"

UTC = timezone.utc
MSK = timezone(timedelta(hours=3), "MSK")

STATUS_EN = {"succeeded": "paid", "pending": "waiting for the buyer",
             "waiting_for_capture": "paid, waiting for your capture", "canceled": "canceled"}
METHOD = {"bank_card": "card", "sbp": "СБП", "sberbank": "SberPay", "tinkoff_bank": "T-Pay", "yoo_money": "ЮMoney",
          "alfa_pay": "Alfa Pay", "sber_loan": "Покупки в кредит", "sber_bnpl": "Плати частями",
          "mobile_balance": "баланс телефона", "cash": "наличные", "b2b_sberbank": "СберБизнес",
          "electronic_certificate": "электронный сертификат", "installments": "рассрочка", "apple_pay": "Apple Pay",
          "google_pay": "Google Pay", "qiwi": "QIWI", "webmoney": "WebMoney", "wechat": "WeChat Pay",
          "alfabank": "Альфа-Клик"}
# Чеки от ЮKassa: receipts go only by email. Other providers are third-party online registers.
YK_RECEIPTS = {"avanpost", "yoo_receipt"}
VAT = {1: "без НДС", 2: "НДС 0%", 3: "НДС 10%", 4: "НДС 20%", 5: "НДС 10/110", 6: "НДС 20/120", 7: "НДС 5%",
       8: "НДС 7%", 9: "НДС 5/105", 10: "НДС 7/107", 11: "НДС 22%", 12: "НДС 22/122"}
SUBJECTS = ["commodity", "excise", "job", "service", "gambling_bet", "gambling_prize", "lottery", "lottery_prize",
            "intellectual_activity", "payment", "agent_commission", "property_right", "non_operating_gain",
            "insurance_premium", "sales_tax", "resort_fee", "composite", "another", "fine", "tax", "lien", "cost",
            "pension_insurance_without_payouts", "pension_insurance_with_payouts", "health_insurance_without_payouts",
            "health_insurance_with_payouts", "health_insurance", "casino", "agent_withdrawals", "non_marked_excise",
            "marked_excise", "marked", "non_marked"]
MODES = ["full_prepayment", "partial_prepayment", "advance", "full_payment", "partial_payment", "credit", "credit_payment"]

# Who canceled and why, from https://yookassa.ru/developers/payment-acceptance/after-the-payment/declined-payments
PARTY = {"merchant": "вы (магазин)", "yoo_money": "ЮKassa",
         "payment_network": "банк покупателя или другой участник платежа"}
REASON = {
    "3d_secure_failed": ("не прошёл 3-D Secure",
                         "Покупатель не подтвердил оплату кодом банка (3-D Secure). Пусть попробует ещё раз по новой "
                         "ссылке, другой картой или уточнит в своём банке."),
    "call_issuer": ("банк отказал без объяснения",
                    "Банк или платёжный сервис покупателя отклонил оплату и не назвал причину. Покупателю стоит "
                    "позвонить в свой банк или заплатить другим способом."),
    "canceled_by_merchant": ("отменён магазином",
                             "Платёж отменил магазин через API, пока он ждал списания. Деньги вернулись покупателю."),
    "card_expired": ("истёк срок карты",
                     "У карты покупателя истёк срок действия. Нужна другая карта или другой способ оплаты."),
    "country_forbidden": ("карта этой страны не принимается",
                          "Магазин не принимает карты, выпущенные в этой стране. Покупателю нужен другой способ "
                          "оплаты. Ограничения по иностранным картам настраиваются в кабинете ЮKassa."),
    "deal_expired": ("истёк срок сделки",
                     "Безопасная сделка: срок сделки закончился. Нужна новая сделка и новый платёж."),
    "expired_on_capture": ("не списали вовремя",
                           "Платёж в две стадии: деньги были заморожены, но их не списали вовремя, и ЮKassa вернула "
                           "их покупателю. Чтобы получить оплату, нужен новый платёж."),
    "expired_on_confirmation": ("покупатель не оплатил вовремя",
                                "Покупатель не завершил оплату за отведённое время (у ссылки на оплату это около "
                                "часа). Если он ещё хочет заплатить, нужна новая ссылка."),
    "fraud_suspected": ("подозрение на мошенничество",
                        "Платёж заблокирован из-за подозрения в мошенничестве. Покупателю нужен другой способ оплаты."),
    "general_decline": ("отказ без подробностей",
                        "Причина не указана. Подробности знает тот, кто отменил платёж (см. «Кто отменил»): "
                        "покупателю стоит спросить свой банк."),
    "identification_required": ("лимит кошелька ЮMoney",
                                "Превышен лимит кошелька ЮMoney. Покупателю нужно идентифицировать кошелёк или "
                                "заплатить другим способом."),
    "insufficient_funds": ("не хватило денег",
                           "На карте или счёте покупателя не хватило денег. Пусть пополнит баланс или заплатит "
                           "другой картой."),
    "internal_timeout": ("сбой на стороне ЮKassa",
                         "У ЮKassa был технический сбой: платёж не обработали за 30 секунд. Нужна новая ссылка, "
                         "покупатель может заплатить снова."),
    "invalid_card_number": ("неверный номер карты",
                            "Номер карты введён с ошибкой. Покупателю нужно оплатить заново и ввести номер внимательно."),
    "invalid_csc": ("неверный код CVC",
                    "Неверно введён код CVV2/CVC2 с обратной стороны карты. Покупателю нужно оплатить заново."),
    "issuer_unavailable": ("банк покупателя недоступен",
                           "Банк или платёжный сервис покупателя не ответил. Пусть попробует позже или другим способом."),
    "loan_application_expired": ("не успел заполнить заявку на кредит",
                                 "Покупатель не успел заполнить заявку на кредит или рассрочку. Если он ещё хочет "
                                 "купить, нужен новый платёж."),
    "loan_declined": ("банк отказал в кредите",
                      "Банк отклонил заявку на кредит или рассрочку. Покупателю нужен другой способ оплаты."),
    "loan_declined_by_payer": ("покупатель отказался от кредита",
                               "Покупатель сам отказался от кредита или рассрочки в период охлаждения. Если он ещё "
                               "хочет купить, нужен новый платёж."),
    "payment_method_limit_exceeded": ("исчерпан лимит",
                                      "Исчерпан лимит платежей для этого способа оплаты или для вашего магазина. "
                                      "Покупатель может заплатить другим способом или на следующий день."),
    "payment_method_restricted": ("карта или кошелёк заблокированы",
                                  "Операции этим способом оплаты запрещены (например, карта заблокирована). "
                                  "Покупателю нужно обратиться в свой банк или заплатить иначе."),
    "permission_revoked": ("отозвано разрешение на автоплатежи",
                           "Покупатель отозвал разрешение на автоплатежи, списать без его подтверждения нельзя. "
                           "Нужен новый платёж, который он подтвердит сам."),
    "unsupported_mobile_operator": ("оператор связи не поддерживается",
                                    "С номера этого мобильного оператора платить нельзя. Покупателю нужен другой "
                                    "способ оплаты."),
}
# Why a refund was canceled, from https://yookassa.ru/developers/payment-acceptance/after-the-payment/refunds
REFUND_REASON = {
    "general_decline": "причина не указана, уточните в поддержке ЮKassa",
    "insufficient_funds": ("не хватило денег для возврата: за день пришло меньше, чем сумма возврата, или есть "
                           "задолженность перед ЮKassa"),
    "rejected_by_payee": ("банк покупателя отклонил возврат; через ЮKassa его не сделать, верните деньги напрямую "
                          "или пусть покупатель уточнит в банке"),
    "rejected_by_timeout": "технический сбой у банка покупателя, возврат можно повторить позже",
    "yoo_money_account_closed": "покупатель закрыл кошелёк ЮMoney, деньги нужно вернуть напрямую",
    "payment_expired": "платёж старше 15 месяцев, банк не принимает возврат, верните деньги напрямую",
    "payment_article_number_not_found": "электронный сертификат: таких товаров не было в корзине",
    "payment_basket_id_not_found": "электронный сертификат: корзина покупки не найдена",
    "payment_tru_code_not_found": "электронный сертификат: таких товаров не было в корзине",
    "some_articles_already_refunded": "часть товаров уже возвращена",
    "too_many_refunding_articles": "возвращаемых товаров больше, чем было куплено",
}


def out(*lines):
    print("\n".join(str(x) for x in lines if x is not None))


def fail(code, text, extra=()):
    out(f"ERROR {code}: {text}", *extra)
    sys.exit(2)


# ---- the stored key -------------------------------------------------------

def store_path(name=ENV_NAME):
    for d in STORES:
        path = os.path.join(d, name)
        if os.path.exists(path):
            return path
    return os.path.join(STORES[0], name)


def load():
    values = {}
    try:
        with open(store_path()) as f:
            for line in f:
                key, sep, value = line.rstrip("\n").partition("=")
                if sep and key.strip() and not key.startswith("#"):
                    values[key.strip()] = value
    except FileNotFoundError:
        pass
    return values


def write_private(name, text):
    """Write one 0600 file in a 0700 folder (the first place that works); drop a copy left in the other."""
    for store in STORES:
        path = os.path.join(store, name)
        try:
            os.makedirs(store, mode=0o700, exist_ok=True)
            os.chmod(store, 0o700)
            tmp = path + ".tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(text)
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except OSError:
            continue
        for other in STORES:
            stale = os.path.join(other, name)
            if other != store and os.path.exists(stale):
                try:
                    os.remove(stale)
                except OSError:
                    pass
        return True
    return False


def save(values):
    clean = {k: re.sub(r"[\r\n]", " ", str(v)).strip() for k, v in values.items() if v not in (None, "")}
    body = "# OINOA yookassa. Secret: never print, copy or send this file.\n" + "".join(
        f"{k}={clean[k]}\n" for k in sorted(clean))
    if not write_private(ENV_NAME, body):
        fail("cannot_save", "the key works, but it could not be kept on this agent. Nothing is stored; "
             "tell them it will be fixed and to try again later.")


def need_cfg():
    cfg = load()
    if not (cfg.get("YK_SHOP_ID") and cfg.get("YK_SECRET_KEY")):
        fail("not_connected", "no ЮKassa shop is connected. Offer to connect it (yookassa skill, Connect), "
             "or ask them to paste what they see in the ЮKassa cabinet.")
    return cfg


# ---- request keys that survive an unknown result ------------------------------
# A POST that got no clear answer (HTTP 500, a timeout) may still have gone through. Its
# Idempotence-Key stays on disk, so running the same command again sends the same key and
# ЮKassa returns the first result instead of doing it twice. A clear answer drops the key.

def load_pending():
    try:
        with open(store_path(PENDING_NAME)) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_pending(data):
    if data:
        write_private(PENDING_NAME, json.dumps(data))
        return
    for d in STORES:
        try:
            os.remove(os.path.join(d, PENDING_NAME))
        except OSError:
            pass


def fingerprint(cfg, path, body, extra=None):
    raw = json.dumps([cfg.get("YK_SHOP_ID"), path, body, extra], sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


# ---- Telegram: the message with the key leaves the chat -------------------

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
                row["text"] = "[deleted: ЮKassa key]"
                lines[i] = json.dumps(row, ensure_ascii=False) + "\n"
                changed = True
        if changed:
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                f.writelines(lines)
            os.chmod(tmp, os.stat(path).st_mode & 0o777)
            os.replace(tmp, path)


# ---- HTTP -------------------------------------------------------------------------

class Http(Exception):
    """A non-2xx answer. explain() turns it into what to tell the person."""

    def __init__(self, code, body, headers):
        super().__init__(code)
        self.code, self.headers = code, headers or {}
        try:
            j = json.loads(body or "")
        except ValueError:
            j = {}
        j = j if isinstance(j, dict) else {}
        self.err_code = str(j.get("code") or "")
        self.desc = re.sub(r"\s+", " ", str(j.get("description") or (body or "")[:200])).strip()[:300]
        self.param = str(j.get("parameter") or "")
        self.err_id = str(j.get("id") or "")
        self.retry_ms = j.get("retry_after")

    def wait(self):
        try:
            if self.retry_ms is not None:
                return float(self.retry_ms) / 1000.0
        except (TypeError, ValueError):
            pass
        try:
            return float(self.headers.get("Retry-After"))
        except (TypeError, ValueError):
            return 2.0


class Net(Exception):
    pass


def auth(cfg):
    pair = f"{cfg['YK_SHOP_ID']}:{cfg['YK_SECRET_KEY']}".encode()
    return "Basic " + base64.b64encode(pair).decode()


def api(cfg, method, path, params=None, body=None, idem=None):
    """One call with a 30 s timeout. A 429 waits once (ЮKassa's retry_after, else 2 s) and retries; a 500
    is asked once more. Retries send the same Idempotence-Key, so ЮKassa never does a write twice."""
    url = API + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    headers = {"Authorization": auth(cfg), "Accept": "application/json", "User-Agent": "oinoa-yookassa/1.0"}
    data = None
    if method == "POST":
        headers["Idempotence-Key"] = idem or str(uuid.uuid4())
        headers["Content-Type"] = "application/json"
        data = json.dumps(body if body is not None else {}, ensure_ascii=False).encode("utf-8")
    for attempt in (1, 2):
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                raw = r.read()
            if not raw.strip():
                return {}
            try:
                return json.loads(raw)
            except ValueError:
                raise Http(502, raw[:200].decode("utf-8", "replace"), {})
        except urllib.error.HTTPError as e:
            err = Http(e.code, e.read(4000).decode("utf-8", "replace"), e.headers)
            if attempt == 1 and e.code == 429 and err.wait() <= RETRY_CAP:
                time.sleep(max(1.0, err.wait()) * WAIT)
                continue
            if attempt == 1 and e.code >= 500:
                time.sleep(2.0 * WAIT)
                continue
            raise err
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as e:
            raise Net(str(getattr(e, "reason", e))[:120])


def explain(e, what="this", pid=""):
    """(code, text) for an Http or Net error, in words the agent can pass on."""
    if isinstance(e, Net):
        return "network", (f"could not reach ЮKassa ({e}). Try again in a minute; if it keeps failing, say ЮKassa "
                           "is not answering from this server.")
    code, desc, param = e.code, e.desc, e.param
    low = (desc + " " + param).lower()
    ref = f" ЮKassa error id {e.err_id}, for their support." if e.err_id else ""
    if code == 401:
        return "wrong_key", (f"ЮKassa did not accept the shopId and secret key ({desc or 'invalid credentials'}). "
                             "The key may be deleted or reissued (an old key stops 48 hours after a reissue), or the "
                             "shopId and the key are from different shops. They copy the key again from Интеграция, "
                             "Ключи API, and you connect again.")
    if code == 403:
        return "forbidden", (f"ЮKassa does not allow {what} for this shop ({desc or 'forbidden'}). Usually the shop's "
                             "contract or settings do not include it. Tell them; their ЮKassa manager or support can "
                             f"switch it on.{ref}")
    if code == 404 and pid:
        return "not_found", (f"ЮKassa has no {what} {pid} in this shop. Check the ID (take it from payments); a "
                             "payment of another shop is not visible with this key.")
    if code == 404:
        return "not_found", f"ЮKassa did not find it while {what} ({desc or 'not found'}).{ref}"
    if code == 429:
        return "rate_limited", ("ЮKassa says too many requests right now. Nothing was done. Wait a minute and try "
                                "once more; do not loop.")
    if code >= 500:
        return "yookassa_down", (f"ЮKassa could not answer properly ({code}). Try again in a few minutes.{ref}")
    if code == 400:
        if param.startswith("receipt") or "receipt" in low:
            return "receipt_needed", (f"ЮKassa wants 54-ФЗ receipt data ({desc}). Add the buyer's email (--email) "
                                      "and the VAT code (--vat N, ask the owner or their accountant), then run it "
                                      "again.")
        if "return_url" in low:
            return "bad_return_url", f"ЮKassa did not accept the return page ({desc}). Give a full https:// address."
        if "amount" in low:
            return "bad_amount", (f"ЮKassa did not accept the amount ({desc}). Each payment method has a minimum and "
                                  "a maximum; check the sum.")
        if "idempotence" in low:
            return "request_key", f"ЮKassa did not accept the request key ({desc}). Run the command again."
        if "status" in low or "captur" in low or "cancel" in low:
            return "wrong_status", f"ЮKassa refused: the payment is not in a state that allows {what} ({desc})."
        return "rejected", f"ЮKassa did not accept the request ({desc}{', field ' + param if param else ''}).{ref}"
    return "rejected", f"ЮKassa answered {code} ({desc}).{ref}"


def get(cfg, path, params=None, what="this", pid=""):
    try:
        return api(cfg, "GET", path, params=params)
    except (Http, Net) as e:
        code, text = explain(e, what, pid)
        fail(code, text)


def write(cfg, path, body, what, volatile=None, same=None, look=""):
    """POST with a request key kept on disk until ЮKassa gives a clear answer. Returns (answer, reused).
    volatile: fields that may change between runs (an invoice deadline); a reused key brings back the old ones.
    same: what else must match for a run to count as the same request (the invoice's number of days).
    look: where to check the result when it is unknown."""
    fp = fingerprint(cfg, path, body, same)
    pend = {k: v for k, v in load_pending().items() if time.time() - v.get("t", 0) < KEY_TTL}
    reused = fp in pend
    if reused:
        key, vol = pend[fp]["key"], pend[fp].get("vol") or {}
    else:
        key, vol = str(uuid.uuid4()), volatile or {}
        pend[fp] = {"key": key, "t": time.time(), "vol": vol}
        save_pending(pend)
    full = dict(body, **vol)
    try:
        answer = api(cfg, "POST", path, body=full, idem=key)
    except Http as e:
        if e.code >= 500:
            unknown(what, f"HTTP {e.code}", e.err_id, look)
        pend.pop(fp, None)
        save_pending(pend)
        raise
    except Net as e:
        unknown(what, str(e), "", look)
    pend.pop(fp, None)
    save_pending(pend)
    return answer, reused


def unknown(what, detail, err_id, look=""):
    fail("unknown_result", (f"ЮKassa did not confirm the {what} ({detail}). It may or may not have gone through. "
                            "Do not change the amount and do not make a new one. In a minute run exactly the same "
                            "command again: it sends the same request key, so ЮKassa cannot do it twice and returns "
                            "the first result. Or look first with: " + (look or "payments") + "."
                            + (f" ЮKassa error id {err_id}." if err_id else "")))


# ---- small helpers ------------------------------------------------------------------

def parse_ts(s):
    if not s:
        return None
    m = re.match(r"(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})(?:\.\d+)?(Z|[+-]\d{2}:?\d{2})?", str(s))
    if not m:
        return None
    dt = datetime.strptime(m.group(1) + "T" + m.group(2), "%Y-%m-%dT%H:%M:%S")
    tz = m.group(3)
    if not tz or tz == "Z":
        return dt.replace(tzinfo=UTC)
    sign = 1 if tz[0] == "+" else -1
    return dt.replace(tzinfo=timezone(sign * timedelta(hours=int(tz[1:3]), minutes=int(tz[-2:]))))


def msk(s, year=False):
    dt = parse_ts(s) if isinstance(s, str) else s
    if not dt:
        return "?"
    return dt.astimezone(MSK).strftime("%d.%m.%Y %H:%M MSK" if year else "%d.%m %H:%M")


def iso_z(dt):
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def dec(x):
    try:
        return Decimal(str(x))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(0)


def money(value, cur="RUB"):
    v = dec(value)
    text = f"{v:,.2f}" if v != v.to_integral_value() else f"{int(v):,}"
    return text.replace(",", " ") + (" ₽" if cur in ("RUB", "", None) else f" {cur}")


def amt(obj):
    obj = obj or {}
    return dec(obj.get("value")), obj.get("currency") or "RUB"


def clip(text, n=60):
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text if len(text) <= n else text[:n - 1] + "…"


def parse_amount(text):
    t = re.sub(r"[\s  ]", "", str(text)).replace(",", ".")
    t = re.sub(r"(?i)(₽|руб\.?|р\.?|rub)$", "", t)
    try:
        v = Decimal(t)
    except InvalidOperation:
        fail("bad_amount", f"«{clip(text, 30)}» is not an amount. Give a number, like 1500 or 1500.50.")
    if not v.is_finite() or v <= 0 or v != v.quantize(Decimal("0.01")):
        fail("bad_amount", "the amount is above zero, with at most 2 digits of kopecks.")
    return v.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def value(v):
    return f"{v:.2f}"


PID = re.compile(r"^[0-9a-fA-F-]{36}$")


def check_id(pid):
    if not PID.match(pid or ""):
        fail("bad_id", "a payment ID has 36 characters, like 2e5a1c3f-000f-5000-9000-1b2c3d4e5f6a. "
             "Take it from the first column of payments.")
    return pid


def method_text(p):
    m = p.get("payment_method") or {}
    t = m.get("type")
    if not t:
        return "not chosen yet"
    name = METHOD.get(t, t)
    card = m.get("card") or {}
    if t == "bank_card" and card:
        kind = card.get("card_type") if card.get("card_type") not in (None, "Unknown") else ""
        name = f"card {kind} *{card.get('last4', '????')}".replace("  ", " ")
    elif t == "yoo_money" and m.get("account_number"):
        name = f"ЮMoney *{str(m['account_number'])[-4:]}"
    return name


def status_text(p, short=True):
    st = p.get("status", "?")
    text = STATUS_EN.get(st, st)
    cd = p.get("cancellation_details") or {}
    if st == "canceled" and cd.get("reason"):
        r = REASON.get(cd["reason"])
        text += f": {r[0] if r else cd['reason']}" if short else ""
    return text


def period_start(days):
    today = datetime.now(MSK).replace(hour=0, minute=0, second=0, microsecond=0)
    return today - timedelta(days=days - 1)


def shop_label(cfg):
    return f"ЮKassa shop {cfg.get('YK_SHOP_ID')}" + (" (TEST shop, not real money)" if cfg.get("YK_TEST") == "1" else "")


def receipts_text(cfg):
    on, prov = cfg.get("YK_RECEIPTS"), cfg.get("YK_RECEIPT_PROVIDER") or ""
    if on == "1":
        return "Receipts 54-ФЗ: " + ("Чеки от ЮKassa (sent by email)" if prov in YK_RECEIPTS or not prov
                                     else f"through the online register {prov}") + "."
    if on == "0":
        return "Receipts 54-ФЗ: not sent through ЮKassa."
    return "Receipts 54-ФЗ: unknown (ЮKassa did not report the shop settings)."


# ---- connect / status / forget --------------------------------------------------------

KEY_RE = re.compile(r"(?<![A-Za-z0-9_])(?:live|test)_[A-Za-z0-9_\-]{16,}")
SHOP_LABEL = re.compile(r"(?i)(?:shop\s*_?\s*id|идентификатор(?:\s+магазина)?|магазин[а-я]*|shop)\D{0,24}?(\d{3,12})(?!\d)")


def parse_connect(raw, shop_arg):
    m = KEY_RE.search(raw)
    key = m.group(0) if m else ""
    if not key:  # a key without the usual live_/test_ start: the longest long token that is not only digits
        cands = [t for t in re.findall(r"[A-Za-z0-9_\-]{30,}", raw) if not t.isdigit()]
        key = max(cands, key=len) if cands else ""
    rest = raw.replace(key, " ") if key else raw
    shop = (shop_arg or "").strip()
    if not shop:
        lab = SHOP_LABEL.search(rest)
        digits = re.findall(r"(?<![\w-])\d{3,12}(?![\w-])", rest)
        shop = lab.group(1) if lab else (digits[0] if digits else "")
    return shop, key


def read_me(cfg):
    """GET /me: the shop's settings. Some shops may not get it with a shop key; then a one-payment
    listing proves the key, and the settings stay unknown."""
    try:
        return api(cfg, "GET", "/me"), None
    except Http as e:
        if e.code in (400, 403, 404, 405):
            try:
                r = api(cfg, "GET", "/payments", params={"limit": 1})
                return None, (r.get("items") or [None])[0]
            except (Http, Net) as e2:
                raise e2
        raise


def cmd_connect(a):
    raw = "" if sys.stdin.isatty() else sys.stdin.read()
    msgs = [m.strip() for item in (a.msg or []) for m in item.split(",") if m.strip()]
    shop, key = parse_connect(raw, a.shop_id)
    note = forget_message(a.chat, msgs, [raw.strip(), key]) if (a.chat or msgs or raw) else ""
    tail = [note] if note else []
    if not key:
        fail("no_key", "no secret key came in on stdin. A ЮKassa secret key starts with live_ (or test_ for a test "
             "shop); run connect with their message in the <<'EOF' block.", tail)
    if not shop.isdigit():
        fail("no_shop_id", "the shopId is missing: the number in Настройки, Магазин, field shopId. Ask for it and "
             "run connect again with the key and the shopId together (or --shop-id N, it is not secret).", tail)
    cfg = {"YK_SHOP_ID": shop, "YK_SECRET_KEY": key}
    try:
        me, sample = read_me(cfg)
    except (Http, Net) as e:
        code, text = explain(e, "reading the shop")
        fail(code, text + " Nothing is stored.", tail)
    values = load()
    old = values.get("YK_SHOP_ID") if values.get("YK_SECRET_KEY") else ""
    for k in [k for k in values if k.startswith("YK_") and k not in ("YK_RETURN_URL", "YK_VAT", "YK_SUBJECT")]:
        values.pop(k)
    if old and old != shop:  # another shop: its remembered defaults do not apply
        for k in ("YK_RETURN_URL", "YK_VAT", "YK_SUBJECT"):
            values.pop(k, None)
    values.update(cfg)
    values["YK_CONNECTED_AT"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    if me is not None:
        fisc = me.get("fiscalization") or {}
        on = fisc.get("enabled") if fisc else me.get("fiscalization_enabled")
        values.update({"YK_TEST": "1" if me.get("test") else "0", "YK_STATUS": me.get("status") or "",
                       "YK_RECEIPTS": "" if on is None else ("1" if on else "0"),
                       "YK_RECEIPT_PROVIDER": fisc.get("provider") or "",
                       "YK_METHODS": ",".join(me.get("payment_methods") or [])})
    else:
        test = (sample or {}).get("test")
        values["YK_TEST"] = "1" if (test is True or (test is None and key.startswith("test_"))) else "0"
    save(values)
    lines = [f"OK connected {shop_label(values)}"
             + (f", {values['YK_STATUS']}" if values.get("YK_STATUS") else "") + ".", receipts_text(values)]
    if values.get("YK_METHODS"):
        lines.append("Payment methods: " + ", ".join(METHOD.get(m, m) for m in values["YK_METHODS"].split(",")) + ".")
    if values.get("YK_STATUS") == "disabled":
        lines.append("The shop is not taking payments yet (ЮKassa says disabled): links will not work until "
                     "ЮKassa switches it on.")
    if values.get("YK_TEST") == "1":
        lines.append("This is a TEST shop: payments are not real. A test key is for tests only; for real money they "
                     "send the key of the real shop.")
    lines.append("The key can do everything the API allows for this shop: read payments, make links, refunds, "
                 "captures and cancels. Money moves only after the owner's yes.")
    if old and old != shop:
        lines.append(f"It replaces shop {old}.")
    out(*lines, *tail)


def cmd_status(a):
    cfg = load()
    if not cfg.get("YK_SECRET_KEY"):
        out("ЮKassa: not connected.")
        return
    try:
        me, _ = read_me(cfg)
        works = "the key works (checked just now)"
    except (Http, Net) as e:
        me = None
        code, text = explain(e, "reading the shop")
        works = f"ERROR {code}: {text}"
    if me is not None:
        fisc = me.get("fiscalization") or {}
        on = fisc.get("enabled") if fisc else me.get("fiscalization_enabled")
        cfg.update({"YK_TEST": "1" if me.get("test") else "0", "YK_STATUS": me.get("status") or "",
                    "YK_RECEIPTS": "" if on is None else ("1" if on else "0"),
                    "YK_RECEIPT_PROVIDER": fisc.get("provider") or "",
                    "YK_METHODS": ",".join(me.get("payment_methods") or [])})
        save(cfg)
    if a.json:
        print(json.dumps(me or {}, ensure_ascii=False, indent=1))
        return
    lines = [f"{shop_label(cfg)}: connected" + (f", {cfg['YK_STATUS']}" if cfg.get("YK_STATUS") else "")
             + f"; {works}." if not works.startswith("ERROR") else f"{shop_label(cfg)}: connected.", receipts_text(cfg)]
    if cfg.get("YK_METHODS"):
        lines.append("Payment methods: " + ", ".join(METHOD.get(m, m) for m in cfg["YK_METHODS"].split(",")) + ".")
    lines.append("After paying, buyers return to: " + (cfg.get("YK_RETURN_URL") or f"{DEFAULT_RETURN} (not set yet)"))
    if cfg.get("YK_VAT"):
        lines.append(f"Default VAT code for receipts: {cfg['YK_VAT']} ({VAT.get(int(cfg['YK_VAT']), '?')}).")
    if works.startswith("ERROR"):
        lines.append(works)
    out(*lines)
    if works.startswith("ERROR"):
        sys.exit(2)


def cmd_forget(a):
    cfg = load()
    if not cfg.get("YK_SECRET_KEY"):
        out("ЮKassa: not connected.")
        return
    for d in STORES:
        for name in (ENV_NAME, PENDING_NAME):
            try:
                os.remove(os.path.join(d, name))
            except OSError:
                pass
    out(f"OK forgot ЮKassa shop {cfg.get('YK_SHOP_ID')}. This agent can no longer reach it.",
        f"The key itself still works. If no site or CMS module uses it, they can delete it in Интеграция, Ключи API "
        f"({KEYS_PAGE}). If their site uses the same key, they leave it: deleting or reissuing it stops payments "
        "on the site.")


# ---- payments ---------------------------------------------------------------------------

def list_all(cfg, path, params):
    items, cursor, capped = [], None, False
    for page in range(MAX_PAGES):
        p = dict(params, limit=100)
        if cursor:
            p["cursor"] = cursor
        r = api(cfg, "GET", path, params=p)
        items += r.get("items") or []
        cursor = r.get("next_cursor")
        if not cursor:
            break
    else:
        capped = True
    return items, capped


def pay_line(p):
    v, cur = amt(p.get("amount"))
    return " | ".join([p.get("id", "?"), msk(p.get("created_at")), money(v, cur), status_text(p),
                       method_text(p), clip(p.get("description") or "", 50) or "-"])


def cmd_payments(a):
    cfg = need_cfg()
    if not 1 <= a.days <= 1095:
        fail("bad_days", "--days is from 1 (today) to 1095 (ЮKassa keeps payments 3 years).")
    start = period_start(a.days)
    params = {"created_at.gte": iso_z(start)}
    if a.status:
        params["status"] = a.status
    try:
        items, capped = list_all(cfg, "/payments", params)
    except (Http, Net) as e:
        code, text = explain(e, "reading payments")
        fail(code, text)
    refunds, refund_err = [], ""
    try:
        refunds, _ = list_all(cfg, "/refunds", {"created_at.gte": iso_z(start), "status": "succeeded"})
    except (Http, Net) as e:
        refund_err = explain(e, "reading refunds")[1]
    if a.json:
        print(json.dumps({"payments": items, "refunds": refunds}, ensure_ascii=False, indent=1))
        return
    span = "today" if a.days == 1 else f"last {a.days} days"
    head = f"{shop_label(cfg)}, {span} (since {start.strftime('%d.%m')} 00:00 MSK)"
    filt = f", status {a.status}" if a.status else ""
    if not items:
        out(f"{head}: no payments{filt}.",
            f"Refunded in this period: {len(refunds)}" + (f" for {sums(refunds)}." if refunds else ".")
            if not refund_err else f"Refunds: not read ({refund_err})")
        return
    by = {}
    for p in items:
        by.setdefault(p.get("status"), []).append(p)
    paid = by.get("succeeded", [])
    hold = by.get("waiting_for_capture", [])
    lines = [f"{head}: {len(items)} payments{filt}."]
    if paid:
        income = sums(paid, "income_amount")
        lines.append(f"Paid: {len(paid)} for {sums(paid)}" + (f", after the ЮKassa fee {income}" if income else "") + ".")
    if not a.status or a.status != "succeeded":
        rest = []
        if by.get("pending"):
            rest.append(f"waiting for the buyer {len(by['pending'])}")
        if hold:
            rest.append(f"paid and waiting for your capture {len(hold)} for {sums(hold)}")
        if by.get("canceled"):
            rest.append(f"canceled {len(by['canceled'])}")
        if rest:
            text = ", ".join(rest)
            lines.append(text[0].upper() + text[1:] + ".")
    if refund_err:
        lines.append(f"Refunds: not read ({refund_err})")
    else:
        lines.append(f"Refunded in this period: {len(refunds)}" + (f" for {sums(refunds)}." if refunds else "."))
    if capped:
        lines.append(f"More than {MAX_PAGES * 100} payments: the totals cover the newest {MAX_PAGES * 100}. "
                     "Use a shorter --days.")
    lines.append("ID | created MSK | amount | status | method | description")
    lines += [pay_line(p) for p in items[:a.limit]]
    if len(items) > a.limit:
        lines.append(f"... and {len(items) - a.limit} more (--limit {len(items)} shows all).")
    out(*lines)


def sums(objs, field="amount"):
    total = {}
    for o in objs:
        if not o.get(field):
            continue
        v, cur = amt(o.get(field))
        total[cur] = total.get(cur, Decimal(0)) + v
    return ", ".join(money(v, c) for c, v in sorted(total.items()))


def cmd_payment(a):
    cfg = need_cfg()
    pid = check_id(a.id)
    p = get(cfg, f"/payments/{pid}", what="payment", pid=pid)
    try:
        refunds, _ = list_all(cfg, "/refunds", {"payment_id": pid})
    except (Http, Net):
        refunds = None
    if a.json:
        print(json.dumps({"payment": p, "refunds": refunds}, ensure_ascii=False, indent=1))
        return
    v, cur = amt(p.get("amount"))
    lines = [f"Payment {pid}" + (" (TEST, not real money)" if p.get("test") else "") + f": {money(v, cur)}, "
             f"{status_text(p, short=False)}.",
             f"Created {msk(p.get('created_at'), year=True)}."
             + (f" Paid {msk(p.get('captured_at'), year=True)}." if p.get("captured_at") else ""),
             f"Description: {p.get('description') or '-'}",
             f"Method: {method_text(p)}" + issuer(p)]
    if p.get("income_amount"):
        iv, icur = amt(p["income_amount"])
        lines.append(f"After the ЮKassa fee: {money(iv, icur)} (fee {money(v - iv, cur)}).")
    st = p.get("status")
    if st == "pending":
        url = (p.get("confirmation") or {}).get("confirmation_url")
        lines.append("The buyer has not paid yet. It cancels itself when the time to pay runs out (about an hour "
                     "for a payment link)." + (f" Link: {url}" if url else ""))
    if st == "waiting_for_capture":
        lines.append(f"The money is frozen on the buyer's side. Capture it (capture) or give it back (cancel) by "
                     f"{msk(p.get('expires_at'), year=True)}; after that ЮKassa cancels it and returns the money.")
    cd = p.get("cancellation_details") or {}
    if st == "canceled":
        r = REASON.get(cd.get("reason"))
        lines.append(f"Кто отменил: {PARTY.get(cd.get('party'), cd.get('party') or 'не указано')}.")
        lines.append(f"Причина ({cd.get('reason') or 'не указана'}): {r[1] if r else 'ЮKassa не дала пояснения.'}")
    if p.get("refunded_amount"):
        rv, rcur = amt(p["refunded_amount"])
        lines.append(f"Refunded so far: {money(rv, rcur)} of {money(v, cur)}.")
    if st == "succeeded":
        lines.append("Refund through the API: " + ("possible." if p.get("refundable") else
                                                    "not possible for this payment (method or time limit)."))
    for r in refunds or []:
        rv, rcur = amt(r.get("amount"))
        why = ""
        if r.get("status") == "canceled":
            rc = (r.get("cancellation_details") or {}).get("reason")
            why = f": {REFUND_REASON.get(rc, rc or 'причина не указана')}"
        lines.append(f"Refund {r.get('id')} | {msk(r.get('created_at'))} | {money(rv, rcur)} | "
                     f"{r.get('status')}{why}")
    if refunds is None:
        lines.append("Refunds of this payment: could not be read.")
    reg = p.get("receipt_registration")
    if reg:
        lines.append("Receipt 54-ФЗ: " + {"succeeded": "registered.", "pending": "being registered.",
                                          "canceled": "FAILED to register. With Чеки от ЮKassa, write to ЮKassa "
                                                      "support; with their own online register, make it by hand."}
                     .get(reg, reg))
    tds = ((p.get("authorization_details") or {}).get("three_d_secure") or {})
    if "applied" in tds:
        lines.append("3-D Secure: " + ("asked." if tds["applied"] else "not asked."))
    out(*lines)


def issuer(p):
    card = (p.get("payment_method") or {}).get("card") or {}
    bits = [x for x in (card.get("issuer_name"), card.get("issuer_country")) if x]
    return f" ({', '.join(bits)})" if bits else ""


# ---- receipts (54-ФЗ) ---------------------------------------------------------------------

def receipt_for(a, cfg, total, description, base=None):
    """A one-line receipt, or None when the shop does not send receipts through ЮKassa.
    Returns (receipt, preview line). Fails with receipt_needed when something is missing."""
    on = cfg.get("YK_RECEIPTS") == "1"
    given = a.email or a.phone or a.vat
    if not (on or given):
        return None, None
    prov = cfg.get("YK_RECEIPT_PROVIDER") or ""
    own = prov in YK_RECEIPTS or (on and not prov)
    base = base or {}
    vat = a.vat or base.get("vat_code") or (int(cfg["YK_VAT"]) if cfg.get("YK_VAT", "").isdigit() else None)
    missing = []
    customer = {}
    if a.email:
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", a.email):
            fail("bad_email", "that is not an email address.")
        customer["email"] = a.email
    elif a.phone:
        digits = re.sub(r"\D", "", a.phone)
        if len(digits) == 11 and digits.startswith("8"):
            digits = "7" + digits[1:]
        if not 4 <= len(digits) <= 15:
            fail("bad_phone", "that is not a phone number. Give it with the country code, like +7 900 000-00-00.")
        if own:
            missing.append("the buyer's email (Чеки от ЮKassa sends receipts only by email, not by SMS)")
        customer["phone"] = digits
    else:
        missing.append("the buyer's email (--email)" + ("" if own else " or phone (--phone)"))
    if not vat:
        missing.append("the VAT code (--vat N: 1 без НДС, 11 НДС 22%, 7 НДС 5%, 8 НДС 7%, 3 НДС 10%; ask the owner "
                       "or their accountant)")
    if missing:
        why = ("this shop sends 54-ФЗ receipts through ЮKassa, so it needs " if on
               else "a receipt was asked for, so it needs ")
        fail("receipt_needed", why + " and ".join(missing) + ". Ask the owner, then run it again.")
    subject = a.subject or base.get("payment_subject") or cfg.get("YK_SUBJECT") or "commodity"
    mode = a.mode or base.get("payment_mode") or "full_payment"
    item = {"description": clip(description, 128), "quantity": 1, "amount": {"value": value(total), "currency": "RUB"},
            "vat_code": int(vat), "payment_subject": subject, "payment_mode": mode}
    receipt = {"customer": customer, "items": [item]}
    if a.tax:
        receipt["tax_system_code"] = a.tax
    line = (f"Receipt 54-ФЗ to {customer.get('email') or '+' + customer.get('phone', '')}: «{item['description']}» x1, "
            f"{money(total)}, VAT code {vat} ({VAT.get(int(vat), '?')}), {subject}, {mode}.")
    return receipt, line


def remember(cfg, **kv):
    changed = {k: str(v) for k, v in kv.items() if v not in (None, "") and cfg.get(k) != str(v)}
    if changed:
        cfg.update(changed)
        save(cfg)


# ---- link ---------------------------------------------------------------------------------

def cmd_link(a):
    cfg = need_cfg()
    total = parse_amount(a.amount)
    desc = " ".join(a.description).strip()
    if not desc:
        fail("no_description", "say what the payment is for: it shows to the buyer and in the cabinet.")
    if len(desc) > 128:
        fail("long_description", f"the description is {len(desc)} characters; ЮKassa takes at most 128. Shorten it.")
    if a.days is not None and not 1 <= a.days <= 30:
        fail("bad_days", "an invoice can wait from 1 to 30 days (ЮKassa's limit).")
    if a.days and total < 1:
        fail("bad_amount", "an invoice is at least 1 ₽.")
    ret = a.return_url or cfg.get("YK_RETURN_URL") or DEFAULT_RETURN
    if not re.match(r"^https?://\S+$", ret) or len(ret) > 2048:
        fail("bad_return_url", "the return page must be a full address that starts with https://.")
    receipt, rline = receipt_for(a, cfg, total, desc)
    test = " TEST shop: no real money moves." if cfg.get("YK_TEST") == "1" else ""
    if a.days:
        deadline = (datetime.now(UTC) + timedelta(days=a.days)).replace(second=0, microsecond=0)
        plan = (f"Invoice link: {money(total)}, «{desc}», paid at once (one stage). The buyer can pay until about "
                f"{msk(deadline, year=True)} ({a.days} {'day' if a.days == 1 else 'days'}).")
        where = None
    else:
        plan = (f"Payment link: {money(total)}, «{desc}», paid at once (one stage). The buyer has about 1 hour to pay "
                "(ЮKassa's limit for a payment link); --days N makes an invoice that waits up to 30 days.")
        where = (f"After paying, the buyer returns to {ret}"
                 + (" (not set: ask the owner for their site or Telegram link and add --return-url URL, it is "
                    "remembered)." if ret == DEFAULT_RETURN and not a.return_url else "."))
    if not a.yes:
        out(f"PREVIEW {plan}{test}", rline, where,
            "Nothing created. Show it to the owner; after a clear yes run the same command with --yes.")
        return
    if a.days:
        body = {"payment_data": {"amount": {"value": value(total), "currency": "RUB"}, "capture": True,
                                 "description": desc},
                "cart": [{"description": clip(desc, 128), "price": {"value": value(total), "currency": "RUB"},
                          "quantity": 1}],
                "delivery_method_data": {"type": "self"}, "locale": "ru_RU", "description": desc}
        if receipt:
            body["payment_data"]["receipt"] = receipt
        path, what, vol, same = "/invoices", "invoice", {"expires_at": iso_z(deadline)}, a.days
    else:
        body = {"amount": {"value": value(total), "currency": "RUB"}, "capture": True,
                "confirmation": {"type": "redirect", "return_url": ret, "locale": "ru_RU"}, "description": desc}
        if receipt:
            body["receipt"] = receipt
        path, what, vol, same = "/payments", "payment link", None, None
    try:
        r, reused = write(cfg, path, body, what, volatile=vol, same=same)
    except Http as e:
        code, text = explain(e, f"creating a {what}")
        fail(code, text + " Nothing was created.")
    remember(cfg, YK_RETURN_URL=a.return_url, YK_VAT=(receipt and receipt["items"][0]["vat_code"]),
             YK_SUBJECT=(receipt and receipt["items"][0]["payment_subject"]))
    if a.json:
        print(json.dumps(r, ensure_ascii=False, indent=1))
        return
    again = "Same request as the one that got no answer before: ЮKassa returned the first result. " if reused else ""
    if a.days:
        url = (r.get("delivery_method") or {}).get("url")
        out(f"OK invoice {r.get('id')}: {money(total)}, «{desc}», until {msk(r.get('expires_at'), year=True)}.{test}",
            f"Link for the buyer: {url or '(ЮKassa returned no link; open Счета клиентам in the cabinet)'}",
            f"{again}When it is paid, the payment shows in payments.")
        return
    url = (r.get("confirmation") or {}).get("confirmation_url")
    out(f"OK payment {r.get('id')}: {money(total)}, «{desc}», {status_text(r)}.{test}",
        f"Link for the buyer: {url or '(ЮKassa returned no link)'}",
        f"{again}The buyer has about 1 hour. Check it with: payment {r.get('id')}")


# ---- refund / capture / cancel ---------------------------------------------------------

def original_item(cfg, pid):
    """The first item of the payment's own receipt: its VAT code, subject and mode carry over."""
    try:
        r = api(cfg, "GET", "/receipts", params={"payment_id": pid, "limit": 10})
    except (Http, Net):
        return {}
    for rec in r.get("items") or []:
        if rec.get("type") == "payment" and rec.get("items"):
            return rec["items"][0]
    return {}


def cmd_refund(a):
    cfg = need_cfg()
    pid = check_id(a.id)
    p = get(cfg, f"/payments/{pid}", what="payment", pid=pid)
    v, cur = amt(p.get("amount"))
    done, _ = amt(p.get("refunded_amount"))
    left = v - done
    what = f"payment {pid}: {money(v, cur)}, {msk(p.get('created_at'), year=True)}, {method_text(p)}, " \
           f"«{clip(p.get('description') or '-', 60)}»"
    if p.get("status") != "succeeded":
        hint = {"waiting_for_capture": " It is a two-stage payment still frozen: use cancel instead.",
                "pending": " The buyer has not paid it.", "canceled": " It was canceled; the money did not stay."}
        fail("not_refundable", f"only a paid payment can be refunded; this one is {status_text(p)}."
             + hint.get(p.get("status"), ""))
    if not p.get("refundable"):
        fail("not_refundable", "ЮKassa says this payment cannot be refunded through the API (the method does not "
             "allow it, or it is too old: cards 15 months, most methods 3 years). Agree with the buyer on a direct "
             "transfer.")
    if left <= 0:
        fail("already_refunded", f"the whole payment is already refunded ({money(done, cur)}).")
    total = parse_amount(a.amount) if a.amount else left
    if total > left:
        fail("too_much", f"only {money(left, cur)} is left to refund ({money(done, cur)} already returned).")
    partial = total < v
    if partial and total < 1:
        fail("too_small", "a partial refund is at least 1 ₽.")
    if Decimal(0) < left - total < 1:
        fail("remainder", f"after a partial refund at least 1 ₽ must stay, or refund all {money(left, cur)}. "
             f"Refund {money(left - 1, cur)} or {money(left, cur)}.")
    if a.reason and len(a.reason) > 250:
        fail("long_reason", "the refund comment is at most 250 characters.")
    receipt, rline = None, None
    if partial or done > 0:  # a full refund takes the receipt data from the payment itself
        if cfg.get("YK_RECEIPTS") == "1" or p.get("receipt_registration") or a.email or a.phone:
            if p.get("receipt_registration") and cfg.get("YK_RECEIPTS") != "1":
                cfg = dict(cfg, YK_RECEIPTS="1")
            base = original_item(cfg, pid)
            receipt, rline = receipt_for(a, cfg, total, base.get("description") or p.get("description") or "Оплата",
                                         base=base)
    stays = (f"After this {money(left - total, cur)} of it stays with you." if left > total
             else "After this nothing of it stays with you" + (" (a full refund)." if done == 0 else "."))
    plan = (f"Refund {money(total, cur)} of {what}. Already refunded: {money(done, cur)}. {stays} The money goes "
            f"back to the same {method_text(p)}; the ЮKassa fee for the payment is not returned.")
    test = " TEST shop: no real money moves." if p.get("test") else ""
    if not a.yes:
        out(f"PREVIEW {plan}{test}", rline,
            "Nothing changed. Show it to the owner; after a clear yes run the same command with --yes.")
        return
    body = {"payment_id": pid, "amount": {"value": value(total), "currency": cur}}
    if a.reason:
        body["description"] = a.reason
    if receipt:
        body["receipt"] = receipt
    try:
        r, reused = write(cfg, "/refunds", body, "refund", look=f"payment {pid}")
    except Http as e:
        code, text = explain(e, "a refund of this payment", pid)
        fail(code, text + " Nothing was refunded.")
    if a.json:
        print(json.dumps(r, ensure_ascii=False, indent=1))
        return
    st = r.get("status")
    again = " Same request as the one that got no answer before: ЮKassa returned the first result." if reused else ""
    if st == "canceled":
        rc = (r.get("cancellation_details") or {}).get("reason")
        fail("refund_canceled", f"ЮKassa did not make refund {r.get('id')}: {REFUND_REASON.get(rc, rc or 'no reason')}.")
    rv, rcur = amt(r.get("amount"))
    out(f"OK refund {r.get('id')}: {money(rv, rcur)} of payment {pid}, "
        + ("done." if st == "succeeded" else "being processed by ЮKassa; check later with payment " + pid + ".")
        + test + again,
        "Cards and ЮMoney usually see it at once; the buyer's bank may show it within a few days.")


def two_stage(cfg, pid, verb):
    p = get(cfg, f"/payments/{pid}", what="payment", pid=pid)
    st = p.get("status")
    if st != "waiting_for_capture":
        hint = {"succeeded": " It is already paid in full: to give money back use refund.",
                "pending": " The buyer has not paid yet; an unpaid payment cancels itself when its time runs out.",
                "canceled": " It is already canceled."}
        fail("wrong_status", f"only a two-stage payment that waits for capture can be {verb}; this one is "
             f"{status_text(p)}." + hint.get(st, ""))
    return p


def cmd_capture(a):
    cfg = need_cfg()
    pid = check_id(a.id)
    p = two_stage(cfg, pid, "captured")
    v, cur = amt(p.get("amount"))
    total = parse_amount(a.amount) if a.amount else v
    if total > v:
        fail("too_much", f"the buyer froze {money(v, cur)}; you cannot take more.")
    partial = total < v
    receipt, rline = None, None
    if partial and (cfg.get("YK_RECEIPTS") == "1" or p.get("receipt_registration") or a.email or a.phone):
        receipt, rline = receipt_for(a, dict(cfg, YK_RECEIPTS="1"), total, p.get("description") or "Оплата",
                                     base=original_item(cfg, pid))
    plan = (f"Capture {money(total, cur)} of payment {pid} ({method_text(p)}, «{clip(p.get('description') or '-', 60)}»)"
            f", frozen until {msk(p.get('expires_at'), year=True)}.")
    if partial:
        plan += (f" The other {money(v - total, cur)} goes back to the buyer. Partial capture works only for cards, "
                 "ЮMoney, SberPay, Mir Pay, T-Pay, Alfa Pay, «Покупки в кредит» and «Плати частями».")
    test = " TEST shop: no real money moves." if p.get("test") else ""
    if not a.yes:
        out(f"PREVIEW {plan}{test}", rline,
            "Nothing changed. Show it to the owner; after a clear yes run the same command with --yes.")
        return
    body = {}
    if partial:
        body["amount"] = {"value": value(total), "currency": cur}
        if receipt:
            body["receipt"] = receipt
    try:
        r, reused = write(cfg, f"/payments/{pid}/capture", body, "capture", look=f"payment {pid}")
    except Http as e:
        code, text = explain(e, "a capture of this payment", pid)
        fail(code, text + " Nothing was captured.")
    if a.json:
        print(json.dumps(r, ensure_ascii=False, indent=1))
        return
    rv, rcur = amt(r.get("amount"))
    out(f"OK payment {pid}: {money(rv, rcur)} captured, status {status_text(r)}.{test}"
        + (" Same request as before: ЮKassa returned the first result." if reused else ""))


def cmd_cancel(a):
    cfg = need_cfg()
    pid = check_id(a.id)
    p = two_stage(cfg, pid, "canceled")
    v, cur = amt(p.get("amount"))
    plan = (f"Cancel payment {pid}: {money(v, cur)} ({method_text(p)}, «{clip(p.get('description') or '-', 60)}»), "
            "frozen and waiting for capture. The money goes back to the buyer (a card, ЮMoney or SberPay at once, "
            "other methods within a few days); ЮKassa takes no fee for it.")
    test = " TEST shop: no real money moves." if p.get("test") else ""
    if not a.yes:
        out(f"PREVIEW {plan}{test}",
            "Nothing changed. Show it to the owner; after a clear yes run the same command with --yes.")
        return
    try:
        r, reused = write(cfg, f"/payments/{pid}/cancel", {}, "cancel", look=f"payment {pid}")
    except Http as e:
        code, text = explain(e, "a cancel of this payment", pid)
        fail(code, text + " Nothing was canceled.")
    if a.json:
        print(json.dumps(r, ensure_ascii=False, indent=1))
        return
    out(f"OK payment {pid} canceled: {money(v, cur)} goes back to the buyer.{test}"
        + (" Same request as before: ЮKassa returned the first result." if reused else ""))


# ---- selftest: everything above against a local stub, no network ------------------------

def selftest():
    import http.server
    import shutil
    import subprocess
    import tempfile
    import threading

    SHOP, KEY = "123456", "live_SeCrEtKeY_0123456789abcdefABCDEF-xyz"
    BAD = "live_WrongKey_0123456789abcdefABCDEF-xyz"
    TSHOP, TKEY = "654321", "test_TeStKeY_0123456789abcdefABCDEF-xyz"
    TG_TOKEN = "123456:TEST-BOT-TOKEN-SECRET"
    good = {"Basic " + base64.b64encode(f"{SHOP}:{KEY}".encode()).decode(): (SHOP, False),
            "Basic " + base64.b64encode(f"{TSHOP}:{TKEY}".encode()).decode(): (TSHOP, True)}
    secrets = [KEY, BAD, TKEY, TG_TOKEN, KEY.split("_", 1)[1], TKEY.split("_", 1)[1]] + list(good)
    now = datetime.now(UTC)
    iso = lambda dt: dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    ids = {n: f"2e5a1c3f-000f-5000-9000-00000000000{n}" for n in range(1, 8)}
    card = lambda t, l4: {"type": "bank_card", "id": "x", "saved": False, "title": f"Bank card *{l4}",
                          "card": {"last4": l4, "card_type": t, "expiry_year": "2030", "expiry_month": "01",
                                   "issuer_name": "Sberbank", "issuer_country": "RU"}}
    rec = lambda: {"account_id": SHOP, "gateway_id": "1"}
    P = {
        ids[1]: {"id": ids[1], "status": "succeeded", "paid": True, "amount": {"value": "1500.00", "currency": "RUB"},
                 "income_amount": {"value": "1447.50", "currency": "RUB"}, "description": "Заказ 72",
                 "created_at": iso(now - timedelta(minutes=50)), "captured_at": iso(now - timedelta(minutes=49)),
                 "payment_method": card("Mir", "4477"), "test": False, "refundable": True, "recipient": rec(),
                 "receipt_registration": "succeeded",
                 "authorization_details": {"rrn": "1", "auth_code": "2", "three_d_secure": {"applied": True}}},
        ids[2]: {"id": ids[2], "status": "canceled", "paid": False, "amount": {"value": "990.00", "currency": "RUB"},
                 "description": "Заказ 71", "created_at": iso(now - timedelta(minutes=40)),
                 "payment_method": card("Visa", "1111"), "test": False, "refundable": False, "recipient": rec(),
                 "cancellation_details": {"party": "payment_network", "reason": "insufficient_funds"}},
        ids[3]: {"id": ids[3], "status": "pending", "paid": False, "amount": {"value": "500.00", "currency": "RUB"},
                 "description": "Заказ 70", "created_at": iso(now - timedelta(minutes=30)), "test": False,
                 "refundable": False, "recipient": rec(),
                 "confirmation": {"type": "redirect", "confirmation_url": "https://yoomoney.ru/checkout/payments/v2/contract?orderId=3"}},
        ids[4]: {"id": ids[4], "status": "waiting_for_capture", "paid": True,
                 "amount": {"value": "2000.00", "currency": "RUB"}, "description": "Бронь стола",
                 "created_at": iso(now - timedelta(minutes=20)), "expires_at": iso(now + timedelta(days=6)),
                 "payment_method": card("MasterCard", "2222"), "test": False, "refundable": False, "recipient": rec()},
        ids[5]: {"id": ids[5], "status": "succeeded", "paid": True, "amount": {"value": "300.00", "currency": "RUB"},
                 "income_amount": {"value": "297.00", "currency": "RUB"}, "description": "Кофе",
                 "refunded_amount": {"value": "100.00", "currency": "RUB"},
                 "created_at": iso(now - timedelta(minutes=10)), "payment_method": {"type": "sbp", "id": "y"},
                 "test": False, "refundable": True, "recipient": rec()},
    }
    R5 = {"id": "3f000000-0015-5000-8000-000000000001", "payment_id": ids[5], "status": "succeeded",
          "amount": {"value": "100.00", "currency": "RUB"}, "created_at": iso(now - timedelta(minutes=5))}
    state = {"log": [], "force": {}, "idem": {}, "made": 0,
             "me": {"account_id": SHOP, "test": False, "status": "enabled",
                    "fiscalization": {"enabled": True, "provider": "avanpost"}, "fiscalization_enabled": True,
                    "payment_methods": ["bank_card", "sbp", "sberbank", "yoo_money"], "itn": "7700000000"}}

    class Stub(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def handle_any(self):
            url = urllib.parse.urlparse(self.path)
            q = dict(urllib.parse.parse_qsl(url.query))
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n).decode() if n else ""
            entry = {"method": self.command, "path": url.path, "query": q, "body": raw,
                     "headers": {k: v for k, v in self.headers.items()}}
            state["log"].append(entry)
            if url.path.startswith("/bot"):
                return self.reply(200, {"ok": url.path == f"/bot{TG_TOKEN}/deleteMessage"})
            queue = state["force"].get((self.command, url.path)) or []
            if queue:
                code, payload, hdrs = queue.pop(0)
                return self.reply(code, payload, hdrs)
            route = url.path[3:] if url.path.startswith("/v3/") else url.path
            who = good.get(self.headers.get("Authorization"))
            if not who:
                return self.reply(401, {"type": "error", "id": "e-401", "code": "invalid_credentials",
                                        "description": "Authentication by given credentials failed",
                                        "parameter": "Authorization"})
            if self.command == "POST":
                key = self.headers.get("Idempotence-Key") or ""
                if not re.fullmatch(r"[0-9a-zA-Z+\-_.]{1,64}", key):
                    return self.reply(400, {"type": "error", "id": "e-k", "code": "invalid_request",
                                            "description": "Idempotence key is missing", "parameter": "Idempotence-Key"})
                if key in state["idem"]:
                    return self.reply(200, state["idem"][key])
                body = json.loads(raw or "{}")
                ans = self.post(route, body, who)
                if ans[0] == 200:
                    state["idem"][key] = ans[1]
                    state["made"] += 1
                return self.reply(*ans)
            return self.reply(*self.get(route, q, who))

        def get(self, path, q, who):
            if path == "/me":
                return 200, dict(state["me"], account_id=who[0], test=who[1])
            if path == "/payments":
                items = sorted(P.values(), key=lambda p: p["created_at"], reverse=True)
                if q.get("status"):
                    items = [p for p in items if p["status"] == q["status"]]
                lim = int(q.get("limit", 10))
                if lim == 1:
                    return 200, {"type": "list", "items": items[:1]}
                if q.get("cursor") == "c2":
                    return 200, {"type": "list", "items": items[3:]}
                return 200, dict({"type": "list", "items": items[:3]}, **({"next_cursor": "c2"} if len(items) > 3 else {}))
            if path.startswith("/payments/"):
                p = P.get(path.split("/")[2])
                if not p:
                    return 404, {"type": "error", "id": "e-404", "code": "not_found", "parameter": "payment_id",
                                 "description": "Incorrect payment_id. Payment doesn't exist or access denied."}
                return 200, p
            if path == "/refunds":
                items = [R5] if q.get("payment_id") in (None, ids[5]) else []
                return 200, {"type": "list", "items": items}
            if path == "/receipts":
                items = [{"id": "r1", "type": "payment", "status": "succeeded", "payment_id": ids[1],
                          "items": [{"description": "Заказ 72", "quantity": 1, "vat_code": 1,
                                     "amount": {"value": "1500.00", "currency": "RUB"},
                                     "payment_subject": "service", "payment_mode": "full_payment"}]}]
                return 200, {"type": "list", "items": items if q.get("payment_id") == ids[1] else []}
            return 404, {"type": "error", "code": "not_found", "description": "no route"}

        def post(self, path, body, who):
            if path == "/payments":
                if state["me"]["fiscalization"]["enabled"] and not body.get("receipt"):
                    return 400, {"type": "error", "id": "e-r", "code": "invalid_request",
                                 "description": "Receipt is missing or illegal", "parameter": "receipt"}
                return 200, {"id": ids[6], "status": "pending", "paid": False, "amount": body["amount"],
                             "description": body.get("description"), "created_at": iso(now), "test": who[1],
                             "refundable": False, "recipient": rec(),
                             "confirmation": {"type": "redirect",
                                              "confirmation_url": f"https://yoomoney.ru/checkout/payments/v2/contract?orderId={ids[6]}"}}
            if path == "/invoices":
                return 200, {"id": "in-0001", "status": "pending", "cart": body["cart"], "created_at": iso(now),
                             "expires_at": body.get("expires_at"),
                             "delivery_method": {"type": "self", "url": "https://yookassa.ru/my/i/in-0001/l"}}
            if path == "/refunds":
                return 200, {"id": "3f000000-0015-5000-8000-000000000009", "payment_id": body["payment_id"],
                             "status": "succeeded", "amount": body["amount"], "created_at": iso(now)}
            m = re.match(r"^/payments/([^/]+)/(capture|cancel)$", path)
            if m and m.group(1) in P:
                p = dict(P[m.group(1)])
                if m.group(2) == "capture":
                    p.update(status="succeeded", amount=body.get("amount") or p["amount"])
                else:
                    p.update(status="canceled", cancellation_details={"party": "merchant", "reason": "canceled_by_merchant"})
                return 200, p
            return 404, {"type": "error", "code": "not_found", "description": "no route"}

        def reply(self, code, payload, headers=None):
            raw = b"" if payload is None else json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(code)
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        do_GET = do_POST = handle_any

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Stub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    tmp = tempfile.mkdtemp(prefix="yk-selftest-")
    store = os.path.join(tmp, "home", ".oinoa")
    tg = os.path.join(tmp, "tg")
    os.makedirs(os.path.join(tg, "msglog"))
    with open(os.path.join(tg, ".env"), "w") as f:
        f.write(f"TELEGRAM_BOT_TOKEN={TG_TOKEN}\n")
    log_file = os.path.join(tg, "msglog", "2026-10-18.jsonl")
    with open(log_file, "w") as f:
        f.write(json.dumps({"dir": "in", "message_id": 42, "text": f"shopId {SHOP}\nключ {KEY}"},
                           ensure_ascii=False) + "\n")
        f.write(json.dumps({"dir": "in", "message_id": 44, "text": "сколько пришло за сегодня?"},
                           ensure_ascii=False) + "\n")
    env = {"PATH": os.environ.get("PATH", ""), "HOME": os.path.join(tmp, "home"), "OINOA_YK_DIR": store,
           "TELEGRAM_STATE_DIR": tg, "OINOA_TG_API": base, "OINOA_YOOKASSA_API": base + "/v3",
           "OINOA_YK_WAIT": "0.01", "LANG": "C.UTF-8", "PYTHONIOENCODING": "utf-8"}
    results, seen = [], []
    path = os.path.join(store, ENV_NAME)
    pending = os.path.join(store, PENDING_NAME)

    def yk(args, stdin="", extra=None):
        p = subprocess.run([sys.executable, "-I", os.path.abspath(__file__)] + args, input=stdin,
                           env=dict(env, **(extra or {})), capture_output=True, text=True, timeout=90)
        seen.append(p.stdout + p.stderr)
        if os.environ.get("OINOA_YK_SELFTEST_SHOW"):  # print each answer, to read what the agent will see
            print(f"$ yk.py {' '.join(args)}  (exit {p.returncode})\n{p.stdout}{p.stderr}")
        return p.returncode, p.stdout, p.stderr

    def check(cond, label, detail=""):
        results.append((bool(cond), label, detail))

    def calls(p, method=None):
        return [x for x in state["log"] if x["path"] == p and (method is None or x["method"] == method)]

    def posts():
        return [x for x in state["log"] if x["method"] == "POST" and not x["path"].startswith("/bot")]

    # connect: shopId and key in one message, on stdin; the message leaves the chat and the log
    rc, so, se = yk(["connect", "--chat", "7", "--msg", "42"], f"Вот shopId: {SHOP}\nСекретный ключ: {KEY}\n")
    check(rc == 0 and f"OK connected ЮKassa shop {SHOP}, enabled" in so and "Чеки от ЮKassa" in so
          and "СБП" in so, "connect", so + se)
    check(calls("/v3/me") and calls("/v3/me")[-1]["headers"].get("Authorization") in good, "basic auth shopId:key")
    check(os.path.exists(path) and (os.stat(path).st_mode & 0o777) == 0o600, "key file is 0600")
    check((os.stat(store).st_mode & 0o777) == 0o700, "key folder is 0700")
    check(KEY in open(path).read(), "key stored")
    dels = calls(f"/bot{TG_TOKEN}/deleteMessage")
    check(dels and "message_id=42" in dels[-1]["body"] and "message with the key is deleted" in so,
          "telegram message deleted")
    check(KEY not in open(log_file).read() and "сколько пришло" in open(log_file).read(), "msglog scrubbed, others kept")
    check(so.count(KEY) == 0 and se == "", "connect prints nothing secret")

    # a wrong key is refused and the stored one stays; other ways to paste
    rc, so, se = yk(["connect"], f"{SHOP}:{BAD}\n")
    check(rc == 2 and "ERROR wrong_key" in so and "Nothing is stored" in so, "wrong key refused", so)
    check(BAD not in open(path).read() and KEY in open(path).read(), "wrong key not stored, old kept")
    rc, so, se = yk(["connect"], "просто текст 123456\n")
    check(rc == 2 and "ERROR no_key" in so, "no key on stdin", so)
    rc, so, se = yk(["connect"], f"{KEY}\n")
    check(rc == 2 and "ERROR no_shop_id" in so, "no shopId", so)
    rc, so, se = yk(["connect", KEY], "")
    check(rc == 2 and "ERROR bad_args" in so and KEY not in so + se, "key in argv refused, not echoed", so + se)
    rc, so, se = yk(["connect"], f"{KEY}\nshopId = {SHOP}\n")
    check(rc == 0 and "OK connected" in so, "key first, shopId after", so)
    rc, so, se = yk(["connect", "--msg", "43"], f"{SHOP}:{KEY}")
    check(rc == 0 and "OK connected" in so and "could not delete" in so, "shop:key pair; no chat id says delete it", so)

    state["force"][("GET", "/v3/me")] = [(403, {"type": "error", "code": "forbidden", "description": "no"}, {})]
    n0 = len(calls("/v3/payments", "GET"))
    rc, so, se = yk(["connect", "--shop-id", SHOP], f"{KEY}\n")
    check(rc == 0 and "OK connected" in so and "Receipts 54-ФЗ: unknown" in so
          and calls("/v3/payments", "GET")[n0:] and calls("/v3/payments", "GET")[-1]["query"].get("limit") == "1",
          "no /me for this key: a one-payment listing proves it", so + se)
    rc, so, se = yk(["status"])
    check(rc == 0 and f"ЮKassa shop {SHOP}: connected, enabled; the key works" in so and "not set yet" in so
          and "Чеки от ЮKassa" in so, "status reads the settings again", so + se)

    # payments: totals, both pages, the period filter, refunds in the period
    before = len(calls("/v3/payments", "GET"))
    rc, so, se = yk(["payments"])
    pages = calls("/v3/payments", "GET")[before:]
    check(rc == 0 and "5 payments" in so and "Paid: 2 for 1 800 ₽, after the ЮKassa fee 1 744.50 ₽" in so
          and "Refunded in this period: 1 for 100 ₽" in so and "Waiting for the buyer 1" in so
          and "paid and waiting for your capture 1 for 2 000 ₽" in so, "payments totals", so + se)
    check(len(pages) == 2 and pages[1]["query"].get("cursor") == "c2" and "created_at.gte" in pages[1]["query"]
          and pages[0]["query"].get("limit") == "100", "cursor followed, filters kept")
    check("card Mir *4477" in so and "canceled: не хватило денег" in so and "| СБП |" in so, "payment lines", so)
    start = pages[0]["query"]["created_at.gte"]
    check(start.endswith(":00:00.000Z") and parse_ts(start).astimezone(MSK).hour == 0, "today starts 00:00 MSK", start)
    rc, so, se = yk(["payments", "--days", "7", "--status", "succeeded"])
    q = calls("/v3/payments", "GET")[-1]["query"]
    check(rc == 0 and q.get("status") == "succeeded" and "2 payments, status succeeded" in so, "status filter", so)
    rc, so, se = yk(["payments", "--days", "0"])
    check(rc == 2 and "ERROR bad_days" in so, "bad --days", so)
    rc, so, se = yk(["payments", "--json"])
    try:
        check(len(json.loads(so)["payments"]) == 5, "--json is valid JSON")
    except (ValueError, KeyError):
        check(False, "--json is valid JSON", so)

    # one payment: why it was canceled, in Russian; refunds of a payment
    rc, so, se = yk(["payment", ids[2]])
    check(rc == 0 and "Кто отменил: банк покупателя" in so and "не хватило денег" in so.lower()
          and "insufficient_funds" in so, "canceled payment explained", so + se)
    rc, so, se = yk(["payment", ids[5]])
    check(rc == 0 and "Refunded so far: 100 ₽ of 300 ₽" in so and "Refund 3f000000" in so, "refunds of a payment", so)
    rc, so, se = yk(["payment", ids[4]])
    check(rc == 0 and "capture" in so and "MSK" in so, "two-stage payment shows its deadline", so)
    rc, so, se = yk(["payment", "abc"])
    check(rc == 2 and "ERROR bad_id" in so, "bad payment id", so)
    rc, so, se = yk(["payment", ids[7]])
    check(rc == 2 and "ERROR not_found" in so, "404 explained", so)

    # link: a preview creates nothing; --yes creates it with a receipt and a request key
    n0 = len(posts())
    rc, so, se = yk(["link", "1500", "Заказ", "73", "--email", "buyer@example.com", "--vat", "1"])
    check(rc == 0 and "PREVIEW Payment link: 1 500 ₽, «Заказ 73»" in so and "без НДС" in so
          and len(posts()) == n0, "link preview creates nothing", so + se)
    rc, so, se = yk(["link", "1 500,00", "Заказ", "73", "--email", "buyer@example.com", "--vat", "1", "--yes",
                     "--return-url", "https://shop.example/thanks"])
    sent = calls("/v3/payments", "POST")[-1]
    b = json.loads(sent["body"])
    check(rc == 0 and f"orderId={ids[6]}" in so and b["capture"] is True and b["amount"] == {"value": "1500.00", "currency": "RUB"}
          and b["confirmation"] == {"type": "redirect", "return_url": "https://shop.example/thanks", "locale": "ru_RU"}
          and b["receipt"]["customer"] == {"email": "buyer@example.com"} and b["receipt"]["items"][0]["vat_code"] == 1,
          "link created", so + se)
    check("YK_RETURN_URL=https://shop.example/thanks" in open(path).read() and "YK_VAT=1" in open(path).read(),
          "return page and VAT remembered")
    rc, so, se = yk(["link", "990", "Заказ", "74"])
    check(rc == 2 and "ERROR receipt_needed" in so and "email" in so, "receipt contact required", so)
    rc, so, se = yk(["link", "990", "Заказ", "74", "--phone", "+7 900 000-00-00"])
    check(rc == 2 and "ERROR receipt_needed" in so and "only by email" in so, "Чеки от ЮKassa needs email", so)
    rc, so, se = yk(["link", "0", "x"])
    check(rc == 2 and "ERROR bad_amount" in so, "zero amount refused", so)
    rc, so, se = yk(["link", "990", "Заказ", "74", "--email", "b@example.com", "--days", "3", "--yes"])
    inv = json.loads(calls("/v3/invoices", "POST")[-1]["body"])
    exp = parse_ts(inv.get("expires_at"))
    check(rc == 0 and "https://yookassa.ru/my/i/in-0001/l" in so and inv["delivery_method_data"] == {"type": "self"}
          and inv["payment_data"]["capture"] is True and inv["cart"][0]["price"]["value"] == "990.00"
          and inv["payment_data"]["receipt"]["items"][0]["vat_code"] == 1
          and exp and abs((exp - now).total_seconds() - 3 * 86400) < 600, "invoice for 3 days", so + se)
    state["force"][("POST", "/v3/payments")] = [(400, {"type": "error", "id": "e-r", "code": "invalid_request",
                                                       "description": "Receipt is missing or illegal",
                                                       "parameter": "receipt"}, {})]
    rc, so, se = yk(["link", "100", "Тест", "--email", "b@example.com", "--yes"])
    check(rc == 2 and "ERROR receipt_needed" in so and "Nothing was created" in so, "API receipt error explained", so)

    # refund: preview, checks, partial with a receipt from the original, full without one
    n0 = len(calls("/v3/refunds", "POST"))
    rc, so, se = yk(["refund", ids[1], "500"])
    check(rc == 2 and "ERROR receipt_needed" in so and "email" in so, "partial refund needs the buyer's email", so)
    rc, so, se = yk(["refund", ids[1], "500", "--email", "buyer@example.com"])
    check(rc == 0 and "PREVIEW Refund 500 ₽" in so and "Already refunded: 0 ₽" in so and "1 000 ₽ of it stays" in so
          and "VAT code 1 (без НДС), service" in so, "refund preview with the original receipt's VAT", so + se)
    rc, so, se = yk(["refund", ids[1]])
    check(rc == 0 and "PREVIEW Refund 1 500 ₽" in so and "Receipt" not in so and "(a full refund)" in so,
          "full refund preview, no receipt", so)
    check(len(calls("/v3/refunds", "POST")) == n0, "refund previews change nothing")
    rc, so, se = yk(["refund", ids[1], "500", "--email", "buyer@example.com", "--yes"])
    b = json.loads(calls("/v3/refunds", "POST")[-1]["body"])
    it = b["receipt"]["items"][0]
    check(rc == 0 and "OK refund" in so and b["amount"] == {"value": "500.00", "currency": "RUB"}
          and it["vat_code"] == 1 and it["payment_subject"] == "service" and it["amount"]["value"] == "500.00"
          and it["description"] == "Заказ 72" and b["receipt"]["customer"] == {"email": "buyer@example.com"},
          "partial refund with the original receipt's VAT", so + se)
    rc, so, se = yk(["refund", ids[1], "--yes"])
    b = json.loads(calls("/v3/refunds", "POST")[-1]["body"])
    check(rc == 0 and b == {"payment_id": ids[1], "amount": {"value": "1500.00", "currency": "RUB"}},
          "full refund sends no receipt", so + se)
    rc, so, se = yk(["refund", ids[5], "250"])
    check(rc == 2 and "ERROR too_much" in so and "200 ₽" in so, "refund over what is left", so)
    rc, so, se = yk(["refund", ids[5], "199.50"])
    check(rc == 2 and "ERROR remainder" in so, "1 ₽ must stay after a partial refund", so)
    rc, so, se = yk(["refund", ids[2]])
    check(rc == 2 and "ERROR not_refundable" in so, "canceled payment cannot be refunded", so)

    # capture and cancel: only for waiting_for_capture, previews first
    n0 = len(posts())
    rc, so, se = yk(["capture", ids[4]])
    check(rc == 0 and "PREVIEW Capture 2 000 ₽" in so and len(posts()) == n0, "capture preview", so + se)
    rc, so, se = yk(["capture", ids[4], "--yes"])
    c = calls(f"/v3/payments/{ids[4]}/capture", "POST")
    check(rc == 0 and "OK payment" in so and "captured" in so and c and json.loads(c[-1]["body"]) == {},
          "capture", so + se)
    rc, so, se = yk(["cancel", ids[1]])
    check(rc == 2 and "ERROR wrong_status" in so and "refund" in so, "cancel a paid payment points to refund", so)
    rc, so, se = yk(["cancel", ids[4]])
    check(rc == 0 and "PREVIEW Cancel payment" in so, "cancel preview", so)
    rc, so, se = yk(["cancel", ids[4], "--yes"])
    check(rc == 0 and "canceled" in so and calls(f"/v3/payments/{ids[4]}/cancel", "POST"), "cancel", so + se)

    # errors: 403, 429 retried with the same key, 429 twice, 500 kept for a safe re-run, 401, network
    state["force"][("POST", "/v3/refunds")] = [(403, {"type": "error", "id": "e-403", "code": "forbidden",
                                                      "description": "Refunds are not allowed for this shop"}, {})]
    rc, so, se = yk(["refund", ids[5], "50", "--email", "b@example.com", "--yes"])
    check(rc == 2 and "ERROR forbidden" in so and "Refunds are not allowed" in so and "Nothing was refunded" in so,
          "403 explained", so)
    state["force"][("POST", "/v3/refunds")] = [(429, {"type": "error", "code": "too_many_requests",
                                                      "description": "Wow, so many requests!", "retry_after": 1000}, {})]
    n0 = len(calls("/v3/refunds", "POST"))
    rc, so, se = yk(["refund", ids[5], "60", "--email", "b@example.com", "--yes"])
    tries = calls("/v3/refunds", "POST")[n0:]
    check(rc == 0 and len(tries) == 2 and tries[0]["headers"].get("Idempotence-Key") == tries[1]["headers"].get("Idempotence-Key"),
          "429 retried once with the same request key", so + se)
    state["force"][("GET", "/v3/payments")] = [(429, {"code": "too_many_requests"}, {}), (429, {"code": "too_many_requests"}, {})]
    rc, so, se = yk(["payments"])
    check(rc == 2 and "ERROR rate_limited" in so, "429 twice is reported", so)
    state["force"][("POST", "/v3/refunds")] = [(500, {"type": "error", "id": "e-500", "code": "internal_server_error",
                                                      "description": "Internal server error"}, {})] * 2
    made = state["made"]
    n0 = len(calls("/v3/refunds", "POST"))
    rc, so, se = yk(["refund", ids[5], "70", "--email", "b@example.com", "--yes"])
    first = calls("/v3/refunds", "POST")[n0:]
    check(rc == 2 and "ERROR unknown_result" in so and "same command again" in so and os.path.exists(pending)
          and f"payment {ids[5]}" in so and len(first) == 2, "500 twice: result unknown, key kept", so)
    rc, so, se = yk(["refund", ids[5], "70", "--email", "b@example.com", "--yes"])
    again = calls("/v3/refunds", "POST")[-1]
    check(rc == 0 and again["headers"].get("Idempotence-Key") == first[0]["headers"].get("Idempotence-Key")
          and "Same request" in so and not os.path.exists(pending) and state["made"] == made + 1,
          "re-run reuses the key, then forgets it", so + se)
    keys = [x["headers"].get("Idempotence-Key") or "" for x in posts()]
    check(keys and all(re.fullmatch(r"[0-9a-zA-Z+\-_.]{1,64}", k) for k in keys),
          f"Idempotence-Key on every POST ({len(keys)})")
    state["force"][("GET", "/v3/payments")] = [(401, {"type": "error", "code": "invalid_credentials",
                                                      "description": "Authentication by given credentials failed"}, {})]
    rc, so, se = yk(["payments"])
    check(rc == 2 and "ERROR wrong_key" in so and "Ключи API" in so, "401 explained", so)
    dead = socket.socket()
    dead.bind(("127.0.0.1", 0))
    port = dead.getsockname()[1]
    dead.close()
    rc, so, se = yk(["payments"], extra={"OINOA_YOOKASSA_API": f"http://127.0.0.1:{port}/v3"})
    check(rc == 2 and "ERROR network" in so, "network error explained", so)

    # a test shop says so; forget
    rc, so, se = yk(["connect"], f"shopId {TSHOP}\n{TKEY}\n")
    check(rc == 0 and "TEST shop" in so and f"It replaces shop {SHOP}" in so and "YK_RETURN_URL" not in open(path).read(),
          "test shop connected, old defaults dropped", so + se)
    rc, so, se = yk(["link", "10", "Проверка", "--email", "t@example.com", "--vat", "1"])
    check(rc == 0 and "TEST shop: no real money moves" in so, "test shop marked on a preview", so)
    rc, so, se = yk(["forget"])
    check(rc == 0 and "OK forgot" in so and "Ключи API" in so and "site" in so and not os.path.exists(path), "forget", so)
    rc, so, se = yk(["payments"])
    check(rc == 2 and "ERROR not_connected" in so, "not connected after forget", so)

    leaks = [s for s in secrets if any(s in text for text in seen)]
    check(not leaks, "no key, auth header or bot token in any stdout or stderr", f"{len(leaks)} leaked")
    srv.shutdown()
    bad = [r for r in results if not r[0]]
    for ok, label, detail in results:
        print(f"{'ok  ' if ok else 'FAIL'} {label}" + ("" if ok else f"\n     {clip(detail, 900)}"))
    print(f"SELFTEST {'OK' if not bad else 'FAILED'}: {len(results) - len(bad)}/{len(results)} checks passed")
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
    if any(KEY_RE.search(x) for x in sys.argv[1:]):
        fail("bad_args", "a secret key is on the command line (not shown). Keys go on stdin in the <<'EOF' block; "
             "since it may now be in a shell history, ask them to reissue it only if nothing else uses it.")
    p = Parser(prog="yk.py", description="The owner's own ЮKassa shop.")
    sub = p.add_subparsers(dest="cmd", required=True, parser_class=Parser)
    c = sub.add_parser("connect")
    c.add_argument("--chat", help="chat_id of the message with the key")
    c.add_argument("--msg", action="append", help="message_id(s) of the message(s) with the key")
    c.add_argument("--shop-id", help="the shopId, if it came separately (not secret)")
    st = sub.add_parser("status")
    st.add_argument("--json", action="store_true")
    sub.add_parser("forget")
    ps = sub.add_parser("payments")
    ps.add_argument("--days", type=int, default=1, help="1 = today since 00:00 MSK, 7 = this and the last 6 days")
    ps.add_argument("--status", choices=["succeeded", "pending", "waiting_for_capture", "canceled"])
    ps.add_argument("--limit", type=int, default=20, help="payment lines to print (totals cover all)")
    ps.add_argument("--json", action="store_true")
    pm = sub.add_parser("payment")
    pm.add_argument("id")
    pm.add_argument("--json", action="store_true")

    def receipt_args(x):
        x.add_argument("--email", help="the buyer's email for the 54-ФЗ receipt")
        x.add_argument("--phone", help="the buyer's phone for the receipt (third-party online registers only)")
        x.add_argument("--vat", type=int, choices=sorted(VAT), help="VAT code for the receipt")
        x.add_argument("--subject", choices=SUBJECTS, help="payment_subject for the receipt (default commodity)")
        x.add_argument("--mode", choices=MODES, help="payment_mode for the receipt (default full_payment)")
        x.add_argument("--tax", type=int, choices=range(1, 7), help="tax_system_code, some online registers need it")
        x.add_argument("--yes", action="store_true", help="do it; without this only a preview")
        x.add_argument("--json", action="store_true")

    lk = sub.add_parser("link")
    lk.add_argument("amount")
    lk.add_argument("description", nargs="+")
    lk.add_argument("--days", type=int, help="an invoice that waits N days (1-30) instead of a 1-hour payment link")
    lk.add_argument("--return-url", help="where the buyer lands after paying (remembered)")
    receipt_args(lk)
    rf = sub.add_parser("refund")
    rf.add_argument("id")
    rf.add_argument("amount", nargs="?")
    rf.add_argument("--reason", help="comment for the refund, up to 250 characters")
    receipt_args(rf)
    cp = sub.add_parser("capture")
    cp.add_argument("id")
    cp.add_argument("amount", nargs="?")
    receipt_args(cp)
    cn = sub.add_parser("cancel")
    cn.add_argument("id")
    cn.add_argument("--yes", action="store_true")
    cn.add_argument("--json", action="store_true")
    a, extra = p.parse_known_args()
    if extra:
        fail("bad_args", "unexpected words on the command line (not shown). Keys go on stdin in the <<'EOF' block, "
             "never as arguments.")
    if getattr(a, "limit", 1) < 1:
        fail("bad_limit", "--limit is 1 or more.")
    socket.setdefaulttimeout(TIMEOUT)
    {"connect": cmd_connect, "status": cmd_status, "forget": cmd_forget, "payments": cmd_payments,
     "payment": cmd_payment, "link": cmd_link, "refund": cmd_refund, "capture": cmd_capture,
     "cancel": cmd_cancel}[a.cmd](a)


if __name__ == "__main__":
    main()
