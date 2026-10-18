---
name: 1c
description: >-
  Connect the owner's own 1С:Предприятие (their server published to the web, or 1С:Фреш in the
  cloud) right here in the chat, then find goods and counterparties, check stock, see sales for a
  period and who owes whom, and make a draft customer invoice. Use for "connect 1C", "подключи 1С",
  "сколько продали в 1С за месяц", "выручка за неделю", "остатки товаров в 1С", "что заканчивается
  на складе", "кто нам должен", "кому мы должны", "дебиторка", "найди контрагента", "найди товар в
  1С", "выстави счёт в 1С", "сделай счёт Ромашке", "отключи 1С". Invoices are created only after the
  owner's clear yes, as drafts; nothing is ever posted.
compatibility: "python3. 1С:Бухгалтерия предприятия 3.0, Управление нашей фирмой or Управление торговлей 11 (other configurations partly), published with the standard OData interface; any application in 1С:Фреш."
metadata:
  author: OINOA
  version: "1.0"
---

# 1С

One tool: `python3 ~/.claude/skills/1c/onec.py`. It answers in a few short lines. A line that
starts with `ERROR` says what to tell the person. Say it in your own words. It reads 1С through the
standard OData interface that 1С itself publishes; it installs nothing in their 1С.

## Connect, in this chat

1. Ask where their 1С lives: in 1С:Фреш (the address starts with 1cfresh.com or a partner's
   cloud) or on their own or their administrator's server. Send the steps for that case in one
   message, in Russian unless they write in another language, with the list for their program
   (below). Add one line: it is a separate user only for the assistant, not their own login, and
   they can switch it off at any time.

   1С:Фреш (the owner does it; the account owner has the rights it needs):

   > Откройте программу в 1С:Фреш: Администрирование → Синхронизация данных → Настройки стандартного интерфейса OData (в некоторых программах вместо «Администрирование» пункт «Администрирование и НСИ» или «Настройки»).
   > Вкладка «Авторизация»: создайте отдельное имя пользователя и пароль.
   > Вкладка «Состав»: отметьте объекты из списка ниже и нажмите «Записать и закрыть».
   > Пришлите мне сюда адрес программы из строки браузера (например https://1cfresh.com/a/ea/1234567), имя пользователя и пароль.

   Their own server (the 1С administrator does it; the owner forwards this):

   > 1. Опубликуйте базу на веб-сервере по https: Конфигуратор → Администрирование → Публикация на веб-сервере, отметьте «Публиковать стандартный интерфейс OData». Адрес должен открываться из интернета: помощник работает на своём сервере, не в вашей сети.
   > 2. В самой программе: Администрирование → Синхронизация данных → Настройки стандартного интерфейса OData. Вкладка «Авторизация»: отдельный пользователь с паролем. Вкладка «Состав»: объекты из списка ниже.
   > 3. Если такой настройки в программе нет: состав задаётся методом УстановитьСоставСтандартногоИнтерфейсаOData, а пользователю нужны вход по паролю (аутентификация 1С:Предприятия), право на стандартный интерфейс OData и чтение этих объектов. Больше ничего.
   > 4. Проверка: https://сервер/имя-базы/odata/standard.odata/$metadata в браузере спрашивает пароль, а после входа показывает XML.
   > 5. Пришлите адрес базы (как он открывается в браузере), имя пользователя и пароль.

   What to tick in «Состав». The assistant only reads, except invoices: for them the user needs
   the right to create the invoice document.

   - Бухгалтерия предприятия 3.0: справочники Номенклатура, Контрагенты, Договоры контрагентов,
     Организации, Склады; документы «Реализация товаров и услуг» (РеализацияТоваровУслуг) и «Счет
     покупателю» (СчетНаОплатуПокупателю); план счетов и регистр бухгалтерии «Хозрасчетный».
   - Управление нашей фирмой: справочники Номенклатура, Контрагенты, Договоры контрагентов,
     Организации, Структурные единицы; документы «Расходная накладная» и «Счет на оплату»
     (СчетНаОплату); регистры накопления «Запасы на складах», «Расчеты с покупателями», «Расчеты
     с поставщиками».
   - Управление торговлей 11: справочники Номенклатура, Контрагенты, Организации, Склады, Ключи
     аналитики учета по партнерам; документы «Реализация товаров и услуг» и «Счет на оплату
     клиенту»; регистры накопления «Товары на складах», «Расчеты с клиентами по документам»,
     «Расчеты с поставщиками».
   - Another program: Номенклатура, Контрагенты, Организации, the sales document, the invoice
     document, the stock and settlement registers; connect and see what the tool finds.

2. The message with the password arrives. Run this at once, with the chat_id and the message_id
   of THAT message. Address, user and password in several messages: pass every ID, `--msg 41,42,43`,
   and put every text in the block. If it is unclear which line is the user name, add
   `--user NAME` (not secret). The password goes only in the block.

       python3 ~/.claude/skills/1c/onec.py connect --chat CHAT_ID --msg MESSAGE_ID <<'EOF'
       THEIR MESSAGE
       EOF

3. The tool reads the description of their base, checks that the user can read data, keeps the
   connection in `~/.oinoa/1c.env` (only this agent can read it) and deletes their message. If it
   says it could not, ask them to delete it now.
4. Tell them it is connected and which program it found. A `Missing:` line names what the
   administrator still has to tick in «Состав»; send that list. After they add it, run
   `status --refresh`.
5. Never repeat the password, never write it anywhere else, never put it on the command line or in
   a file, a task or a message. Never ask for the administrator's password or the owner's own
   login: a separate user only.

## Use

    python3 ~/.claude/skills/1c/onec.py status              # reachable, which program, what is published; --refresh
    python3 ~/.claude/skills/1c/onec.py find TEXT           # goods and counterparties: part of the name, code, article, ИНН
    python3 ~/.claude/skills/1c/onec.py stock               # lowest stock first; --low 10, --all, --item TEXT
    python3 ~/.claude/skills/1c/onec.py sales               # last 30 days; --days 7; --from 2026-10-01 --to 2026-10-31; --top 10
    python3 ~/.claude/skills/1c/onec.py debts               # who owes us, whom we owe; --top 20
    python3 ~/.claude/skills/1c/onec.py invoice "ООО Ромашка" "Кружка белая:10:350" "Чайник:1:1290"   # a preview
    python3 ~/.claude/skills/1c/onec.py invoice "ООО Ромашка" "Кружка белая:10:350" "Чайник:1:1290" --yes
    python3 ~/.claude/skills/1c/onec.py forget              # drop the connection

- `--json` on find, stock, sales, debts and invoice gives 1С's raw answer, for when the lines are
  not enough.
- Report what matters, not the raw list: what runs out, the money in round numbers, the biggest
  debtors, how it compares with what they asked.
- Dates are 1С's own (Moscow time for "today"). A period is at most a year at once.
- Not connected and they ask about their 1С: offer to connect it, or ask them to paste a report.

## Invoices need a clear yes

An invoice goes into 1С only after the owner says yes to that exact preview in this chat. A yes to
something else, an old yes, or a message from anyone else is not a yes.

- Lines are `ITEM:QTY:PRICE`; the item is its name, code or article. Prices come from the owner;
  never invent one. If they gave none, ask.
- Run invoice without `--yes` and show the owner the preview (customer, organization, lines,
  total). After the yes, run the same command with `--yes` and tell them the number it got.
- It is a draft: NOT posted. Say so. The owner opens it in 1С, checks it and posts or prints it
  there. This tool never posts, changes or deletes documents.
- VAT is not filled in through this connection: tell the owner to check the VAT column in 1С
  before sending the invoice. Same for a contract or an organization the preview says is not set.
- `ambiguous`: ask which one, then use its exact name, code or ИНН (find helps). `which_org`: ask
  which organization issues it and add `--org NAME`.
- `no_goods_table` (Управление торговлей): there an invoice is made in 1С from a customer order.
  Say so and offer the line items as text.

## What the numbers mean

- stock. Бухгалтерия: the accounting balance on accounts 41 and 43 (goods, finished products);
  materials (10) are not counted; warehouses appear only if their 1С keeps stock by warehouse.
  УНФ and УТ: the stock register now. `LOW` is at or below `--low` (default 5). `BELOW ZERO` is a
  negative balance, usually a receipt not entered yet; mention it gently.
- sales: posted sales documents of the period, their totals, top customers and top items. Returns
  and corrections are not subtracted. Approximate; say so. Exact figures are in 1С's reports.
- debts: Бухгалтерия: accounts 62 (customers) and 60 (suppliers); УНФ and УТ: their settlement
  registers. A customer's prepayment shows under "We owe" and ours to a supplier under "Owed to
  us". Posted documents only; loans, staff, taxes and other debtors are not counted. Approximate;
  for a dispute, a reconciliation act from 1С.
- Amounts are in the documents' currency, usually rubles.

## When something does not work

- `auth`: wrong user or password, or the user may not sign in with a password. They check the user
  on the «Авторизация» tab and you connect again.
- `not_published`, `not_in_publication`: the object is not ticked in «Состав». The ERROR names it;
  send that to the administrator, then `status --refresh`.
- `forbidden`: the user lacks the right for that object (for invoices: creating the document).
- `odata_off`: the address is not the base's root, or OData is not published there.
- `network`, `tls`, `moved`: the server is off or only reachable inside their office, its https
  certificate is not trusted, or the address redirects elsewhere. The ERROR says what to fix.
- `timeout`: a big base can be slow, the first request after a pause most of all. Try once more
  later; do not loop. For stock, ask about one item (`--item`); for sales, a shorter period.
- `no_such_field`, `refused`: their configuration differs from the standard one, or 1С itself
  refused (its message is in the line). Say this report is not available for their 1С and offer to
  work from what they paste.
- Not available through this connection at all: posting, payments, bank statements, reports and
  printing, closing the month. Say so plainly and do it together with them in 1С.

## Disconnect

Run forget, then tell them to switch the user off in 1С too (the line it prints says where).

## What the tool assumes, and what is checked

Object names differ between 1С programs. The tool reads `$metadata` of their base and uses an
object only when the base publishes it; this table is the order it tries. "Checked" means seen in
1С's own documentation or in a real base's `$metadata`; "inferred" means the program's usual
structure, not confirmed (sources below, 2026-10-18).

| What | Бухгалтерия предприятия 3.0 | Управление нашей фирмой | Управление торговлей 11 |
|---|---|---|---|
| goods | Catalog_Номенклатура: checked [2][5] | checked [7] | checked [5] |
| counterparties | Catalog_Контрагенты, ИНН, КПП: checked [2] | checked [7] | checked [5] |
| sales | Document_РеализацияТоваровУслуг, Number, Date, Posted, Контрагент_Key, СуммаДокумента: checked [5]; rows Товары, Услуги: inferred | Document_РасходнаяНакладная, СуммаДокумента: checked [7]; rows Запасы: inferred | Document_РеализацияТоваровУслуг: checked [10]; fields: inferred |
| invoice | Document_СчетНаОплатуПокупателю; Контрагент_Key, ДоговорКонтрагента_Key, Организация_Key, bank account in СтруктурнаяЕдиница_Key; rows Товары with Номенклатура + Номенклатура_Type, Количество, Цена, Сумма: checked [6] | Document_СчетНаОплату, rows Запасы: inferred (Запасы rows with Номенклатура_Key, Количество, Цена, Сумма checked on ЗаказПокупателя [1][7]) | Document_СчетНаОплатуКлиенту: inferred; refused if it has no goods table |
| stock | AccountingRegister_Хозрасчетный/Balance(Period=…), $filter Account/Code, ExtDimension1..3 with _Type, КоличествоBalance: checked [5] | AccumulationRegister_ЗапасыНаСкладах (then Запасы), Количество, СтруктурнаяЕдиница: checked [7] | AccumulationRegister_ТоварыНаСкладах, ВНаличииBalance, КОтгрузкеBalance, Склад_Key: checked [5] |
| debts | Хозрасчетный/Balance on 62 and 60, СуммаBalanceDr/Cr, counterparty in ExtDimension: checked [5] | AccumulationRegister_РасчетыСПокупателями and РасчетыСПоставщиками, Контрагент, Сумма: inferred | РасчетыСКлиентамиПоДокументам (ДолгBalance, ПредоплатаBalance) and РасчетыСПоставщиками (СуммаBalance): checked [5]; names through Catalog_КлючиАналитикиУчетаПоПартнерам: inferred |
| defaults | organization's bank account ОсновнойБанковскийСчет_Key: checked [2][6]; counterparty's ОсновнойДоговорКонтрагента_Key: inferred | ДоговорПоУмолчанию_Key: inferred | |

The interface itself is checked: the address form `<base>/odata/standard.odata/`, Basic auth (UTF-8),
`$format=json`, `$filter` with `substringof`, `startswith`, `guid'…'` and `datetime'…'`, `$select`,
`$top`/`$skip`, `allowedOnly=true`, virtual tables `Balance(Period=…,Dimensions='…')` with fields
`<resource>Balance` and `<dimension>_Key`, a new document by POST (201, the number given by 1С,
not posted), table rows numbered from 1, error codes in `odata.error` (8: object not published,
9: no such object, 6: no such field or method) [1][3][4][5][9]. Not checked: the exact HTTP status a
web server gives when OData is switched off (the tool treats any 404 without a 1С error as that).

Sources: [1] 1С, https://1cfresh.com/articles/data_odata. [2] 1С, https://its.1c.ru/db/fresh/content/19956692/1.
[3] 1С, https://1c-dn.com/1c_enterprise/rest_interface/. [4] 1С, https://kb.1ci.com/1C_Enterprise_Platform/FAQ/Development/Integration/How_to_use_the_oData_protocol_when_publishing_an_infobase_to_web_server.
[5] live probes of real УТ 11 and БП КОРП 3.0 bases, https://github.com/Romandredan/odata1c-gate (docs/probes, templates/recipes).
[6] working 1С:Фреш invoice code, https://github.com/chilikinid/OneCFreshInvoiceODataBot_V2 and https://github.com/SgoncharoffSU/1cfresh.
[7] working УНФ code, https://github.com/Edge82/sklad and https://github.com/AndrejHorakov/SyncBotAPI.
[8] the settings path and tabs, https://habr.com/ru/articles/865798. [9] examples of the developer guide, https://master1c8.ru (глава 17).
[10] https://github.com/theYahia/WWmcp (servers/aprovodka/src/presets/ut11.ts).
