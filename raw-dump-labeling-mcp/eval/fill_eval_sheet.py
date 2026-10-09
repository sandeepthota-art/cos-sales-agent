"""Fills eval/label_eval_set.xlsx with 15 drafted test emails (sender email,
subject, body, expected label) -- content only; Date Sent/Gmail Message
ID/Actual Label stay blank until the real emails are sent and run.

Run once: python eval/fill_eval_sheet.py
"""

from openpyxl import load_workbook

# (sender_email, sender_display_name) -- placeholder @example.com addresses;
# replace with real mailboxes you control before sending.
SENDERS = {
    1: "Alex Rivera <prospect1@example.com>",       # prospect, Nimbus Retail
    2: "Priya Nair <teammate2@example.com>",        # internal colleague
    3: "Dana Whitfield <vendor3@example.com>",      # cold vendor outreach, BrightLeaf Solutions
    4: "Michael Chen <client4@example.com>",        # long-time client, Lattice Manufacturing
    5: "Rachel Ortiz <ops5@example.com>",           # internal ops
}

# Row order matches generate_eval_sheet.py's nested loop: sender 1..5, each
# with email# 1..3 (follow-up, commitment, ambiguous/short).
ROWS = [
    # sender, email#, subject, body, expected_label
    (1, 1, "Re: Our call last week",
     "Hi, following up on our conversation last Thursday about the Q4 rollout. "
     "Have you had a chance to discuss internally? Would love to hear where "
     "things stand whenever you get a chance.",
     "1. Needs reply"),
    (1, 2, "PO details needed today to lock in pricing",
     "Hi, we're ready to move forward -- I just need your billing contact and "
     "tax ID so I can get the PO cut on our end today before the quarter "
     "closes. Can you send those over in the next hour or two?",
     "1. Needs reply: ASAP"),
    (1, 3, "Thoughts?",
     "Thoughts?",
     "1. Undecided"),

    (2, 1, "Circling back on the deck",
     "Hey, circling back on the client deck -- did you want any changes "
     "before I send it out tomorrow morning? No rush if it's already good "
     "to go.",
     "1. Needs reply"),
    (2, 2, "Need confirmation -- Monday EOD deliverable",
     "Quick one -- I told the client we'd have the report to them by Monday "
     "end of day. Can you confirm you're still on track so I don't need to "
     "adjust expectations with them?",
     "1. Needs reply: ASAP"),
    (2, 3, "Fwd: Pricing doc",
     "Did you see Sarah's comment on the pricing doc? She flagged something "
     "on slide 4 that might need your input before we finalize.",
     "1. Needs reply: mention"),

    (3, 1, "Just checking in on our proposal",
     "Hi there, just wanted to check in and see if you'd had a chance to "
     "review the proposal we sent over last month. Happy to hop on a call "
     "whenever works for you!",
     "1. Delete"),
    (3, 2, "20% off if you sign this month",
     "Wanted to reach out directly -- we can offer a 20% discount on the "
     "annual plan if you're able to sign before the end of the month. Let "
     "me know if that changes things on your end.",
     "1. Needs reply"),
    (3, 3, "Meeting Response: Declined",
     "Dana Whitfield has declined the meeting 'Intro Call -- BrightLeaf x "
     "Your Company'.",
     "1. Delete"),

    (4, 1, "Renewal terms -- contract expires in 3 days",
     "Hi, following up -- did the team land on a decision for the renewal "
     "terms we discussed last week? Just flagging that our current contract "
     "expires in 3 days, so we'll need to move quickly either way.",
     "1. Needs reply: ASAP"),
    (4, 2, "Signed contract attached",
     "As promised, attached is the signed contract on our end. Let me know "
     "once you've countersigned and I'll loop in finance to set up billing.",
     "1. Needs reply"),
    (4, 3, "Re: Signed contract attached",
     "Thanks!",
     "1. Read only"),

    (5, 1, "Expense report due tomorrow",
     "Hey, following up -- have you submitted your Q3 expense report yet? "
     "Finance needs everything in by tomorrow at noon to close the quarter "
     "on time.",
     "1. Needs reply: ASAP"),
    (5, 2, "FYI -- budget numbers submitted",
     "FYI, I went ahead and submitted the Q4 budget numbers on your behalf "
     "since you were out yesterday. No action needed from you -- just "
     "wanted you in the loop.",
     "1. Read only"),
    (5, 3, "Fwd: Office update",
     "See below.\n\n---\nFrom: Facilities\nSubject: Carpet cleaning "
     "schedule\n\nCarpet cleaning in the east wing is scheduled for next "
     "Tuesday between 6-8am.",
     "1. Undecided"),
]

wb = load_workbook("eval/label_eval_set.xlsx")
ws = wb["Eval Set"]

row = 2
for sender, email_num, subject, body, expected in ROWS:
    ws.cell(row=row, column=2).value = SENDERS[sender]  # Sender Email
    ws.cell(row=row, column=5).value = subject            # Subject
    ws.cell(row=row, column=6).value = body                # Body
    ws.cell(row=row, column=7).value = expected             # Expected Label
    row += 1

wb.save("eval/label_eval_set.xlsx")
print(f"Filled {len(ROWS)} rows in eval/label_eval_set.xlsx")
