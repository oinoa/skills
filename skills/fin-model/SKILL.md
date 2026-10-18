---
name: fin-model
description: >-
  Build a financial model, an estimate or a deal calculation the person can check line by
  line: inputs with their sources, formulas instead of typed totals, a base case and a bad
  case, and the one number that decides. Use for "сделай финмодель", "посчитай окупаемость",
  "составь смету", "выгодна ли аренда", "оцени сделку", "посчитай неустойку", "build a
  financial model", "is this deal worth it", "make an estimate", "what is the payback".
metadata:
  author: OINOA
  version: "1.0"
---

# Financial model

A model is only as good as the input someone can check. Every number traces back to a
source the person can open, and every total is a formula. The answer is one line: the
number that decides, and what has to be true for it.

## Procedure

1. **Name the decision.** Write in one line what the model must answer: buy or rent,
   which price, is the deal worth it, how much to ask for. A model with no decision is a
   spreadsheet.
2. **Collect the inputs.** Prices, volumes, costs, rates, dates. For each: the value, the
   unit, the source (their document, a quote, a public page with its date) or the word
   "assumption". Ask the person only for the inputs that decide the answer. Never invent a
   price, a rate or a volume: leave a marked gap `[цена?]` and say what it changes.
3. **Lay it out in sheets.**
   - `Inputs`: every input on its own row, with unit and source. The only sheet with typed
     numbers.
   - `Model`: months (or years) in columns, every cell a formula that points at `Inputs`.
     Revenue, costs, taxes, cash flow, cumulative cash.
   - `Summary`: the decision line, payback, the lowest cash point, and the two cases side by
     side.
4. **Two cases at least.** Base, and a bad case where the two riskiest inputs move against
   them (fewer sales, higher costs). If the bad case breaks the business, say so first.
5. **Check before you send.** Totals match the sum of their lines. Units agree (per month vs
   per year, with or without VAT). Change one input and see the summary move. Taxes and
   legal rates are checked against the current law with **deep-research**, with the date.
6. **Deliver.** An Excel file with live formulas (python3 with openpyxl). If that library is
   missing and cannot be installed, send one CSV per sheet and say so. In chat: the decision
   line, the two cases in one sentence each, and the gaps that are still open.

## Estimates (смета)

One line per item: name, unit, quantity, unit price, total as a formula, source of the price.
Group by section, subtotal each, then VAT if it applies and the grand total. Mark every price
that is a guess and every item the person must confirm.

## Rules

- A figure without a source is labelled an assumption, in the file and in the chat.
- Taxes, penalties and interest follow the law in force on the date of the calculation; cite
  the article.
- You calculate and explain. The decision and any payment stay with the person.

## Worked example

The person: "Посчитай, окупится ли второй гостевой дом за 3 млн." Inputs they gave: build cost
3 000 000, price per night 6 000, open 300 nights a year, tax 6% of revenue. Missing:
occupancy and running costs.

Reply in chat (the file is attached):

```
Окупится за 5,5 года при загрузке 50%, при 35% больше чем за 10 лет.
Загрузку и расходы на содержание я принял сам: 50% и 25 000 в месяц. Это два главных допущения: пришлите свои цифры, и я пересчитаю.
Файл: finmodel-dom2.xlsx, листы Inputs, Model, Summary.
```

Behind it, on `Model`, every line is a formula on `Inputs`:

```
revenue  = price * nights * occupancy   6 000 x 300 x 0,50 = 900 000
costs    = monthly_costs * 12           25 000 x 12        = 300 000
tax      = revenue * tax_rate           900 000 x 6%       =  54 000
net      = revenue - costs - tax                           = 546 000
payback  = build_cost / net             3 000 000 / 546 000 = 5,5 years
bad case, occupancy 0,35: revenue 630 000, tax 37 800, net 292 200, payback 10,3 years
```

Change occupancy on `Inputs` and both payback lines move. A typed 5,5 in the summary would not.
