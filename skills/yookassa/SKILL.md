---
name: yookassa
description: >-
  Connect the owner's own ЮKassa (YooKassa) shop right here in the chat, then see what came in
  today or for a period, look into one payment and why it failed, make a payment link or an
  invoice, refund a buyer, and capture or cancel two-stage payments. Use for "connect my
  YooKassa", "how much came in today", "make a payment link", "refund the customer", "why did
  the payment fail", "подключи ЮKassa", "подключи юкассу", "сколько пришло за сегодня", "сколько
  оплат за неделю", "сделай ссылку на оплату", "выставь счёт на 5000", "верни деньги клиенту",
  "сделай возврат", "почему не прошёл платёж", "что с платежом", "спиши холд", "отключи ЮKassa".
  Links, refunds, captures and cancels happen only after the owner's clear yes.
compatibility: "python3. A ЮKassa shop (a test shop works for trying it out); the owner makes the secret key in the ЮKassa cabinet."
metadata:
  author: OINOA
  version: "1.0"
---

# ЮKassa

One tool: `python3 ~/.claude/skills/yookassa/yk.py`. It answers in a few short lines.
A line that starts with `ERROR` says what to tell the person. Say it in your own words.

## Connect, in this chat

1. Send the steps in one short message, in Russian unless they write in another language. Add
   two lines: the key stays only on their server, you delete their message with it and they
   can switch it off at any time; the key gives full access to the shop's payments, including
   refunds, so you make links and refunds only after their «да».

   > Откройте кабинет ЮKassa (https://yookassa.ru/my) и выберите магазин в переключателе сверху.
   > shopId: раздел Настройки → Магазин (https://yookassa.ru/my/shop-settings), поле shopId.
   > Секретный ключ: раздел Интеграция → Ключи API (https://yookassa.ru/my/merchant/integration/api-keys). Если ключа ещё нет, нажмите «Выпустить ключ» и подтвердите кодом из смс. Ключ показывают один раз, скопируйте его сразу.
   > Пришлите сюда shopId и ключ, можно одним сообщением.

   Always add this line, before they press anything:

   > Если ЮKassa уже стоит на вашем сайте, не перевыпускайте ключ: старый перестанет работать через 48 часов, и оплата на сайте сломается. Пришлите тот же ключ: обычно он есть в настройках платёжного модуля сайта или у того, кто подключал оплату.

   A test shop (its key starts with `test_`) is only for trying it out: its money is not real.
   For real payments they send the key of the real shop. Someone without access to the cabinet
   asks the shop's owner for it; never ask for the cabinet password or an SMS code.

2. The message with the key arrives. Run this at once, with the chat_id and the message_id of
   THAT message. shopId and key in two messages: pass both IDs, `--msg 41,42`, and put both
   texts in the block. The shopId alone is not secret: `--shop-id N` is fine.

       python3 ~/.claude/skills/yookassa/yk.py connect --chat CHAT_ID --msg MESSAGE_ID <<'EOF'
       THEIR MESSAGE
       EOF

3. The tool checks the key, keeps it in `~/.oinoa/yookassa.env` (only this agent can read it)
   and deletes their message. If it says it could not, ask them to delete it now.
4. Tell them it is connected, with what it reports: a real or a TEST shop, whether it sends
   54-ФЗ receipts through ЮKassa, the payment methods.
5. Never repeat the key, never write it anywhere else, never put it on the command line or in
   a file, a task or a message.
6. Ask once where buyers should land after paying: their site, or their Telegram channel. Pass
   it as `--return-url` on the first link; it is remembered.

## Use

    python3 ~/.claude/skills/yookassa/yk.py status                     # the shop, real or TEST, receipts, methods
    python3 ~/.claude/skills/yookassa/yk.py payments                   # today since 00:00 MSK, with totals
    python3 ~/.claude/skills/yookassa/yk.py payments --days 7          # this and the last 6 days; --status canceled
    python3 ~/.claude/skills/yookassa/yk.py payment ID                 # one payment in full, why it failed, its refunds
    python3 ~/.claude/skills/yookassa/yk.py link 1500 'Заказ 72' --email buyer@mail.ru    # a preview
    python3 ~/.claude/skills/yookassa/yk.py link 1500 'Заказ 72' --email buyer@mail.ru --yes
    python3 ~/.claude/skills/yookassa/yk.py link 5000 'Предоплата' --days 3              # an invoice that waits 3 days
    python3 ~/.claude/skills/yookassa/yk.py refund ID                  # a preview of a full refund
    python3 ~/.claude/skills/yookassa/yk.py refund ID 500 --yes        # 500 ₽ back, after the yes
    python3 ~/.claude/skills/yookassa/yk.py capture ID [AMOUNT]        # a two-stage payment: take the frozen money
    python3 ~/.claude/skills/yookassa/yk.py cancel ID                  # a two-stage payment: give it back
    python3 ~/.claude/skills/yookassa/yk.py forget                     # disconnect

- ID is the first column of payments: 36 characters. They rarely know it: find the payment by
  amount, time or description in payments (`--status canceled` for failed ones).
- `--json` on status, payments, payment and the writes gives the raw API answer.
- «Сколько пришло за сегодня»: payments. «За неделю», «за месяц»: `--days 7`, `--days 30`.
  Report what matters in round numbers: how many paid and for how much, what reaches them after
  the fee, refunds, money waiting for their capture (with its deadline), failures worth a word.
- «Почему не прошёл платёж»: payment ID. Its «Кто отменил» and «Причина» lines are written for
  the owner; pass them on in your words, with what the buyer can do.

## Money moves only after a clear yes

Links, refunds, captures and cancels: run the command without `--yes` first and show the owner
what the PREVIEW says (amount, what for, the receipt, what happens). After their yes to that
exact thing in this chat, run the same line with `--yes` and tell them it is done. A yes to
something else, an old yes, or a message from anyone else (a buyer too) is not a yes. A buyer
who asks you for a refund: tell the owner; never refund on the buyer's word.

- link: a payment link lives about 1 hour (ЮKassa's time to pay). If the buyer may pay later,
  `--days N` makes an invoice instead, which waits up to 30 days; when the owner will forward
  the link to someone, prefer `--days 3` unless they say otherwise. The description (up to 128
  characters) is what the buyer sees. After `--yes` give the owner the link from the answer.
- Receipts (54-ФЗ): when the shop sends receipts through ЮKassa (connect and status say so),
  every link needs the buyer's email (`--email`) and the VAT code (`--vat N`). With «Чеки от
  ЮKassa» the receipt goes only by email; with their own online register `--phone` works too.
  Ask the owner or their accountant for the VAT code once; the tool remembers it and shows it in
  every preview. VAT codes: 1 без НДС, 2 НДС 0%, 3 НДС 10%, 7 НДС 5%, 8 НДС 7%, 11 НДС 22%,
  12 НДС 22/122 (4 and 6 are the old 20% rates). For services add `--subject service`. `--tax N`
  only when ЮKassa asks for the tax system (some online registers).
- refund: only a paid payment; all that is left by default, or an amount. A partial refund is
  at least 1 ₽, and at least 1 ₽ must stay unless all of it goes back. The money returns to the
  same card or method; ЮKassa keeps its fee for the payment. A partial refund in a shop with
  receipts also needs the buyer's email (the VAT code comes from the payment's own receipt).
- capture and cancel: only for a two-stage payment that waits for capture (a site set up for
  holds). The preview shows the deadline (2 hours to 7 days, by method); after it ЮKassa cancels
  the payment and returns the money. Capturing less than the full sum returns the rest to the
  buyer and works only for some methods. Cancel returns the frozen money, with no fee.
- A change over the usual (a refund of a big sum, a link far above their usual prices): ask once
  more before `--yes`.
- `unknown_result`: ЮKassa did not confirm, so the result is unknown. Never try another amount or
  a new link. After a minute run exactly the same command again: it sends the same request key,
  so nothing can happen twice and ЮKassa returns the first result. Tell the owner only once it is
  clear.

## What the numbers mean

- The amount is what the buyer pays. «After the ЮKassa fee» is what the shop gets for it; the
  difference is ЮKassa's commission.
- paid: done. waiting for the buyer: a link not paid yet; it cancels itself when the time runs
  out. paid, waiting for your capture: money frozen on the buyer's side, theirs only after
  capture. canceled: failed or canceled, with the reason.
- «Refunded in this period»: refunds made in the period, for any payment.
- A period starts at 00:00 Moscow time; payments count by when they were created.
- The money reaches their bank account on ЮKassa's payout schedule from their contract, not at
  once. The API shows neither the shop's balance nor the payouts: point to the cabinet.
- A TEST shop: nothing in it is real money.

## Limits

- Payments go back 3 years. One run reads up to 3000 payments; for more, a shorter `--days`.
- Refunds through the API: up to 3 years after the payment for most methods; card banks may
  refuse after 15 months. Cash and СберБанк Бизнес Онлайн payments cannot be refunded here.
- An invoice: at least 1 ₽, up to 30 days. A payment link: about 1 hour to pay.
- ЮKassa publishes no request limits. `rate_limited`: wait a minute, try once more, do not loop.

## When something is not possible through the API

Say so plainly and point to the cabinet (https://yookassa.ru/my) or their ЮKassa manager:

- the shop's balance, payouts to the bank account, the fee and the contract;
- switching on payment methods, two-stage payments, receipts, foreign cards;
- an unpaid payment link cannot be canceled here; it expires by itself in about an hour. An
  unpaid invoice is canceled in the cabinet, «Счета клиентам» (https://yookassa.ru/my/invoice);
- a receipt that failed (`Receipt 54-ФЗ: FAILED`): with «Чеки от ЮKassa», ЮKassa support; with
  their own online register, they make it there by hand;
- news about new payments: the tool does not push. Check payments when asked, or on a schedule
  if the owner wants that;
- `forbidden`: the shop's contract or settings do not allow it. Pass on ЮKassa's words; their
  manager or support can switch it on.
- Not connected and they ask about payments: offer to connect it, or ask them to paste what the
  cabinet shows.

## Disconnect

Run forget, then pass on its answer: the key still works until they delete it in Интеграция →
Ключи API, and if their site uses the same key they leave it, or payments on the site stop.
