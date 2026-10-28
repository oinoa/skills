---
name: print-check
description: >-
  Check a package, a label or any layout for errors before it goes to print: the barcode
  and its check digit, every word of the text, addresses, dates, the information the label
  must carry, and the technical setup the printer needs. Use for "проверь макет перед
  печатью", "проверь этикетку", "аудит упаковки", "нет ли ошибок на упаковке", "проверь
  штрихкод", "check this label before print", "proof this packaging", "is the barcode right".
metadata:
  author: OINOA
  version: "1.0"
---

# Print check

A reprinted run costs more than any other mistake a designer makes. Read the layout as the
printer, the shop scanner, the inspector and the buyer will. Every finding names where it is,
what is wrong and the fix.

## Procedure

1. **Get the real file and the facts.** The print file (PDF or the layout), the printer's
   requirements if there are any, and the source of truth for the text: the product
   name, composition, weight or volume, the maker's name and address, dates. Compare the
   layout with that source, not with memory.
2. **Barcode.**
   - EAN-13: compute the check digit from the first 12 digits (weights 1 and 3 from the left,
     sum, check = (10 − sum mod 10) mod 10) and compare with the 13th.
   - The number on the label is the one the person was issued for this product and this pack
     size. Never make up a barcode number.
   - Dark bars on a light background (red bars do not scan), a clear margin on both sides,
     not cut by a fold or a seam, not shrunk below 80% of the nominal size (37.29 × 25.93 mm).
   - A mandatory marking code (for example Russia's «Честный знак» DataMatrix, or a GS1 DataMatrix a
     retailer or an EU rule asks for) comes from that system. Leave its place and size as required;
     never draw or invent one.
3. **Text, every word.** Spelling, numbers, units (г, мл, %), the same name everywhere, the
   phone, the address and the website character by character. Read it twice: once as text,
   once backwards line by line, which stops the eye from filling in what it expects.
4. **What the label must carry.** Ask which rules apply, or check them with **deep-research**
   (food sold in the EU: Regulation (EU) 1169/2011 and the languages each country requires; in Russia
   and the EAEU: ТР ТС 022/2011). List each required item and where
   it is on the layout, or that it is missing. You check against the rule; the person and
   their lawyer decide.
5. **Technical setup.** Bleed (usually 3 mm, or what the printer asks), nothing important
   within 3 mm of the cut, images at least 300 dpi at final size, CMYK or the printer's
   colour profile, fonts embedded or outlined, the smallest text still readable.
6. **Report.** A numbered list: page or side, what is wrong, the fix. The most expensive
   finding first. Then the corrected layout as a new version, never over the old file.

## Rules

- Nothing is sent to the printer by you. The person approves the final version and sends it,
  or says yes to you sending it.
- A finding is concrete: "the 13th digit is 7, it should be 2", not "check the barcode".
- When a rule is unclear for this product, say so and who can confirm it.

## Worked example

A label for a jar of honey, side B. The barcode under the bars reads `4607001234567`.

```
first 12 digits:  4 6 0 7 0 0 1 2 3 4 5 6
weights:          1 3 1 3 1 3 1 3 1 3 1 3
products:         4 18 0 21 0 0 1 6 3 12 5 18   sum = 88
check digit:      (10 - 88 mod 10) mod 10 = 2
printed:          7   -> wrong, the code will not scan
```

Report in chat:

```
Нашёл три ошибки на этикетке, исправил в версии 2.
1. Штрихкод: последняя цифра 7, должна быть 2. Сверьте номер с тем, что вам выдали для этого товара.
2. Состав: «пчелинный» вместо «пчелиный».
3. Адрес производителя: на этикетке «ул. Лесная, 5», в ваших данных «ул. Лесная, 15».
До печати посмотрите версию 2.
```
