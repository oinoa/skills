---
name: mail
description: >-
  Connect the person's own mailbox (Gmail, Yandex, Mail.ru, iCloud) right here in the chat,
  then list, search, read, reply to and send their mail. Use for "connect my mail",
  "подключи почту", "what's new in my inbox", "find the letter from…", "answer Ivan",
  "disconnect my mail". Outlook cannot be connected yet. Calendars are not connected here:
  Yandex Calendar is connected in Настройки → Инструменты, then read with business-apps.
compatibility: "python3. Gmail needs 2-Step Verification on for an app password."
metadata:
  author: OINOA
  version: "1.0"
---

# Mail

One tool: `python3 ~/.claude/skills/mail/mail.py`. It answers in a few short lines.
A line that starts with `ERROR` says what to tell the person. Say it in your own words.

## Connect, in this chat

1. Get the address. Outlook, Hotmail, Live or a Microsoft work address: say Outlook cannot
   be connected yet, and ask them to forward the letter or paste it here. Stop.
2. Send the steps for their mail in one short message, in their language:
   - Gmail: turn on 2-Step Verification, open https://myaccount.google.com/apppasswords,
     type OINOA as the name, tap Create, send me the 16 letters.
   - Yandex: open https://id.yandex.ru/security/app-passwords, choose Mail, create, send me the password.
   - Mail.ru: open https://account.mail.ru/user/2-step-auth/passwords/, add one, send me the password.
   - iCloud: open https://account.apple.com, Sign-In and Security, App-Specific Passwords,
     create one, send me the password.
   Add one line: it is a separate password only for mail, not their main one, and they can
   switch it off at any time.
3. The message with the password arrives. Run this at once, with the chat_id and the
   message_id of THAT message:

       python3 ~/.claude/skills/mail/mail.py connect ADDRESS --chat CHAT_ID --msg MESSAGE_ID <<'EOF'
       THE PASSWORD
       EOF

   A company address on Google or Yandex: add `--provider gmail` or `--provider yandex`.
4. The tool deletes their message with the password. If it says it could not, ask them to
   delete that message now.
5. Tell them the mailbox is connected, with the count it gives. Never repeat the password
   or write it anywhere else. Never ask for their main password.

Gmail says the app password page is not available: 2-Step Verification is off, or their
work admin switched app passwords off. Until that changes, they forward or paste.

## Use

    python3 ~/.claude/skills/mail/mail.py list              # the 10 newest; list 30; list --unread
    python3 ~/.claude/skills/mail/mail.py search WORDS      # by sender, subject or text
    python3 ~/.claude/skills/mail/mail.py read ID           # one letter in full; --save keeps attachments
    python3 ~/.claude/skills/mail/mail.py reply ID <<'EOF'
    TEXT
    EOF
    python3 ~/.claude/skills/mail/mail.py send ADDRESS "SUBJECT" <<'EOF'
    TEXT
    EOF
    python3 ~/.claude/skills/mail/mail.py status            # which mailbox is connected
    python3 ~/.claude/skills/mail/mail.py disconnect        # forget the mailbox

- ID is the number at the start of a line from list or search.
- Reading never marks a letter as read.
- reply and send only after a clear yes to that exact text.
- Report what matters, not the raw list: who wrote, what they want, by when.
- Disconnect: run it, then give them the link from its answer to switch the password off.
- Not connected and they ask about their mail: offer to connect it, or ask them to forward
  or paste the letter.
- Calendars are not connected here. Yandex Calendar (and Bitrix24's) the person connects in
  Настройки → Инструменты, then use **business-apps**; Google and Outlook calendars cannot be
  connected yet. An invitation that arrives by mail you read like any letter.
