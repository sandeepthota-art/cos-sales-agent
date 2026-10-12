"""One-off generator for the labeling eval tracking sheet (eval/label_eval_set.xlsx).

5 sender ids x 3 emails each = 15 rows: one clear follow-up, one clear
commitment, one short/ambiguous email per sender -- content/expected label
left blank for a human to fill in once each email is actually drafted,
since classification depends on the real wording, not the variety alone.

Re-run after changing the layout below:
    python eval/generate_eval_sheet.py
"""

from openpyxl import Workbook
from openpyxl.worksheet.datavalidation import DataValidation
from openpyxl.styles import Font, Alignment, PatternFill
from openpyxl.utils import get_column_letter

LABELS = [
    "1. Needs reply: ASAP",
    "1. Needs reply",
    "1. Needs reply: mention",
    "1. Read only",
    "1. Delete",
    "1. Undecided",
]
VARIETIES = [
    "Follow-up (clear)",
    "Commitment (clear)",
    "Ambiguous/short (context-dependent)",
]
SENDER_COUNT = 5

HEADERS = [
    "Sender Group", "Sender Email", "Email #", "Variety",
    "Subject", "Body", "Expected Label", "Date Sent",
    "Gmail Message ID", "Actual Label", "Match",
]

wb = Workbook()
ws = wb.active
ws.title = "Eval Set"

ws.append(HEADERS)
header_fill = PatternFill("solid", fgColor="D9E1F2")
for col in range(1, len(HEADERS) + 1):
    cell = ws.cell(row=1, column=col)
    cell.font = Font(bold=True)
    cell.fill = header_fill
    cell.alignment = Alignment(wrap_text=True, vertical="center")
ws.freeze_panes = "A2"

row_num = 2
for sender in range(1, SENDER_COUNT + 1):
    for email_num, variety in enumerate(VARIETIES, start=1):
        ws.append([sender, "", email_num, variety, "", "", "", "", "", "", ""])
        row_num += 1

last_row = row_num - 1

# Match column: blank until both Expected (G) and Actual (J) are filled,
# then a simple string-equality check -- deliberately not fuzzy/case-insensitive,
# since the six labels are fixed exact strings.
for r in range(2, last_row + 1):
    ws.cell(row=r, column=11).value = f'=IF(OR(G{r}="",J{r}=""),"",IF(G{r}=J{r},"MATCH","MISMATCH"))'

label_list = ",".join(LABELS)
dv_variety = DataValidation(type="list", formula1=f'"{",".join(VARIETIES)}"', allow_blank=True)
dv_label_expected = DataValidation(type="list", formula1=f'"{label_list}"', allow_blank=True)
dv_label_actual = DataValidation(type="list", formula1=f'"{label_list}"', allow_blank=True)
for dv, col in ((dv_variety, "D"), (dv_label_expected, "G"), (dv_label_actual, "J")):
    ws.add_data_validation(dv)
    dv.add(f"{col}2:{col}{last_row}")

widths = {"A": 12, "B": 26, "C": 9, "D": 30, "E": 28, "F": 40, "G": 24, "H": 14, "I": 22, "J": 24, "K": 11}
for col, width in widths.items():
    ws.column_dimensions[col].width = width

notes = wb.create_sheet("Notes")
notes["A1"] = "How to use this sheet"
notes["A1"].font = Font(bold=True, size=13)
notes_text = [
    "",
    "1. Fill in 'Sender Email' for each of the 5 sender groups (5 different real mailboxes).",
    "2. Draft Subject/Body for each of the 15 rows. Variety is a stress-test design, not a label --",
    "   a 'Commitment (clear)' email might still correctly classify as 'Needs reply' or 'Read only'",
    "   depending on its actual wording. Decide Expected Label only after the content is final.",
    "3. Send the 15 emails from their respective sender addresses to the inbox the agent reads.",
    "4. Run Mode A (dump) in raw-dump-labeling, then Mode B (label) -- see skills/raw-dump-labeling/SKILL.md.",
    "5. Fill in Gmail Message ID and Actual Label per row from the run's report.",
    "6. Match column computes automatically once both Expected and Actual are filled.",
    "7. Accuracy = count(MATCH) / 15. Review every MISMATCH -- usually a sign the six-label rules in",
    "   SKILL.md need a sharper edge case, not that Claude 'got it wrong' arbitrarily.",
]
for i, line in enumerate(notes_text, start=2):
    notes[f"A{i}"] = line
notes.column_dimensions["A"].width = 100

wb.save("eval/label_eval_set.xlsx")
print("Wrote eval/label_eval_set.xlsx")
