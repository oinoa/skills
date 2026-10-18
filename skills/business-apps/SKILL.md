---
name: business-apps
description: Use when the person asks about their own data in a connected business app - CRM deals, leads, contacts and tasks (Bitrix24, amoCRM/Kommo, HubSpot, Pipedrive), stock, products and customer orders (MoySklad), calendar events (Yandex Calendar), Notion pages and databases, Asana tasks, Calendly bookings, or invoices and customers in lexoffice, sevDesk or Holded - or wants a record added or changed there.
---

# Business apps

The person connects apps in the OINOA app: Settings → Tools (Настройки → Инструменты).
Each saved key is an environment variable in every shell command you run. Call the apps over
plain HTTPS with `curl` or `python3` (standard library only). Install nothing.

## First

1. Check the app's variables (every one it lists) without showing them:
   `[ -n "$AMOCRM_DOMAIN" ] && [ -n "$AMOCRM_TOKEN" ] && echo set || echo missing`
2. Missing: tell the person to connect the app in the OINOA app, Settings → Tools
   (Настройки → Инструменты), then stop. Never ask for a key, token or password in the chat. If
   they paste one, do not use or repeat it: ask them to delete that message and save the key in
   Tools.
3. Never print, echo, log or repeat a key or a webhook URL, and never write one to a file. Use it
   only as `"$VAR"` inside a command. No `env`, `printenv`, `set -x` or `curl -v`.
4. 401/403 on every call: the key is wrong, expired or lacks a permission; ask them to check it
   in Tools. 404 on every call: the domain or URL saved there is wrong.

## Rules

- Read before you write. Find the exact record and show it to the person first.
- Create, change, cancel or delete only after a clear yes to exactly that action: which record,
  which fields, which text. One yes covers one action. Say what was done afterwards.
- Answer in the person's language with what matters: names, amounts, stages, dates, who is
  responsible. No raw JSON; IDs only if they ask. Times in their zone (`$TZ`, else the server's zone).
- Request only the fields you need and only as many records as you need. For long lists, page on
  (each app says how), stop when you have enough, and say how many there are in total.
- 429 or a "limit" error: wait (honour `Retry-After`), retry once or twice, slower. In loops,
  `sleep 0.5` between calls. Do not run calls to one app in parallel.
- Each app has a setup line (`B=...`). Shell state does not carry over, so put the setup line and
  the calls in one command.
- Read JSON with a short filter: `| python3 -c 'import json,sys; d=json.load(sys.stdin); ...'`.
  When a body contains the person's text, build it with `json.dumps` so quotes are escaped.

## Bitrix24

`BITRIX24_WEBHOOK_URL` is an incoming webhook, `https://<portal>/rest/<user id>/<secret>/`; the
secret is in the URL, so treat the whole URL as a key. POST JSON to `<url><method>`. Lists return
50 at a time with `total` and `next`: send `"start": <next>` until `next` is gone.

```bash
B="${BITRIX24_WEBHOOK_URL%/}"; J=(-H 'Content-Type: application/json')
curl -sS -X POST "$B/profile" "${J[@]}" -d '{}'      # me: result.ID
# open deals; stage names: crm.status.list {"filter":{"ENTITY_ID":"DEAL_STAGE"}} (other pipelines DEAL_STAGE_<CATEGORY_ID>)
curl -sS -X POST "$B/crm.deal.list" "${J[@]}" -d '{"filter":{"CLOSED":"N"},"select":["ID","TITLE","STAGE_ID","OPPORTUNITY","CURRENCY_ID","ASSIGNED_BY_ID","CLOSEDATE"],"order":{"ID":"DESC"},"start":0}'
# leads in work (STATUS_SEMANTIC_ID: P in work, S converted, F junk)
curl -sS -X POST "$B/crm.lead.list" "${J[@]}" -d '{"filter":{"STATUS_SEMANTIC_ID":"P"},"select":["ID","TITLE","NAME","LAST_NAME","STATUS_ID","PHONE","EMAIL"]}'
# contact by phone or email -> result.CONTACT = [ids]; by name: crm.contact.list
curl -sS -X POST "$B/crm.duplicate.findbycomm" "${J[@]}" -d '{"type":"PHONE","values":["+79991234567"],"entity_type":"CONTACT"}'
curl -sS -X POST "$B/crm.contact.list" "${J[@]}" -d '{"filter":{"%LAST_NAME":"Иванов"},"select":["ID","NAME","LAST_NAME","PHONE","EMAIL"]}'
# open tasks of a user (REAL_STATUS 5 = done); answer is result.tasks[] with camelCase keys
curl -sS -X POST "$B/tasks.task.list" "${J[@]}" -d '{"filter":{"RESPONSIBLE_ID":1,"!REAL_STATUS":5},"select":["ID","TITLE","STATUS","DEADLINE"],"order":{"DEADLINE":"asc"}}'
# a user's calendar; read times from DATE_FROM_TS_UTC (DATE_FROM is in the portal's format)
curl -sS -X POST "$B/calendar.event.get" "${J[@]}" -d '{"type":"user","ownerId":1,"from":"2026-10-05","to":"2026-10-12"}'
# writes: comment on a deal; task linked to deal 10
curl -sS -X POST "$B/crm.timeline.comment.add" "${J[@]}" -d '{"fields":{"ENTITY_ID":10,"ENTITY_TYPE":"deal","COMMENT":"Текст"}}'
curl -sS -X POST "$B/tasks.task.add" "${J[@]}" -d '{"fields":{"TITLE":"Позвонить клиенту","RESPONSIBLE_ID":1,"DEADLINE":"2026-10-10T18:00:00+03:00","UF_CRM_TASK":["D_10"]}}'
```
- About 2 requests/s; `QUERY_LIMIT_EXCEEDED` (503): wait a second. `insufficient_scope`: the
  webhook lacks crm, task or calendar access; the person adds it in the webhook's settings.
- `ACCESS_DENIED` "REST is available only by subscription": their Bitrix24 plan has no API access.
- PHONE and EMAIL filters in `*.list` match exactly; use `crm.duplicate.findbycomm` for phones.

## amoCRM / Kommo

`AMOCRM_DOMAIN` (`company.amocrm.ru` or `company.kommo.com`) and `AMOCRM_TOKEN` (long-lived
token). Same API v4 for both. Answers are `_embedded.<items>`; pages: `page`, `limit` (max 250),
follow `_links.next.href`. An empty result is 204 with no body, not an error.

```bash
A="https://${AMOCRM_DOMAIN#https://}"; A="${A%/}/api/v4"; T=(-H "Authorization: Bearer $AMOCRM_TOKEN")
curl -sS "$A/account" "${T[@]}"               # current_user_id
curl -sS "$A/leads/pipelines" "${T[@]}"       # pipelines with _embedded.statuses (142 won, 143 lost)
curl -sS -G "$A/leads" "${T[@]}" --data-urlencode 'with=contacts' -d limit=250 -d page=1 \
  --data-urlencode 'order[updated_at]=desc' --data-urlencode 'filter[statuses][0][pipeline_id]=3177727' \
  --data-urlencode 'filter[statuses][0][status_id]=32392156'
curl -sS -G "$A/contacts" "${T[@]}" --data-urlencode 'query=+79991234567' -d limit=50
curl -sS -G "$A/tasks" "${T[@]}" --data-urlencode 'filter[is_completed]=0' \
  --data-urlencode 'filter[responsible_user_id]=123' --data-urlencode 'order[complete_till]=asc'
# writes take a JSON array: note on a lead; task on a lead (complete_till = unix time)
curl -sS -X POST "$A/leads/12345/notes" "${T[@]}" -H 'Content-Type: application/json' \
  -d '[{"note_type":"common","params":{"text":"Текст примечания"}}]'
curl -sS -X POST "$A/tasks" "${T[@]}" -H 'Content-Type: application/json' \
  -d '[{"text":"Перезвонить","complete_till":'"$(date -d '+1 day 12:00' +%s)"',"entity_id":12345,"entity_type":"leads"}]'
```
- Open leads: everything not in status 142 or 143; filter by open statuses, or drop those two.
- 7 requests/s. 402: the account is unpaid (writes stop at once, reads 30 days later).

## MoySklad

`MOYSKLAD_TOKEN`. Every request needs `Accept-Encoding: gzip` (`curl --compressed`), otherwise
415. Pages: `limit` (max 1000), `offset`; total in `meta.size`. Prices are in kopecks (divide by 100).

```bash
M=https://api.moysklad.ru/api/remap/1.2; C=(--compressed -H "Authorization: Bearer $MOYSKLAD_TOKEN")
# stock; search and stockMode go inside filter (positiveOnly = in stock)
curl -sS "${C[@]}" -G "$M/report/stock/all" --data-urlencode 'filter=search=кофе;stockMode=positiveOnly' -d limit=100
#   rows[]: name, code, article, stock, reserve, inTransit, quantity, salePrice
# latest customer orders with customer and status (expand needs limit <= 100)
curl -sS "${C[@]}" -G "$M/entity/customerorder" --data-urlencode 'order=moment,desc' -d limit=50 \
  --data-urlencode 'expand=agent,state'
# find goods and services by name, code or article
curl -sS "${C[@]}" -G "$M/entity/assortment" --data-urlencode 'search=кофе' -d limit=20
```
- Read-only here: orders and documents are created in MoySklad itself.
- About 15 requests per 3 seconds per user (the stock report counts as more), at most 5 at once.
  429: wait as `X-Lognex-Retry-After` says. Dates in filters are Moscow time, `YYYY-MM-DD HH:MM`.

## Yandex Calendar (CalDAV)

`YANDEX_LOGIN` (full address: `name@yandex.ru`, or `name@company.ru` on Yandex 360; a bare login
gets `@yandex.ru`) and `YANDEX_CALDAV_PASSWORD` (an app password of the Calendar type; a new one
works only 2-3 hours after it is created). Server `https://caldav.yandex.ru`, Basic auth.
Save this helper once (it holds no secret), then use it:

```bash
[ -f ~/.cache/yacal.py ] || { mkdir -p ~/.cache && cat > ~/.cache/yacal.py <<'PY'
import os, sys, base64, uuid, datetime as dt, urllib.request, urllib.error, xml.etree.ElementTree as ET
from urllib.parse import quote
from zoneinfo import ZoneInfo
B = 'https://caldav.yandex.ru'
L = os.environ['YANDEX_LOGIN']; L = L if '@' in L else L + '@yandex.ru'
AUTH = 'Basic ' + base64.b64encode(f"{L}:{os.environ['YANDEX_CALDAV_PASSWORD']}".encode()).decode()
def zone(z):
    try: return ZoneInfo(z)
    except Exception: return None
TZ = (zone(os.environ.get('TZ', '').lstrip(':') or os.path.realpath('/etc/localtime').split('zoneinfo/')[-1])
      or dt.datetime.now().astimezone().tzinfo)
NS = {'d': 'DAV:', 'c': 'urn:ietf:params:xml:ns:caldav'}

def req(method, path, body='', depth='1', ctype='application/xml; charset=utf-8', extra={}):
    r = urllib.request.Request(B + path, body.encode() or None,
        {'Authorization': AUTH, 'Depth': depth, 'Content-Type': ctype, **extra}, method=method)
    with urllib.request.urlopen(r, timeout=90) as f: return f.read().decode()

def calendars():
    x = ET.fromstring(req('PROPFIND', f'/calendars/{quote(L)}/',
        '<d:propfind xmlns:d="DAV:"><d:prop><d:displayname/><d:resourcetype/></d:prop></d:propfind>'))
    for r in x.findall('d:response', NS):
        if r.find('.//d:resourcetype/c:calendar', NS) is not None:
            yield r.findtext('d:href', '', NS), r.findtext('.//d:displayname', '', NS)

def utc(d): return d.astimezone(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')

def when(line):  # DTSTART;TZID=Europe/Moscow:20261005T100000 | :20261005T070000Z | ;VALUE=DATE:20261005
    head, v = line.split(':', 1)
    if 'T' not in v: return f'{v[:4]}-{v[4:6]}-{v[6:8]}'
    t = dt.datetime.strptime(v[:15], '%Y%m%dT%H%M%S')
    z = dt.timezone.utc if v.endswith('Z') else zone(head.split('TZID=')[-1].split(';')[0]) if 'TZID=' in head else None
    return t.replace(tzinfo=z or TZ).astimezone(TZ).strftime('%Y-%m-%d %H:%M')

def events(start, end):  # the person's local dates YYYY-MM-DD, end exclusive
    s, e = (dt.datetime.fromisoformat(d).replace(tzinfo=TZ) for d in (start, end))
    q = ('<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"><d:prop><c:calendar-data/>'
         '</d:prop><c:filter><c:comp-filter name="VCALENDAR"><c:comp-filter name="VEVENT">'
         f'<c:time-range start="{utc(s)}" end="{utc(e)}"/></c:comp-filter></c:comp-filter></c:filter></c:calendar-query>')
    rows = []
    for href, name in calendars():
        try: x = ET.fromstring(req('REPORT', href, q))
        except urllib.error.HTTPError as err: print('ERROR', err.code, 'in', name); continue
        for r in x.findall('d:response', NS):
            ev = r.findtext('.//c:calendar-data', '', NS).replace('\r\n ', '').replace('\n ', '')
            ev = ev.split('BEGIN:VEVENT', 1)[-1].splitlines()
            get = lambda k: next((l for l in ev if l.split(':')[0].split(';')[0] == k), '')
            if get('DTSTART'):
                rows.append((when(get('DTSTART')), when(get('DTEND')) if get('DTEND') else '',
                    get('SUMMARY').split(':', 1)[-1].replace('\\', ''), name, 'repeats' if get('RRULE') else ''))
    for row in sorted(rows): print(' | '.join(row))

def add(cal_href, start, minutes, title):  # start 'YYYY-MM-DD HH:MM' in the person's zone
    s = dt.datetime.fromisoformat(start).replace(tzinfo=TZ); uid = str(uuid.uuid4())
    title = ' '.join(title.split()).replace('\\', '').replace(',', '\\,').replace(';', '\\;')
    ics = '\r\n'.join(['BEGIN:VCALENDAR', 'VERSION:2.0', 'PRODID:-//OINOA//agent//EN', 'BEGIN:VEVENT',
        f'UID:{uid}', f'DTSTAMP:{utc(dt.datetime.now())}', f'DTSTART:{utc(s)}',
        f'DTEND:{utc(s + dt.timedelta(minutes=int(minutes)))}', f'SUMMARY:{title}', 'END:VEVENT', 'END:VCALENDAR', ''])
    url = cal_href.rstrip('/') + f'/{uid}.ics'
    req('PUT', url, ics, '0', 'text/calendar; charset=utf-8', {'If-None-Match': '*'})
    req('GET', url, depth='0')  # read-only calendars accept a PUT and keep nothing: this raises 404 then
    print('created', url)

cmd, *a = sys.argv[1:] or ['calendars']
try:
    if cmd == 'calendars': [print(h, '|', n) for h, n in calendars()]
    elif cmd == 'events': events(*a)
    elif cmd == 'add': add(*a)
except urllib.error.HTTPError as e: sys.exit(f'ERROR {e.code} {e.reason}')
PY
}
python3 ~/.cache/yacal.py calendars                       # HREF | name
python3 ~/.cache/yacal.py events 2026-10-05 2026-10-12    # local dates, end not included
python3 ~/.cache/yacal.py add HREF "2026-10-05 15:00" 60 "Звонок с Петровым"   # start, minutes, title
```
- `add` goes to the main calendar (`events-default`) unless the person names another; holiday
  and birthday calendars are read-only.
- A repeating event shows once, at the first date of the series, marked `repeats`: say so.
- The server is slow and can answer 504: wait a few seconds and retry once. After a failed
  `add`, run `events` for that day before trying again, so you do not create it twice.
- 401: wrong login or password, or the app password is less than 2-3 hours old.

## HubSpot

`HUBSPOT_TOKEN` (service key or private-app token, `pat-...`). Paths use API version 2026-09;
`/crm/v3/objects/...` is the same API. Search: `limit` max 200, next page with
`"after": paging.next.after`, at most 5 searches per second; new records appear in search after a
few seconds. All values come back as strings; `dealstage` is an ID, labels come from pipelines.

```bash
H=https://api.hubapi.com; T=(-H "Authorization: Bearer $HUBSPOT_TOKEN" -H 'Content-Type: application/json')
curl -sS "${T[@]}" "$H/crm/pipelines/2026-09/deals"   # stages[].id, label, metadata.isClosed
curl -sS "${T[@]}" -X POST "$H/crm/objects/2026-09/deals/search" -d '{"filterGroups":[{"filters":[
  {"propertyName":"hs_is_closed","operator":"EQ","value":"false"}]}],"properties":["dealname","amount",
  "dealstage","closedate","hubspot_owner_id"],"sorts":[{"propertyName":"closedate","direction":"ASCENDING"}],"limit":100}'
curl -sS "${T[@]}" -X POST "$H/crm/objects/2026-09/contacts/search" -d '{"query":"anna@acme.com",
  "properties":["firstname","lastname","email","phone","company"],"limit":10}'
# note on deal 123 (association 214; on a contact 202)
curl -sS "${T[@]}" -X POST "$H/crm/objects/2026-09/notes" -d '{"properties":{"hs_timestamp":"2026-10-05T09:00:00Z",
  "hs_note_body":"Called, sent the offer."},"associations":[{"to":{"id":"123"},"types":[
  {"associationCategory":"HUBSPOT_DEFINED","associationTypeId":214}]}]}'
# task on deal 123 (216; on a contact 204); hs_timestamp is the due time
curl -sS "${T[@]}" -X POST "$H/crm/objects/2026-09/tasks" -d '{"properties":{"hs_task_subject":"Follow up",
  "hs_task_status":"NOT_STARTED","hs_task_priority":"MEDIUM","hs_task_type":"TODO","hs_timestamp":"2026-10-07T09:00:00Z"},
  "associations":[{"to":{"id":"123"},"types":[{"associationCategory":"HUBSPOT_DEFINED","associationTypeId":216}]}]}'
```
- If the `hs_is_closed` filter fails, take the stage IDs with `metadata.isClosed` "true" from
  pipelines and filter `{"propertyName":"dealstage","operator":"NOT_IN","values":[...]}`.
- 403 with a scope message: the key needs `crm.objects.deals.read/write` or
  `crm.objects.contacts.read/write`. 400 on a write may be an account validation rule; read it.

## Pipedrive

`PIPEDRIVE_TOKEN` (personal API token). Use API
v2 (the old v1 list calls are out of support); notes are still v1. Pages: `limit` (max 500),
`&cursor=<additional_data.next_cursor>` until it is null. Times are UTC.

```bash
P=https://api.pipedrive.com; T=(-H "x-api-token: $PIPEDRIVE_TOKEN")
curl -sS "$P/v1/users/me" "${T[@]}"                 # data.id
curl -sS "$P/api/v2/deals?status=open&limit=100&sort_by=update_time&sort_direction=desc" "${T[@]}"
curl -sS "$P/api/v2/stages?limit=500" "${T[@]}"    # stage_id -> name
curl -sS -G "$P/api/v2/persons/search" "${T[@]}" --data-urlencode 'term=Anna Smith' -d fields=name,email,phone -d limit=20
# open activities of a user; no date-range filter: pick by due_date / due_time yourself
curl -sS "$P/api/v2/activities?done=false&owner_id=123&sort_by=due_date&sort_direction=asc&limit=100" "${T[@]}"
# note on deal 123 (or person_id, org_id, lead_id); content is HTML
curl -sS -X POST "$P/v1/notes" "${T[@]}" -H 'Content-Type: application/json' -d '{"content":"Called, sent the offer.","deal_id":123}'
```
- Each call spends a daily token budget; search is limited to 10 calls per 2 seconds.

## Notion

`NOTION_TOKEN` (`ntn_...` or `secret_...`). An internal connection sees only pages shared with it:
when search finds nothing or a page gives 404, ask the person to open the page in Notion, `•••` →
Connections → add the connection they made for OINOA (sub-pages follow). Pages: `page_size` (max 100),
`start_cursor` = `next_cursor` while `has_more`. About 3 requests/s.

```bash
N=https://api.notion.com/v1; T=(-H "Authorization: Bearer $NOTION_TOKEN" -H 'Notion-Version: 2026-03-11' -H 'Content-Type: application/json')
# search titles; filter value "page" or "data_source" (a database's table)
curl -sS -X POST "$N/search" "${T[@]}" -d '{"query":"Встречи","filter":{"property":"object","value":"page"},"page_size":20}'
# page content, first level (blocks with has_children=true need their own call)
curl -sS "$N/blocks/$PAGE_ID/children?page_size=100" "${T[@]}"
# database: get its data source, see property names/types, query it
curl -sS "$N/databases/$DB_ID" "${T[@]}"                  # data_sources[].id
curl -sS "$N/data_sources/$DS_ID" "${T[@]}"               # properties
curl -sS -X POST "$N/data_sources/$DS_ID/query" "${T[@]}" -d '{"page_size":50,"filter":{"property":"Status","status":{"does_not_equal":"Done"}}}'
# append a paragraph at the end of a page
curl -sS -X PATCH "$N/blocks/$PAGE_ID/children" "${T[@]}" -d '{"children":[{"object":"block","type":"paragraph",
  "paragraph":{"rich_text":[{"type":"text","text":{"content":"Текст"}}]}}]}'
```
- An ID in a database's URL is a database ID: get its data source first. Search returns
  data-source IDs. At most 100 blocks per append and 2000 characters per text item.

## Asana

`ASANA_TOKEN` (personal access token). Bodies go in `{"data":{...}}`. Always pass `limit=100` and
`opt_fields`; next page with `offset=<next_page.offset>`. 150 requests/min (paid: 1500).

```bash
S=https://app.asana.com/api/1.0; T=(-H "Authorization: Bearer $ASANA_TOKEN")
curl -sS "$S/users/me?opt_fields=name,workspaces.name" "${T[@]}"              # workspace gids
# my open tasks: My Tasks list of a workspace, then its incomplete tasks
curl -sS "$S/users/me/user_task_list?workspace=$WS" "${T[@]}"                 # data.gid
curl -sS "$S/user_task_lists/$UTL/tasks?completed_since=now&limit=100&opt_fields=name,due_on,projects.name,permalink_url" "${T[@]}"
curl -sS "$S/projects?workspace=$WS&archived=false&limit=100&opt_fields=name" "${T[@]}"
curl -sS "$S/projects/$PRJ/tasks?completed_since=now&limit=100&opt_fields=name,due_on,assignee.name" "${T[@]}"
# create a task (needs projects or workspace); comment on a task
curl -sS -X POST "$S/tasks" "${T[@]}" -H 'Content-Type: application/json' \
  -d '{"data":{"name":"Позвонить Ивану","projects":["'"$PRJ"'"],"assignee":"me","due_on":"2026-10-06"}}'
curl -sS -X POST "$S/tasks/$TASK/stories" "${T[@]}" -H 'Content-Type: application/json' -d '{"data":{"text":"Готово"}}'
```

## Calendly

`CALENDLY_TOKEN` (personal access token). Query parameters take full URIs; paths take the UUID,
the last part of a URI. Times are UTC. Next page: `pagination.next_page` (a full URL).

```bash
C=https://api.calendly.com; T=(-H "Authorization: Bearer $CALENDLY_TOKEN")
ME=$(curl -sS "$C/users/me" "${T[@]}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["resource"]["uri"])')
curl -sS -G "$C/scheduled_events" "${T[@]}" --data-urlencode "user=$ME" -d status=active \
  -d sort=start_time:asc -d count=100 --data-urlencode "min_start_time=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
curl -sS "$C/scheduled_events/$UUID/invitees?count=100" "${T[@]}"     # name, email, questions_and_answers
# cancel: only after a clear yes, with the reason the person approved
curl -sS -X POST "$C/scheduled_events/$UUID/cancellation" "${T[@]}" -H 'Content-Type: application/json' -d '{"reason":"..."}'
```
- 403 or empty results: the token was created without the scopes `users:read`,
  `scheduled_events:read`, `scheduled_events:write`; the person makes a new one with them.

## lexoffice (Lexware Office)

`LEXOFFICE_API_KEY`. Base `https://api.lexware.io/v1` (the old `api.lexoffice.io` host is being
retired). At most 2 requests per second: `sleep 0.6` between calls. Pages: `page` (from 0), `size`
(max 250). 402: the plan has no API access.

```bash
L=https://api.lexware.io/v1; T=(-H "Authorization: Bearer $LEXOFFICE_API_KEY" -H 'Accept: application/json')
# open invoices (voucherType and voucherStatus are both required; "overdue" cannot be combined)
curl -sS "$L/voucherlist?voucherType=invoice&voucherStatus=open&sort=voucherDate,DESC&size=100" "${T[@]}"
#   content[]: voucherNumber, contactName, voucherDate, dueDate, totalAmount, openAmount, currency
curl -sS -G "$L/contacts" "${T[@]}" --data-urlencode 'name=Muster' -d customer=true   # name: 3+ letters
curl -sS "$L/invoices/$ID" "${T[@]}"
# invoice draft (no ?finalize=true: it stays a draft for the person to check and send)
curl -sS -X POST "$L/invoices" "${T[@]}" -H 'Content-Type: application/json' -d '{
 "voucherDate":"2026-10-05T00:00:00.000+02:00","address":{"contactId":"<contact id>"},
 "lineItems":[{"type":"custom","name":"Beratung","quantity":2,"unitName":"Stunde",
   "unitPrice":{"currency":"EUR","netAmount":100,"taxRatePercentage":19}}],
 "totalPrice":{"currency":"EUR"},"taxConditions":{"taxType":"net"},
 "shippingConditions":{"shippingDate":"2026-10-05T00:00:00.000+02:00","shippingType":"service"}}'
```

## sevDesk

`SEVDESK_API_TOKEN`. Header `Authorization: <token>`, raw, without "Bearer". Lists come as
`{"objects":[...]}`; pages: `limit` (1-1000), `offset`; `countAll=true` adds `total`.
Invoice status: 100 draft, 200 open, 750 partly paid, 1000 paid.

```bash
D=https://my.sevdesk.de/api/v1; T=(-H "Authorization: $SEVDESK_API_TOKEN" -H 'User-Agent: oinoa-agent' -H 'Accept: application/json')
curl -sS "$D/Invoice?status=200&embed=contact&limit=100&countAll=true" "${T[@]}"   # open
curl -sS "$D/Invoice?delinquent=true&embed=contact&limit=100" "${T[@]}"           # overdue
curl -sS "$D/Invoice/$ID" "${T[@]}"
curl -sS -G "$D/Contact" "${T[@]}" --data-urlencode 'name=Muster' -d depth=1      # depth=1 adds people
```
- No due-date field: due = `invoiceDate` + `timeToPay` days. Email and phone are not on the
  contact: `GET $D/CommunicationWay?contact[id]=<id>&contact[objectName]=Contact` (use `-g`).
- Read-only here; invoices are created in sevDesk itself.

## Holded

`HOLDED_API_KEY`. Holded has two kinds of key: the current API token (v2, Bearer) and an older
v1 key (`key` header). Check which one it is:
`curl -sS -o /dev/null -w '%{http_code}\n' 'https://api.holded.com/api/v2/invoices?limit=1' -H "Authorization: Bearer $HOLDED_API_KEY"`
200: v2. Otherwise try the v1 invoice call below; if it gives 401 too, the key is wrong or is
a v2 token without the invoice permission.

```bash
# v2: pages with &cursor=<cursor> while has_more; amounts are strings
V=https://api.holded.com/api/v2; T=(-H "Authorization: Bearer $HOLDED_API_KEY" -H 'Accept: application/json')
curl -sS "$V/invoices?status=outstanding&sort=-date&limit=100" "${T[@]}"   # unpaid; status=overdue: past due
curl -sS "$V/invoices/$ID" "${T[@]}"
curl -sS -G "$V/contacts/search" "${T[@]}" --data-urlencode 'name=Acme'
# invoice draft (contact_id from contacts)
curl -sS -X POST "$V/invoices" "${T[@]}" -H 'Content-Type: application/json' -d '{"contact_id":"<id>",
  "date":"2026-10-05","due_date":"2026-11-04","items":[{"name":"Consultoría","type":"service","units":1,"price":100,"taxes":["s_iva_21"]}]}'
# v1: paid=0 unpaid, 1 paid, 2 partly; starttmp/endtmp are unix times
O=https://api.holded.com/api/invoicing/v1; K=(-H "key: $HOLDED_API_KEY" -H 'Accept: application/json')
curl -sS "$O/documents/invoice?paid=0&sort=created-desc" "${K[@]}"
curl -sS "$O/contacts" "${K[@]}"            # whole list; phone= and mobile= match exactly
```
- v2 403 with a permission message: the token lacks the scope (`sales:invoices.read`,
  `contacts:contacts.read`); the person edits it in Holded. 429: wait for `Retry-After`.
