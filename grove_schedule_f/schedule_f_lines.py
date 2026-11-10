"""Schedule F account and line map (pure Python, no Odoo import).

Line labels follow harknett/schedule-f-books (MIT).
They must be checked against the current IRS Schedule F instructions before
relying on them for a filing.

Each account dict carries:
  code          4-digit account code
  name          plain account name (the Schedule F line lives in `line`)
  line          Schedule F line (e.g. "1a", "21b") or None for balance-sheet accounts
  account_type  Odoo account.account.account_type value
  kind          "income", "expense" or "balance"
"""

ACCOUNTS = [
    # Income
    {"code": "4010", "name": "Sales of resale items", "line": "1a", "account_type": "income", "kind": "income"},
    {
        "code": "4020",
        "name": "Sales of raised produce, plants and seeds",
        "line": "2",
        "account_type": "income",
        "kind": "income",
    },
    {"code": "4030", "name": "Cooperative distributions", "line": "3a", "account_type": "income", "kind": "income"},
    {"code": "4040", "name": "Agricultural program payments", "line": "4a", "account_type": "income", "kind": "income"},
    {"code": "4050", "name": "CCC loans reported", "line": "5a", "account_type": "income", "kind": "income"},
    {"code": "4055", "name": "CCC loans forfeited", "line": "5b", "account_type": "income", "kind": "income"},
    {
        "code": "4060",
        "name": "Crop insurance and disaster payments",
        "line": "6a",
        "account_type": "income",
        "kind": "income",
    },
    {"code": "4070", "name": "Custom hire income", "line": "7", "account_type": "income", "kind": "income"},
    {"code": "4080", "name": "Other farm income", "line": "8", "account_type": "income", "kind": "income"},
    # Expenses
    {
        "code": "4015",
        "name": "Cost of resale items",
        "line": "1b",
        "account_type": "expense_direct_cost",
        "kind": "expense",
    },
    {"code": "6100", "name": "Car and truck", "line": "10", "account_type": "expense", "kind": "expense"},
    {"code": "6110", "name": "Chemicals", "line": "11", "account_type": "expense", "kind": "expense"},
    {"code": "6120", "name": "Conservation", "line": "12", "account_type": "expense", "kind": "expense"},
    {"code": "6130", "name": "Custom hire", "line": "13", "account_type": "expense", "kind": "expense"},
    {
        "code": "6140",
        "name": "Depreciation and section 179",
        "line": "14",
        "account_type": "expense_depreciation",
        "kind": "expense",
    },
    {
        "code": "6150",
        "name": "Employee benefit programs",
        "line": "15",
        "account_type": "expense",
        "kind": "expense",
    },
    {"code": "6160", "name": "Feed", "line": "16", "account_type": "expense", "kind": "expense"},
    {"code": "6170", "name": "Fertilizers and lime", "line": "17", "account_type": "expense", "kind": "expense"},
    {"code": "6180", "name": "Freight and trucking", "line": "18", "account_type": "expense", "kind": "expense"},
    {"code": "6190", "name": "Gasoline, fuel and oil", "line": "19", "account_type": "expense", "kind": "expense"},
    {"code": "6200", "name": "Insurance", "line": "20", "account_type": "expense", "kind": "expense"},
    {"code": "6210", "name": "Interest, mortgage", "line": "21a", "account_type": "expense", "kind": "expense"},
    {"code": "6215", "name": "Interest, other", "line": "21b", "account_type": "expense", "kind": "expense"},
    {"code": "6220", "name": "Labor hired", "line": "22", "account_type": "expense", "kind": "expense"},
    {
        "code": "6230",
        "name": "Pension and profit sharing",
        "line": "23",
        "account_type": "expense",
        "kind": "expense",
    },
    {
        "code": "6240",
        "name": "Rent, machinery and equipment",
        "line": "24a",
        "account_type": "expense",
        "kind": "expense",
    },
    {
        "code": "6245",
        "name": "Rent, land and other",
        "line": "24b",
        "account_type": "expense",
        "kind": "expense",
    },
    {"code": "6250", "name": "Repairs and maintenance", "line": "25", "account_type": "expense", "kind": "expense"},
    {"code": "6260", "name": "Seeds and plants", "line": "26", "account_type": "expense", "kind": "expense"},
    {"code": "6270", "name": "Storage and warehousing", "line": "27", "account_type": "expense", "kind": "expense"},
    {"code": "6280", "name": "Supplies", "line": "28", "account_type": "expense", "kind": "expense"},
    {"code": "6290", "name": "Taxes", "line": "29", "account_type": "expense", "kind": "expense"},
    {"code": "6300", "name": "Utilities", "line": "30", "account_type": "expense", "kind": "expense"},
    {
        "code": "6310",
        "name": "Veterinary, breeding and medicine",
        "line": "31",
        "account_type": "expense",
        "kind": "expense",
    },
    {"code": "6320", "name": "Other expenses", "line": "32", "account_type": "expense", "kind": "expense"},
    {"code": "6321", "name": "Payment processing fees", "line": "32", "account_type": "expense", "kind": "expense"},
    # Balance sheet (no Schedule F line)
    {"code": "1350", "name": "Intercompany", "line": None, "account_type": "asset_current", "kind": "balance"},
    {
        "code": "1500",
        "name": "Farm equipment and improvements",
        "line": None,
        "account_type": "asset_fixed",
        "kind": "balance",
    },
    {
        "code": "1590",
        "name": "Accumulated depreciation",
        "line": None,
        "account_type": "asset_fixed",
        "kind": "balance",
    },
    # Odoo 19 only accepts asset_cash or liability_credit_card as a bank journal default account,
    # and the "Owner paid" journal uses 2900, hence liability_credit_card rather than liability_current.
    {
        "code": "2900",
        "name": "Owner contributions payable",
        "line": None,
        "account_type": "liability_credit_card",
        "kind": "balance",
    },
    {"code": "3100", "name": "Owner draw", "line": None, "account_type": "equity", "kind": "balance"},
]

LINE_ORDER = [
    "1a",
    "1b",
    "2",
    "3a",
    "4a",
    "5a",
    "5b",
    "6a",
    "7",
    "8",
    "10",
    "11",
    "12",
    "13",
    "14",
    "15",
    "16",
    "17",
    "18",
    "19",
    "20",
    "21a",
    "21b",
    "22",
    "23",
    "24a",
    "24b",
    "25",
    "26",
    "27",
    "28",
    "29",
    "30",
    "31",
    "32",
]


def sort_key(line):
    """Sort key putting Schedule F lines in form order. None sorts last."""
    if line is None:
        return (999, "")
    digits = ""
    letters = ""
    for ch in line:
        if ch.isdigit() and not letters:
            digits += ch
        else:
            letters += ch
    return (int(digits), letters)


def accounts_for_line(line):
    """All accounts mapped to the given Schedule F line, in ACCOUNTS order."""
    return [a for a in ACCOUNTS if a["line"] == line]


def account_by_code(code):
    """The account with the given 4-digit code. Raises KeyError if unknown."""
    for a in ACCOUNTS:
        if a["code"] == code:
            return a
    raise KeyError(code)
