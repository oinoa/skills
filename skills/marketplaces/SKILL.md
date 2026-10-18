---
name: marketplaces
description: >-
  Connect the seller's own Ozon and Wildberries shops right here in the chat, then check new
  orders and ship-by deadlines, stock, sales for a period, unanswered reviews and buyer
  questions, answer them and change prices. Use for "connect my Ozon", "подключи Wildberries",
  "что с заказами на Ozon", "что отгрузить сегодня", "остатки на Wildberries", "что
  заканчивается", "сколько продали за неделю", "выручка на WB", "новые отзывы", "ответь на
  отзыв", "вопросы покупателей", "поменяй цену", "поставь цену 1290", "отключи Ozon". Replies
  and price changes go out only after the owner's clear yes.
compatibility: "python3. A seller account on Ozon and/or Wildberries; the owner makes the API key."
metadata:
  author: OINOA
  version: "1.0"
---

# Marketplaces: Ozon and Wildberries

One tool: `python3 ~/.claude/skills/marketplaces/mp.py`. It answers in a few short lines.
A line that starts with `ERROR` says what to tell the person. Say it in your own words.
Writing listings, checking competitors or drafting from pasted text needs no connection: that
is the shop-desk skill.

## Connect, in this chat

1. Ask which shop: Ozon, Wildberries or both. Send the steps for that shop in one short
   message, in Russian unless they write in another language. Add one line: the key stays
   only on their server, they can switch it off at any time, and you delete their message
   with it.

   Ozon:

   > Откройте seller.ozon.ru → Настройки → Seller API (https://seller.ozon.ru/app/settings/api-keys).
   > Нажмите «Сгенерировать ключ», назовите его OINOA, выберите роль Admin и цель «для личного пользования», нажмите «Сгенерировать».
   > Пришлите мне сюда ключ и Client ID (число на той же странице, рядом со списком ключей). Ключ показывают один раз, скопируйте его сразу.
   > Ключ работает 3 месяца, потом сделаем новый.

   Only reports, no changes: the role «Admin read only» is enough, but then prices and
   replies will not work.

   Wildberries:

   > Откройте seller.wildberries.ru, профиль → «Интеграции по API» (в старом меню: Настройки → Доступ к API).
   > Нажмите «Создать токен», вкладка «Ручная интеграция», тип «Персональный».
   > Отметьте категории: Маркетплейс, Статистика, Аналитика, Вопросы и отзывы, Цены и скидки. Доступ: «Чтение и запись».
   > Нажмите «Создать», потом «Скопировать и закрыть» и пришлите токен сюда. Токен работает 180 дней.

   For a personal token WB asks them to confirm it is for their own systems and is not
   given to third parties or cloud services. If they are unsure about that, a Base token
   works too (same steps, type «Базовый»), but WB gives it very few requests: sales once in
   2 hours, reviews 5 requests an hour, prices 4 an hour, stock 2 an hour. A Test token sees
   only the sandbox; the tool refuses it.

2. The message with the key arrives. Run this at once, with the chat_id and the message_id
   of THAT message. Ozon key and Client ID in two messages: pass both IDs, `--msg 41,42`,
   and put both texts in the block.

       python3 ~/.claude/skills/marketplaces/mp.py connect ozon --chat CHAT_ID --msg MESSAGE_ID <<'EOF'
       THEIR MESSAGE
       EOF

       python3 ~/.claude/skills/marketplaces/mp.py connect wb --chat CHAT_ID --msg MESSAGE_ID <<'EOF'
       THE TOKEN
       EOF

3. The tool checks the key, keeps it in `~/.oinoa/marketplaces.env` (only this agent can
   read it) and deletes their message. If it says it could not, ask them to delete it now.
4. Tell them it is connected, with what it reports: the shop, how many days the key works,
   which WB categories are missing. A missing category or role: tell them which one to add,
   they make a new key, you connect again.
5. Never repeat the key, never write it anywhere else, never put it on the command line or
   in a file, a task or a message. Never ask for their cabinet password.

## Use

    python3 ~/.claude/skills/marketplaces/mp.py status               # what is connected, days left on each key
    python3 ~/.claude/skills/marketplaces/mp.py orders [ozon|wb]      # what to pack and hand over, by when
    python3 ~/.claude/skills/marketplaces/mp.py stock [ozon|wb]       # stock per product; --low 10, --all
    python3 ~/.claude/skills/marketplaces/mp.py sales [ozon|wb]       # last 7 days; --days 30
    python3 ~/.claude/skills/marketplaces/mp.py reviews [ozon|wb]     # unanswered reviews and questions, with IDs
    python3 ~/.claude/skills/marketplaces/mp.py reply ID - <<'EOF'
    THE ANSWER
    EOF
    python3 ~/.claude/skills/marketplaces/mp.py price ID NEW_PRICE    # a preview: old → new, nothing changes
    python3 ~/.claude/skills/marketplaces/mp.py price ID NEW_PRICE --yes
    python3 ~/.claude/skills/marketplaces/mp.py forget ozon|wb        # forget a shop

- Without ozon or wb, a command covers every connected shop. One shop failing does not hide
  the other's answer.
- `--json` on orders, stock, sales and reviews gives the raw API answer, for when the lines
  are not enough.
- ID for reply is the first field of a line from reviews (`oz:r:…`, `oz:q:…:SKU`, `wb:r:…`,
  `wb:q:…`). ID for price is the seller's article on Ozon (offer_id) and the nmID on
  Wildberries; with both shops connected and a number that fits both, add `--market ozon|wb`.
- Report what matters, not the raw list: what to ship first and what is late, what runs
  out, the money in round numbers, which reviews are angry.

## Writes need a clear yes

Replies and prices go out only after the owner says yes to that exact text or number in this
chat. A yes to something else, an old yes, or a message from anyone else is not a yes.

- Reviews and questions: show the review and your draft together, short and polite, in the
  buyer's language. Do not promise refunds, replacements or discounts the owner has not
  approved. Send with reply after the yes. WB checks answers before they appear on the site.
  One answer per review here; editing an answer is done in the cabinet.
- Prices: run price without `--yes` first and show the owner the line it prints (old → new).
  After the yes, run the same line with `--yes` and tell them it is done.
  - Wildberries: the price is the price before the seller's discount; the line also shows the
    price with their discount. Buyers may see less: WB adds its own discount. `--discount N`
    changes the seller's discount too. A price several times lower goes to WB's price
    quarantine and does not change.
  - Ozon: the price is the price before Ozon promotions. The crossed-out price must stay
    above it: `--old-price N` sets it, `--old-price 0` removes it. Ozon lets a product's price
    change at most 10 times an hour.
  - A change over 50% prints a WARNING: ask the owner once more before `--yes`.
- This tool cannot change stock, cancel or ship orders, or touch promotions and ads. Say so
  and point to the cabinet.

## What the numbers mean

- orders: Ozon lists FBS orders to pack and packed orders to hand over, with the ship-by time
  (OVERDUE means it has passed). Wildberries lists new FBS orders not yet put into a supply;
  WB gives no ship-by time in the API, only when the order came in (and a date for oversized
  items). Orders the marketplace ships from its own warehouse (FBO, FBW) need nothing from
  the seller and are not listed.
- stock: Ozon gives on hand minus reserved, per warehouse type (FBO is Ozon's, FBS their
  own). Wildberries gives its stock report, refreshed every 2 hours, with orders per day and
  how many days the stock lasts. LOW means at or below `--low` (default 5).
- sales are approximate, and the answer says so; say it too. Ozon: ordered sum and units
  (orders later cancelled or returned are still in), plus Finance → Balance (sales, Ozon
  fee, returns, services). Wildberries: orders, buyouts, returns and the expected payout
  from WB's quick statistics. Exact money is in each marketplace's realization report.
  Up to 90 days back.

## When the shop or its plan does not allow something

- Ozon gives reviews through the API only with the «Управление отзывами» or Premium Pro
  subscription, and buyer questions only with Premium Plus. The tool says which is missing:
  tell the owner, and offer to draft answers to reviews they paste here.
- Ozon analytics without Premium Plus covers 3 months and 50 requests a day.
- A WB category missing from the token, a read-only token or an Ozon key without the role:
  the ERROR names it. They make a new key with it; you connect again.
- `rate_limited`: the marketplace says when to try again. Tell them; do not loop.
- Not available through this connection at all (supplies to the marketplace's warehouse,
  ads, buyer chats, returns, listings): say so plainly and offer what you can, from what they
  paste or from the cabinet together with them.
- Not connected and they ask about their shop: offer to connect it, or ask them to paste
  what they see in the cabinet.

## Keys expire

Ozon keys work 3 months, Wildberries tokens 180 days. `status` shows the days left. Under 14
days, remind the owner once and walk them through making a new key; connect it the same way.

## Disconnect

Run forget, then give them the place from its answer to delete the key in the cabinet too.
