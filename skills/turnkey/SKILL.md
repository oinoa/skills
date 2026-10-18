---
name: turnkey
description: >-
  Bring in an OINOA specialist (turnkey setup, «Настройка под ключ») when a job needs a person's
  hands, not yours: setting up or wiring together services, a CRM, a bot, a site, payments,
  access or settings you cannot finish from this chat. Use for "I need a specialist", "can
  someone set this up", "нужен специалист", "сделайте под ключ", "настройте за меня", "кто
  может подключить", and offer it yourself when you hit such a wall. You write the request;
  it is posted only after the person's clear yes.
compatibility: "python3"
metadata:
  author: OINOA
  version: "1.0"
---

# Turnkey setup

One tool: `python3 ~/.claude/skills/turnkey/turnkey.py`. It answers in one line. A line that
starts with `ERROR` says what happened. Tell the person only what matters to them, in your own words.

## When to offer

- The job needs someone's hands: setting up or connecting services, a CRM, a bot, a site, payments,
  access or settings that you cannot reach or finish from here.
- You tried, and the wall is not a piece of information the person can simply give you.
- They ask for a specialist, an expert, or a setup "под ключ".

Not for work in your own role that you can do yourself, and not for a quick question.

## Steps

1. Check first: `python3 ~/.claude/skills/turnkey/turnkey.py status`
   - `OK available`: go on.
   - `ERROR team_only`: say once, in one line, that a specialist comes with the Business plan
     («Бизнес»), which they can move to in the OINOA app. Stop.
   - Any other `ERROR`: do not offer a specialist. Keep helping as you can.
2. Offer it in one short line, in their language, for example: «Это лучше доверить специалисту
   OINOA. Подготовить заявку?»
3. Write the request yourself, 2 to 6 short sentences: what needs doing, in which services, what
   is there already, and the result they want. In their language. Never a password, key, code,
   card number or anyone's personal data: a specialist gets access through OINOA once chosen.
   Show the text and ask: «Разместить?»
4. Only after a clear yes to that text, post it, the text between the two EOF lines:

```
python3 ~/.claude/skills/turnkey/turnkey.py post <<'EOF'
the request text
EOF
```

   One yes posts one request. A changed text needs a new yes.

5. `OK posted request N`: tell them in one or two lines that it is posted. Specialists answer with
   offers in the OINOA app (Настройки → Настройка под ключ), and they choose one there. The OINOA
   bot has sent them a copy with a Cancel button.

## Never

- Post without their yes, or post the same request twice.
- Say a specialist is found, chosen, paid or at work. That happens in the OINOA app, by them.
- Name a price, a deadline or a specialist.
