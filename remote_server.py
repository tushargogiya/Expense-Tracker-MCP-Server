"""
Expense Tracker MCP Server — SQLite3 backend, industry-standard feature set.
"""
import calendar
import csv
import io
import json
import os
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from fastmcp import FastMCP

# ─── Constants ────────────────────────────────────────────────────────────────

DB_PATH = Path(os.environ.get("DB_PATH", "/tmp/expenses.db"))

DEFAULT_CATEGORIES = [
    ("Food & Dining",        "#FF6B6B", "🍽️"),
    ("Transportation",       "#4ECDC4", "🚗"),
    ("Shopping",             "#45B7D1", "🛍️"),
    ("Entertainment",        "#96CEB4", "🎬"),
    ("Bills & Utilities",    "#FFEAA7", "⚡"),
    ("Health & Medical",     "#DDA0DD", "🏥"),
    ("Travel",               "#98D8C8", "✈️"),
    ("Education",            "#F7DC6F", "📚"),
    ("Personal Care",        "#F8C8D4", "💄"),
    ("Home & Garden",        "#A8D8EA", "🏠"),
    ("Business",             "#B8860B", "💼"),
    ("Savings & Investment", "#90EE90", "💰"),
    ("Other",                "#D3D3D3", "📌"),
]

VALID_PAYMENT_METHODS = [
    "cash", "credit_card", "debit_card",
    "bank_transfer", "digital_wallet", "check", "other",
]
VALID_PERIODS    = ["monthly", "yearly"]
VALID_FREQUENCIES = ["daily", "weekly", "biweekly", "monthly", "yearly"]
VALID_SORT_FIELDS = ["date", "amount", "created_at"]

# ─── Database Layer ────────────────────────────────────────────────────────────

def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db() -> None:
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS categories (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                name         TEXT    NOT NULL UNIQUE,
                color        TEXT    NOT NULL DEFAULT '#808080',
                icon         TEXT    NOT NULL DEFAULT '📌',
                budget_limit REAL,
                created_at   TEXT    NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS expenses (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                amount         REAL    NOT NULL CHECK(amount > 0),
                currency       TEXT    NOT NULL DEFAULT 'USD',
                category_id    INTEGER REFERENCES categories(id) ON DELETE SET NULL,
                description    TEXT    NOT NULL DEFAULT '',
                date           TEXT    NOT NULL DEFAULT (date('now')),
                payment_method TEXT    NOT NULL DEFAULT 'cash',
                tags           TEXT    NOT NULL DEFAULT '[]',
                notes          TEXT    NOT NULL DEFAULT '',
                created_at     TEXT    NOT NULL DEFAULT (datetime('now')),
                updated_at     TEXT    NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS budgets (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
                amount      REAL    NOT NULL CHECK(amount > 0),
                period      TEXT    NOT NULL DEFAULT 'monthly',
                year        INTEGER NOT NULL,
                month       INTEGER,
                created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
                UNIQUE(category_id, period, year, month)
            );

            CREATE TABLE IF NOT EXISTS recurring_expenses (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                amount         REAL    NOT NULL CHECK(amount > 0),
                currency       TEXT    NOT NULL DEFAULT 'USD',
                category_id    INTEGER REFERENCES categories(id) ON DELETE SET NULL,
                description    TEXT    NOT NULL,
                frequency      TEXT    NOT NULL,
                next_due       TEXT    NOT NULL,
                payment_method TEXT    NOT NULL DEFAULT 'cash',
                tags           TEXT    NOT NULL DEFAULT '[]',
                is_active      INTEGER NOT NULL DEFAULT 1,
                created_at     TEXT    NOT NULL DEFAULT (datetime('now'))
            );

            CREATE INDEX IF NOT EXISTS idx_expenses_date     ON expenses(date);
            CREATE INDEX IF NOT EXISTS idx_expenses_category ON expenses(category_id);
            CREATE INDEX IF NOT EXISTS idx_expenses_amount   ON expenses(amount);
        """)

        if conn.execute("SELECT COUNT(*) FROM categories").fetchone()[0] == 0:
            conn.executemany(
                "INSERT OR IGNORE INTO categories (name, color, icon) VALUES (?, ?, ?)",
                DEFAULT_CATEGORIES,
            )


def row_to_dict(row: sqlite3.Row) -> dict:
    return dict(row)


def resolve_category(conn: sqlite3.Connection, category: str) -> Optional[int]:
    """Resolve a category name or numeric ID string to a category row ID."""
    if category.strip().lstrip("-").isdigit():
        row = conn.execute("SELECT id FROM categories WHERE id = ?", (int(category),)).fetchone()
    else:
        row = conn.execute(
            "SELECT id FROM categories WHERE LOWER(name) = LOWER(?)", (category,)
        ).fetchone()
    return row["id"] if row else None


def next_due_after(current: str, frequency: str) -> str:
    dt = datetime.strptime(current, "%Y-%m-%d")
    if frequency == "daily":
        dt += timedelta(days=1)
    elif frequency == "weekly":
        dt += timedelta(weeks=1)
    elif frequency == "biweekly":
        dt += timedelta(weeks=2)
    elif frequency == "monthly":
        month = dt.month % 12 + 1
        year  = dt.year + (1 if dt.month == 12 else 0)
        dt = dt.replace(year=year, month=month)
    elif frequency == "yearly":
        dt = dt.replace(year=dt.year + 1)
    return dt.strftime("%Y-%m-%d")


# ─── MCP Server ───────────────────────────────────────────────────────────────

mcp = FastMCP(
    name="Expense Tracker",
    instructions=(
        "Comprehensive expense tracking server. "
        "Track expenses, manage budgets, analyse spending, handle recurring bills, "
        "and import/export data. Dates use YYYY-MM-DD; amounts are positive numbers."
    ),
)

init_db()


# ═══════════════════════════════════════════════════════════════════════════════
# EXPENSE CRUD
# ═══════════════════════════════════════════════════════════════════════════════

@mcp.tool
def add_expense(
    amount: float,
    category: str,
    description: str = "",
    expense_date: str = "",
    currency: str = "USD",
    payment_method: str = "cash",
    tags: str = "",
    notes: str = "",
) -> dict:
    """
    Add a new expense entry.

    Args:
        amount: Positive expense amount.
        category: Category name or ID (see list_categories).
        description: Short label for the expense.
        expense_date: YYYY-MM-DD, defaults to today.
        currency: ISO-4217 code, e.g. USD, EUR, INR.
        payment_method: cash | credit_card | debit_card | bank_transfer | digital_wallet | check | other.
        tags: Comma-separated tags, e.g. "lunch,client".
        notes: Free-form notes.

    Returns:
        The created expense record.
    """
    if amount <= 0:
        return {"error": "amount must be greater than 0"}
    if payment_method not in VALID_PAYMENT_METHODS:
        return {"error": f"payment_method must be one of: {', '.join(VALID_PAYMENT_METHODS)}"}

    on_date = expense_date or datetime.now().strftime("%Y-%m-%d")
    try:
        datetime.strptime(on_date, "%Y-%m-%d")
    except ValueError:
        return {"error": "expense_date must be YYYY-MM-DD"}

    tags_json = json.dumps([t.strip() for t in tags.split(",") if t.strip()])

    with get_db() as conn:
        cat_id = resolve_category(conn, category)
        if cat_id is None:
            return {"error": f"Category '{category}' not found. Use list_categories() or add_category()."}

        cursor = conn.execute(
            """
            INSERT INTO expenses
                (amount, currency, category_id, description, date, payment_method, tags, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (amount, currency.upper(), cat_id, description, on_date, payment_method, tags_json, notes),
        )
        row = conn.execute(
            """
            SELECT e.*, c.name AS category_name, c.icon AS category_icon
            FROM expenses e LEFT JOIN categories c ON e.category_id = c.id
            WHERE e.id = ?
            """,
            (cursor.lastrowid,),
        ).fetchone()

        result = row_to_dict(row)
        result["tags"] = json.loads(result["tags"])
        return {"success": True, "expense": result}


@mcp.tool
def get_expense(expense_id: int) -> dict:
    """
    Retrieve a single expense by its ID.

    Args:
        expense_id: The expense ID.
    """
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT e.*, c.name AS category_name, c.icon AS category_icon
            FROM expenses e LEFT JOIN categories c ON e.category_id = c.id
            WHERE e.id = ?
            """,
            (expense_id,),
        ).fetchone()
        if not row:
            return {"error": f"Expense #{expense_id} not found"}
        result = row_to_dict(row)
        result["tags"] = json.loads(result["tags"])
        return result


@mcp.tool
def update_expense(
    expense_id: int,
    amount: Optional[float] = None,
    category: Optional[str] = None,
    description: Optional[str] = None,
    expense_date: Optional[str] = None,
    currency: Optional[str] = None,
    payment_method: Optional[str] = None,
    tags: Optional[str] = None,
    notes: Optional[str] = None,
) -> dict:
    """
    Update fields of an existing expense. Only supplied fields are changed.

    Args:
        expense_id: ID of the expense to update.
        amount: New positive amount.
        category: New category name or ID.
        description: New description.
        expense_date: New date YYYY-MM-DD.
        currency: New currency code.
        payment_method: New payment method.
        tags: Comma-separated tags (fully replaces existing tags).
        notes: New notes.
    """
    with get_db() as conn:
        if not conn.execute("SELECT id FROM expenses WHERE id = ?", (expense_id,)).fetchone():
            return {"error": f"Expense #{expense_id} not found"}

        updates: dict = {}

        if amount is not None:
            if amount <= 0:
                return {"error": "amount must be greater than 0"}
            updates["amount"] = amount

        if category is not None:
            cat_id = resolve_category(conn, category)
            if cat_id is None:
                return {"error": f"Category '{category}' not found"}
            updates["category_id"] = cat_id

        if description is not None:
            updates["description"] = description

        if expense_date is not None:
            try:
                datetime.strptime(expense_date, "%Y-%m-%d")
            except ValueError:
                return {"error": "expense_date must be YYYY-MM-DD"}
            updates["date"] = expense_date

        if currency is not None:
            updates["currency"] = currency.upper()

        if payment_method is not None:
            if payment_method not in VALID_PAYMENT_METHODS:
                return {"error": f"payment_method must be one of: {', '.join(VALID_PAYMENT_METHODS)}"}
            updates["payment_method"] = payment_method

        if tags is not None:
            updates["tags"] = json.dumps([t.strip() for t in tags.split(",") if t.strip()])

        if notes is not None:
            updates["notes"] = notes

        if not updates:
            return {"error": "No fields provided to update"}

        updates["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        conn.execute(
            f"UPDATE expenses SET {set_clause} WHERE id = ?",
            [*updates.values(), expense_id],
        )

        row = conn.execute(
            """
            SELECT e.*, c.name AS category_name, c.icon AS category_icon
            FROM expenses e LEFT JOIN categories c ON e.category_id = c.id
            WHERE e.id = ?
            """,
            (expense_id,),
        ).fetchone()
        result = row_to_dict(row)
        result["tags"] = json.loads(result["tags"])
        return {"success": True, "expense": result}


@mcp.tool
def delete_expense(expense_id: int) -> dict:
    """
    Permanently delete an expense.

    Args:
        expense_id: The expense ID to delete.
    """
    with get_db() as conn:
        row = conn.execute(
            "SELECT id, amount, description FROM expenses WHERE id = ?", (expense_id,)
        ).fetchone()
        if not row:
            return {"error": f"Expense #{expense_id} not found"}
        conn.execute("DELETE FROM expenses WHERE id = ?", (expense_id,))
        return {
            "success": True,
            "message": (
                f"Deleted expense #{expense_id} — "
                f"${row['amount']:.2f} ({row['description'] or 'no description'})"
            ),
        }


@mcp.tool
def list_expenses(
    start_date: str = "",
    end_date: str = "",
    category: str = "",
    min_amount: float = 0.0,
    max_amount: float = 0.0,
    payment_method: str = "",
    search: str = "",
    tags: str = "",
    currency: str = "",
    sort_by: str = "date",
    sort_order: str = "desc",
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """
    List expenses with optional filtering and pagination.

    Args:
        start_date: Inclusive start date YYYY-MM-DD.
        end_date: Inclusive end date YYYY-MM-DD.
        category: Filter by category name or ID.
        min_amount: Minimum amount (0 = no lower bound).
        max_amount: Maximum amount (0 = no upper bound).
        payment_method: Filter by payment method.
        search: Substring search across description and notes.
        tags: Comma-separated tags; expenses must contain ALL listed tags.
        currency: Filter by currency code.
        sort_by: date | amount | created_at.
        sort_order: asc | desc.
        limit: Max rows to return (capped at 500).
        offset: Pagination offset.
    """
    limit     = min(max(limit, 1), 500)
    sort_col  = sort_by if sort_by in VALID_SORT_FIELDS else "date"
    sort_dir  = "ASC" if sort_order.lower() == "asc" else "DESC"

    conditions: list[str] = []
    params: list = []

    if start_date:
        conditions.append("e.date >= ?"); params.append(start_date)
    if end_date:
        conditions.append("e.date <= ?"); params.append(end_date)
    if min_amount > 0:
        conditions.append("e.amount >= ?"); params.append(min_amount)
    if max_amount > 0:
        conditions.append("e.amount <= ?"); params.append(max_amount)
    if payment_method:
        conditions.append("e.payment_method = ?"); params.append(payment_method)
    if currency:
        conditions.append("e.currency = ?"); params.append(currency.upper())
    if search:
        conditions.append("(e.description LIKE ? OR e.notes LIKE ?)")
        params += [f"%{search}%", f"%{search}%"]

    with get_db() as conn:
        if category:
            cat_id = resolve_category(conn, category)
            if cat_id is None:
                return {"error": f"Category '{category}' not found"}
            conditions.append("e.category_id = ?"); params.append(cat_id)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        total_count = conn.execute(
            f"SELECT COUNT(*) FROM expenses e {where}", params
        ).fetchone()[0]

        rows = conn.execute(
            f"""
            SELECT e.*, c.name AS category_name, c.icon AS category_icon
            FROM expenses e LEFT JOIN categories c ON e.category_id = c.id
            {where}
            ORDER BY e.{sort_col} {sort_dir}
            LIMIT ? OFFSET ?
            """,
            params + [limit, offset],
        ).fetchall()

        filter_tags = (
            {t.strip().lower() for t in tags.split(",") if t.strip()} if tags else set()
        )

        expenses = []
        for row in rows:
            item = row_to_dict(row)
            item["tags"] = json.loads(item["tags"])
            if filter_tags and not filter_tags.issubset({t.lower() for t in item["tags"]}):
                continue
            expenses.append(item)

        return {
            "expenses":       expenses,
            "returned_count": len(expenses),
            "total_count":    total_count,
            "total_amount":   round(sum(e["amount"] for e in expenses), 2),
            "offset":         offset,
            "limit":          limit,
        }


# ═══════════════════════════════════════════════════════════════════════════════
# CATEGORY MANAGEMENT
# ═══════════════════════════════════════════════════════════════════════════════

@mcp.tool
def add_category(
    name: str,
    color: str = "#808080",
    icon: str = "📌",
    budget_limit: float = 0.0,
) -> dict:
    """
    Create a new expense category.

    Args:
        name: Unique category name.
        color: Hex color, e.g. #FF6B6B.
        icon: Emoji icon.
        budget_limit: Optional monthly spending cap (0 = no limit).
    """
    with get_db() as conn:
        if conn.execute(
            "SELECT id FROM categories WHERE LOWER(name) = LOWER(?)", (name,)
        ).fetchone():
            return {"error": f"Category '{name}' already exists"}

        cursor = conn.execute(
            "INSERT INTO categories (name, color, icon, budget_limit) VALUES (?, ?, ?, ?)",
            (name, color, icon, budget_limit if budget_limit > 0 else None),
        )
        row = conn.execute("SELECT * FROM categories WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return {"success": True, "category": row_to_dict(row)}


@mcp.tool
def list_categories(include_spending: bool = False) -> dict:
    """
    List all categories.

    Args:
        include_spending: When True, adds current-month spending and budget utilisation.
    """
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM categories ORDER BY name").fetchall()
        categories = [row_to_dict(r) for r in rows]

        if include_spending:
            month_start = datetime.now().strftime("%Y-%m-01")
            for cat in categories:
                spent = conn.execute(
                    "SELECT COALESCE(SUM(amount), 0) FROM expenses WHERE category_id = ? AND date >= ?",
                    (cat["id"], month_start),
                ).fetchone()[0]
                cat["current_month_spending"] = round(spent, 2)
                if cat["budget_limit"]:
                    cat["budget_remaining"]      = round(cat["budget_limit"] - spent, 2)
                    cat["budget_utilization_pct"] = round((spent / cat["budget_limit"]) * 100, 1)

        return {"categories": categories, "total": len(categories)}


@mcp.tool
def update_category(
    category: str,
    name: Optional[str] = None,
    color: Optional[str] = None,
    icon: Optional[str] = None,
    budget_limit: Optional[float] = None,
) -> dict:
    """
    Update an existing category's fields.

    Args:
        category: Category name or ID to update.
        name: New unique name.
        color: New hex color.
        icon: New emoji icon.
        budget_limit: New monthly limit (-1 to remove the limit).
    """
    with get_db() as conn:
        cat_id = resolve_category(conn, category)
        if cat_id is None:
            return {"error": f"Category '{category}' not found"}

        updates: dict = {}
        if name         is not None: updates["name"]         = name
        if color        is not None: updates["color"]        = color
        if icon         is not None: updates["icon"]         = icon
        if budget_limit is not None:
            updates["budget_limit"] = None if budget_limit == -1 else budget_limit

        if not updates:
            return {"error": "No fields provided to update"}

        set_clause = ", ".join(f"{k} = ?" for k in updates)
        conn.execute(
            f"UPDATE categories SET {set_clause} WHERE id = ?", [*updates.values(), cat_id]
        )
        row = conn.execute("SELECT * FROM categories WHERE id = ?", (cat_id,)).fetchone()
        return {"success": True, "category": row_to_dict(row)}


@mcp.tool
def delete_category(category: str, reassign_to: str = "") -> dict:
    """
    Delete a category. Expenses become uncategorised unless reassign_to is specified.

    Args:
        category: Category name or ID to delete.
        reassign_to: Optional category to move affected expenses into first.
    """
    with get_db() as conn:
        cat_id = resolve_category(conn, category)
        if cat_id is None:
            return {"error": f"Category '{category}' not found"}

        cat_name = conn.execute("SELECT name FROM categories WHERE id = ?", (cat_id,)).fetchone()["name"]
        expense_count = conn.execute(
            "SELECT COUNT(*) FROM expenses WHERE category_id = ?", (cat_id,)
        ).fetchone()[0]

        if reassign_to:
            new_id = resolve_category(conn, reassign_to)
            if new_id is None:
                return {"error": f"Reassign target '{reassign_to}' not found"}
            conn.execute(
                "UPDATE expenses SET category_id = ? WHERE category_id = ?", (new_id, cat_id)
            )

        conn.execute("DELETE FROM categories WHERE id = ?", (cat_id,))

        msg = f"Category '{cat_name}' deleted."
        if expense_count and not reassign_to:
            msg += f" {expense_count} expense(s) are now uncategorised."
        elif expense_count and reassign_to:
            msg += f" {expense_count} expense(s) moved to '{reassign_to}'."

        return {"success": True, "message": msg}


# ═══════════════════════════════════════════════════════════════════════════════
# BUDGET MANAGEMENT
# ═══════════════════════════════════════════════════════════════════════════════

@mcp.tool
def set_budget(
    category: str,
    amount: float,
    period: str = "monthly",
    year: int = 0,
    month: int = 0,
) -> dict:
    """
    Set (or replace) a budget for a category. Creates or overwrites the existing value.

    Args:
        category: Category name or ID.
        amount: Positive budget amount.
        period: monthly | yearly.
        year: Budget year (defaults to current year).
        month: Budget month 1–12 (monthly period only; defaults to current month).
    """
    if amount <= 0:
        return {"error": "Budget amount must be greater than 0"}
    if period not in VALID_PERIODS:
        return {"error": f"period must be one of: {', '.join(VALID_PERIODS)}"}

    now   = datetime.now()
    year  = year  or now.year
    month = (month or now.month) if period == "monthly" else None

    if month is not None and not (1 <= month <= 12):
        return {"error": "month must be between 1 and 12"}

    with get_db() as conn:
        cat_id = resolve_category(conn, category)
        if cat_id is None:
            return {"error": f"Category '{category}' not found"}

        conn.execute(
            """
            INSERT INTO budgets (category_id, amount, period, year, month)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(category_id, period, year, month)
            DO UPDATE SET amount = excluded.amount
            """,
            (cat_id, amount, period, year, month),
        )
        cat = conn.execute("SELECT name, icon FROM categories WHERE id = ?", (cat_id,)).fetchone()
        return {
            "success": True,
            "budget": {
                "category": cat["name"],
                "icon":     cat["icon"],
                "amount":   amount,
                "period":   period,
                "year":     year,
                "month":    month,
            },
        }


@mcp.tool
def get_budget_status(year: int = 0, month: int = 0) -> dict:
    """
    Budget vs. actual spending for every category in a given month.

    Args:
        year: Year to check (defaults to current year).
        month: Month 1–12 (defaults to current month).
    """
    now   = datetime.now()
    year  = year  or now.year
    month = month or now.month

    month_start = f"{year}-{month:02d}-01"
    if month == 12:
        month_end = f"{year + 1}-01-01"
    else:
        month_end = f"{year}-{month + 1:02d}-01"

    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT
                c.id, c.name, c.icon,
                b.amount AS budget,
                COALESCE(SUM(e.amount), 0) AS spent
            FROM categories c
            LEFT JOIN budgets b
                ON b.category_id = c.id AND b.period = 'monthly'
                AND b.year = ? AND b.month = ?
            LEFT JOIN expenses e
                ON e.category_id = c.id AND e.date >= ? AND e.date < ?
            GROUP BY c.id
            ORDER BY spent DESC
            """,
            (year, month, month_start, month_end),
        ).fetchall()

        total_budget = 0.0
        total_spent  = 0.0
        categories   = []

        for row in rows:
            spent  = round(row["spent"], 2)
            budget = row["budget"]
            if spent == 0 and budget is None:
                continue

            entry = {"category": row["name"], "icon": row["icon"], "spent": spent, "budget": budget}
            if budget:
                remaining  = round(budget - spent, 2)
                util_pct   = round((spent / budget) * 100, 1)
                entry.update({
                    "remaining":       remaining,
                    "utilization_pct": util_pct,
                    "status": (
                        "over_budget" if spent > budget
                        else ("warning" if util_pct >= 80 else "ok")
                    ),
                })
                total_budget += budget
            total_spent += spent
            categories.append(entry)

        return {
            "year":                    year,
            "month":                   month,
            "total_budget":            round(total_budget, 2),
            "total_spent":             round(total_spent, 2),
            "total_remaining":         round(total_budget - total_spent, 2) if total_budget else None,
            "overall_utilization_pct": round((total_spent / total_budget) * 100, 1) if total_budget else None,
            "categories":              categories,
        }


@mcp.tool
def list_budgets() -> dict:
    """List all defined budgets across all categories and periods."""
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT b.*, c.name AS category_name, c.icon AS category_icon
            FROM budgets b JOIN categories c ON b.category_id = c.id
            ORDER BY b.year DESC, b.month DESC, c.name
            """,
        ).fetchall()
        return {"budgets": [row_to_dict(r) for r in rows], "total": len(rows)}


# ═══════════════════════════════════════════════════════════════════════════════
# ANALYTICS & REPORTS
# ═══════════════════════════════════════════════════════════════════════════════

@mcp.tool
def get_spending_summary(
    start_date: str = "",
    end_date: str = "",
    group_by: str = "category",
) -> dict:
    """
    Spending summary grouped by category, month, day, or payment method.

    Args:
        start_date: YYYY-MM-DD (defaults to first day of current month).
        end_date:   YYYY-MM-DD (defaults to today).
        group_by:   category | month | day | payment_method.
    """
    now        = datetime.now()
    start_date = start_date or now.strftime("%Y-%m-01")
    end_date   = end_date   or now.strftime("%Y-%m-%d")

    VALID_GROUPS = ("category", "month", "day", "payment_method")
    if group_by not in VALID_GROUPS:
        return {"error": f"group_by must be one of: {', '.join(VALID_GROUPS)}"}

    queries = {
        "category": (
            """
            SELECT c.name AS label, c.icon,
                   COUNT(e.id) AS count,
                   ROUND(SUM(e.amount), 2) AS total,
                   ROUND(AVG(e.amount), 2) AS avg,
                   ROUND(MIN(e.amount), 2) AS min,
                   ROUND(MAX(e.amount), 2) AS max
            FROM expenses e LEFT JOIN categories c ON e.category_id = c.id
            WHERE e.date BETWEEN ? AND ?
            GROUP BY e.category_id ORDER BY total DESC
            """
        ),
        "month": (
            """
            SELECT strftime('%Y-%m', date) AS label,
                   COUNT(*) AS count,
                   ROUND(SUM(amount), 2) AS total,
                   ROUND(AVG(amount), 2) AS avg,
                   ROUND(MIN(amount), 2) AS min,
                   ROUND(MAX(amount), 2) AS max
            FROM expenses WHERE date BETWEEN ? AND ?
            GROUP BY strftime('%Y-%m', date) ORDER BY label
            """
        ),
        "day": (
            """
            SELECT date AS label,
                   COUNT(*) AS count,
                   ROUND(SUM(amount), 2) AS total,
                   ROUND(AVG(amount), 2) AS avg,
                   ROUND(MIN(amount), 2) AS min,
                   ROUND(MAX(amount), 2) AS max
            FROM expenses WHERE date BETWEEN ? AND ?
            GROUP BY date ORDER BY date
            """
        ),
        "payment_method": (
            """
            SELECT payment_method AS label,
                   COUNT(*) AS count,
                   ROUND(SUM(amount), 2) AS total,
                   ROUND(AVG(amount), 2) AS avg,
                   ROUND(MIN(amount), 2) AS min,
                   ROUND(MAX(amount), 2) AS max
            FROM expenses WHERE date BETWEEN ? AND ?
            GROUP BY payment_method ORDER BY total DESC
            """
        ),
    }

    with get_db() as conn:
        rows = conn.execute(queries[group_by], (start_date, end_date)).fetchall()
        data        = [row_to_dict(r) for r in rows]
        grand_total = sum(r["total"] for r in data)
        for r in data:
            r["pct_of_total"] = round((r["total"] / grand_total) * 100, 1) if grand_total else 0.0

        return {
            "start_date":  start_date,
            "end_date":    end_date,
            "group_by":    group_by,
            "grand_total": round(grand_total, 2),
            "data":        data,
        }


@mcp.tool
def get_monthly_report(year: int = 0, month: int = 0) -> dict:
    """
    Comprehensive breakdown for a specific calendar month.

    Args:
        year:  Year (defaults to current year).
        month: Month 1–12 (defaults to current month).
    """
    now   = datetime.now()
    year  = year  or now.year
    month = month or now.month

    month_start = f"{year}-{month:02d}-01"
    if month == 12:
        month_end        = f"{year + 1}-01-01"
        prev_month_start = f"{year}-11-01"
        prev_month_end   = f"{year}-12-01"
    else:
        month_end     = f"{year}-{month + 1:02d}-01"
        prev_y        = year if month > 1 else year - 1
        prev_m        = month - 1 if month > 1 else 12
        prev_month_start = f"{prev_y}-{prev_m:02d}-01"
        prev_month_end   = month_start

    with get_db() as conn:
        stats = conn.execute(
            """
            SELECT COUNT(*) AS count,
                   COALESCE(ROUND(SUM(amount),2),0) AS total,
                   COALESCE(ROUND(AVG(amount),2),0) AS avg_per_expense,
                   COALESCE(ROUND(MIN(amount),2),0) AS min_expense,
                   COALESCE(ROUND(MAX(amount),2),0) AS max_expense
            FROM expenses WHERE date >= ? AND date < ?
            """,
            (month_start, month_end),
        ).fetchone()

        prev_total = conn.execute(
            "SELECT COALESCE(ROUND(SUM(amount),2),0) FROM expenses WHERE date >= ? AND date < ?",
            (prev_month_start, prev_month_end),
        ).fetchone()[0]

        by_category = conn.execute(
            """
            SELECT c.name, c.icon, COUNT(e.id) AS count, ROUND(SUM(e.amount),2) AS total
            FROM expenses e LEFT JOIN categories c ON e.category_id = c.id
            WHERE e.date >= ? AND e.date < ?
            GROUP BY e.category_id ORDER BY total DESC
            """,
            (month_start, month_end),
        ).fetchall()

        by_payment = conn.execute(
            """
            SELECT payment_method, COUNT(*) AS count, ROUND(SUM(amount),2) AS total
            FROM expenses WHERE date >= ? AND date < ?
            GROUP BY payment_method ORDER BY total DESC
            """,
            (month_start, month_end),
        ).fetchall()

        top_expenses = conn.execute(
            """
            SELECT e.id, e.amount, e.description, e.date, e.payment_method,
                   c.name AS category_name, c.icon
            FROM expenses e LEFT JOIN categories c ON e.category_id = c.id
            WHERE e.date >= ? AND e.date < ?
            ORDER BY e.amount DESC LIMIT 5
            """,
            (month_start, month_end),
        ).fetchall()

        busiest_day = conn.execute(
            """
            SELECT date, ROUND(SUM(amount),2) AS total, COUNT(*) AS count
            FROM expenses WHERE date >= ? AND date < ?
            GROUP BY date ORDER BY total DESC LIMIT 1
            """,
            (month_start, month_end),
        ).fetchone()

    curr_total = stats["total"]
    mom_change = (
        round(((curr_total - prev_total) / prev_total) * 100, 1)
        if prev_total else None
    )
    days_in_month = calendar.monthrange(year, month)[1]

    return {
        "period": f"{calendar.month_name[month]} {year}",
        "year":   year,
        "month":  month,
        "summary": {
            "total_spent":      curr_total,
            "expense_count":    stats["count"],
            "avg_per_expense":  stats["avg_per_expense"],
            "min_expense":      stats["min_expense"],
            "max_expense":      stats["max_expense"],
            "avg_daily_spend":  round(curr_total / days_in_month, 2),
            "prev_month_total": prev_total,
            "mom_change_pct":   mom_change,
        },
        "by_category":       [row_to_dict(r) for r in by_category],
        "by_payment_method": [row_to_dict(r) for r in by_payment],
        "top_5_expenses":    [row_to_dict(r) for r in top_expenses],
        "busiest_day":       row_to_dict(busiest_day) if busiest_day else None,
    }


@mcp.tool
def get_spending_trend(months_back: int = 6) -> dict:
    """
    Month-over-month spending trend.

    Args:
        months_back: How many months to include (default 6, max 24).
    """
    months_back = min(max(months_back, 1), 24)
    now = datetime.now()

    periods = []
    for i in range(months_back - 1, -1, -1):
        m = now.month - i
        y = now.year
        while m <= 0:
            m += 12
            y -= 1
        periods.append((y, m))

    with get_db() as conn:
        trend = []
        for y, m in periods:
            ms  = f"{y}-{m:02d}-01"
            me  = f"{y + 1 if m == 12 else y}-{1 if m == 12 else m + 1:02d}-01"
            row = conn.execute(
                """
                SELECT COUNT(*) AS count,
                       COALESCE(ROUND(SUM(amount),2),0) AS total,
                       COALESCE(ROUND(AVG(amount),2),0) AS avg
                FROM expenses WHERE date >= ? AND date < ?
                """,
                (ms, me),
            ).fetchone()
            trend.append({
                "period":          f"{y}-{m:02d}",
                "month_name":      calendar.month_abbr[m],
                "year":            y,
                "month":           m,
                "total":           row["total"],
                "count":           row["count"],
                "avg_per_expense": row["avg"],
            })

        for i in range(1, len(trend)):
            prev = trend[i - 1]["total"]
            curr = trend[i]["total"]
            trend[i]["mom_change"]     = round(curr - prev, 2)
            trend[i]["mom_change_pct"] = round(((curr - prev) / prev) * 100, 1) if prev else None

        totals = [t["total"] for t in trend]
        return {
            "months_back":       months_back,
            "trend":             trend,
            "avg_monthly_spend": round(sum(totals) / len(totals), 2) if totals else 0,
            "highest_month":     max(trend, key=lambda x: x["total"])["period"] if trend else None,
            "lowest_month":      min(trend, key=lambda x: x["total"])["period"] if trend else None,
        }


@mcp.tool
def get_expense_stats(start_date: str = "", end_date: str = "") -> dict:
    """
    Statistical analysis (mean, median, percentiles, etc.) for a date range.

    Args:
        start_date: YYYY-MM-DD (defaults to first day of current month).
        end_date:   YYYY-MM-DD (defaults to today).
    """
    now        = datetime.now()
    start_date = start_date or now.strftime("%Y-%m-01")
    end_date   = end_date   or now.strftime("%Y-%m-%d")

    with get_db() as conn:
        agg = conn.execute(
            """
            SELECT COUNT(*) AS total_count,
                   COALESCE(ROUND(SUM(amount),2),0)  AS total_amount,
                   COALESCE(ROUND(AVG(amount),2),0)  AS mean,
                   COALESCE(ROUND(MIN(amount),2),0)  AS minimum,
                   COALESCE(ROUND(MAX(amount),2),0)  AS maximum,
                   COUNT(DISTINCT category_id)        AS unique_categories,
                   COUNT(DISTINCT date)               AS active_days,
                   COUNT(DISTINCT payment_method)     AS payment_methods_used
            FROM expenses WHERE date BETWEEN ? AND ?
            """,
            (start_date, end_date),
        ).fetchone()

        amounts = [
            r[0] for r in conn.execute(
                "SELECT amount FROM expenses WHERE date BETWEEN ? AND ? ORDER BY amount",
                (start_date, end_date),
            ).fetchall()
        ]

    n      = len(amounts)
    median = round(
        amounts[n // 2] if n % 2 else (amounts[n // 2 - 1] + amounts[n // 2]) / 2, 2
    ) if n else 0

    def pct(p: int) -> float:
        return round(amounts[max(0, int(n * p / 100) - 1)], 2) if n else 0

    try:
        d1   = datetime.strptime(start_date, "%Y-%m-%d")
        d2   = datetime.strptime(end_date,   "%Y-%m-%d")
        days = max((d2 - d1).days + 1, 1)
    except ValueError:
        days = 30

    result = dict(agg)
    result.update({
        "median":        median,
        "p25":           pct(25),
        "p75":           pct(75),
        "p90":           pct(90),
        "daily_average": round(result["total_amount"] / days, 2),
        "start_date":    start_date,
        "end_date":      end_date,
    })
    return result


@mcp.tool
def get_financial_overview() -> dict:
    """
    High-level financial snapshot: current-month totals, YTD, budget alerts,
    top categories, and the 5 most recent expenses.
    """
    now         = datetime.now()
    today       = now.strftime("%Y-%m-%d")
    month_start = now.strftime("%Y-%m-01")
    year_start  = now.strftime("%Y-01-01")

    with get_db() as conn:
        month_agg = conn.execute(
            """
            SELECT COUNT(*) AS count, COALESCE(ROUND(SUM(amount),2),0) AS total
            FROM expenses WHERE date >= ? AND date <= ?
            """,
            (month_start, today),
        ).fetchone()

        ytd_total = conn.execute(
            "SELECT COALESCE(ROUND(SUM(amount),2),0) FROM expenses WHERE date >= ? AND date <= ?",
            (year_start, today),
        ).fetchone()[0]

        alerts = conn.execute(
            """
            SELECT c.name, c.icon, b.amount AS budget,
                   COALESCE(SUM(e.amount),0) AS spent
            FROM budgets b
            JOIN categories c ON b.category_id = c.id
            LEFT JOIN expenses e
                ON e.category_id = b.category_id AND e.date >= ? AND e.date <= ?
            WHERE b.period = 'monthly' AND b.year = ? AND b.month = ?
            GROUP BY b.id
            HAVING (spent / b.amount) >= 0.8
            ORDER BY (spent / b.amount) DESC
            """,
            (month_start, today, now.year, now.month),
        ).fetchall()

        top_cats = conn.execute(
            """
            SELECT c.name, c.icon, ROUND(SUM(e.amount),2) AS total
            FROM expenses e JOIN categories c ON e.category_id = c.id
            WHERE e.date >= ? AND e.date <= ?
            GROUP BY e.category_id ORDER BY total DESC LIMIT 5
            """,
            (month_start, today),
        ).fetchall()

        recent = conn.execute(
            """
            SELECT e.id, e.amount, e.description, e.date,
                   c.name AS category, c.icon
            FROM expenses e LEFT JOIN categories c ON e.category_id = c.id
            ORDER BY e.created_at DESC LIMIT 5
            """,
        ).fetchall()

    days_in_month = calendar.monthrange(now.year, now.month)[1]
    projected     = round(
        (month_agg["total"] / now.day) * days_in_month, 2
    ) if now.day else 0

    budget_alerts = [
        {
            "category":        r["name"],
            "icon":            r["icon"],
            "budget":          r["budget"],
            "spent":           round(r["spent"], 2),
            "utilization_pct": round((r["spent"] / r["budget"]) * 100, 1),
            "status":          "over_budget" if r["spent"] > r["budget"] else "warning",
        }
        for r in alerts
    ]

    return {
        "as_of": today,
        "current_month": {
            "total_spent":             month_agg["total"],
            "expense_count":           month_agg["count"],
            "days_elapsed":            now.day,
            "projected_month_total":   projected,
        },
        "year_to_date":                {"total_spent": ytd_total},
        "budget_alerts":               budget_alerts,
        "top_categories_this_month":   [row_to_dict(r) for r in top_cats],
        "recent_expenses":             [row_to_dict(r) for r in recent],
    }


# ═══════════════════════════════════════════════════════════════════════════════
# RECURRING EXPENSES
# ═══════════════════════════════════════════════════════════════════════════════

@mcp.tool
def add_recurring_expense(
    amount: float,
    category: str,
    description: str,
    frequency: str = "monthly",
    start_date: str = "",
    currency: str = "USD",
    payment_method: str = "cash",
    tags: str = "",
) -> dict:
    """
    Register a recurring expense (rent, subscriptions, EMIs, etc.).

    Args:
        amount: Recurring amount.
        category: Category name or ID.
        description: What this charge is for.
        frequency: daily | weekly | biweekly | monthly | yearly.
        start_date: First occurrence YYYY-MM-DD (defaults to today).
        currency: Currency code.
        payment_method: Payment method.
        tags: Comma-separated tags.
    """
    if amount <= 0:
        return {"error": "amount must be greater than 0"}
    if frequency not in VALID_FREQUENCIES:
        return {"error": f"frequency must be one of: {', '.join(VALID_FREQUENCIES)}"}
    if payment_method not in VALID_PAYMENT_METHODS:
        return {"error": f"payment_method must be one of: {', '.join(VALID_PAYMENT_METHODS)}"}

    next_due  = start_date or datetime.now().strftime("%Y-%m-%d")
    tags_json = json.dumps([t.strip() for t in tags.split(",") if t.strip()])

    with get_db() as conn:
        cat_id = resolve_category(conn, category)
        if cat_id is None:
            return {"error": f"Category '{category}' not found"}

        cursor = conn.execute(
            """
            INSERT INTO recurring_expenses
                (amount, currency, category_id, description, frequency,
                 next_due, payment_method, tags)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (amount, currency.upper(), cat_id, description, frequency,
             next_due, payment_method, tags_json),
        )
        row = conn.execute(
            """
            SELECT r.*, c.name AS category_name, c.icon AS category_icon
            FROM recurring_expenses r LEFT JOIN categories c ON r.category_id = c.id
            WHERE r.id = ?
            """,
            (cursor.lastrowid,),
        ).fetchone()
        result = row_to_dict(row)
        result["tags"] = json.loads(result["tags"])
        return {"success": True, "recurring_expense": result}


@mcp.tool
def list_recurring_expenses(active_only: bool = True) -> dict:
    """
    List recurring expenses.

    Args:
        active_only: When True (default) only active entries are returned.
    """
    with get_db() as conn:
        where = "WHERE r.is_active = 1" if active_only else ""
        rows  = conn.execute(
            f"""
            SELECT r.*, c.name AS category_name, c.icon AS category_icon
            FROM recurring_expenses r LEFT JOIN categories c ON r.category_id = c.id
            {where}
            ORDER BY r.next_due ASC
            """,
        ).fetchall()

        today   = datetime.now().strftime("%Y-%m-%d")
        results = []
        for row in rows:
            item = row_to_dict(row)
            item["tags"]       = json.loads(item["tags"])
            item["is_overdue"] = item["next_due"] < today
            results.append(item)

        return {"recurring_expenses": results, "total": len(results)}


@mcp.tool
def process_due_recurring_expenses() -> dict:
    """
    Create expense entries for all overdue recurring items and advance their next-due dates.
    Run this daily (or hook into a scheduler) to keep the ledger current.
    """
    today = datetime.now().strftime("%Y-%m-%d")

    with get_db() as conn:
        due_rows = conn.execute(
            """
            SELECT r.*, c.name AS category_name
            FROM recurring_expenses r LEFT JOIN categories c ON r.category_id = c.id
            WHERE r.is_active = 1 AND r.next_due <= ?
            """,
            (today,),
        ).fetchall()

        created = []
        for item in due_rows:
            cursor = conn.execute(
                """
                INSERT INTO expenses
                    (amount, currency, category_id, description, date, payment_method, tags)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item["amount"], item["currency"], item["category_id"],
                    f"[Recurring] {item['description']}", item["next_due"],
                    item["payment_method"], item["tags"],
                ),
            )
            new_due = next_due_after(item["next_due"], item["frequency"])
            conn.execute(
                "UPDATE recurring_expenses SET next_due = ? WHERE id = ?",
                (new_due, item["id"]),
            )
            created.append({
                "recurring_id": item["id"],
                "expense_id":   cursor.lastrowid,
                "description":  item["description"],
                "amount":       item["amount"],
                "next_due":     new_due,
            })

        return {
            "processed":        len(created),
            "expenses_created": created,
            "message":          f"{len(created)} recurring expense(s) processed for {today}",
        }


@mcp.tool
def toggle_recurring_expense(recurring_id: int, active: bool = True) -> dict:
    """
    Activate or pause a recurring expense.

    Args:
        recurring_id: The recurring expense ID.
        active: True to activate, False to pause.
    """
    with get_db() as conn:
        row = conn.execute(
            "SELECT description FROM recurring_expenses WHERE id = ?", (recurring_id,)
        ).fetchone()
        if not row:
            return {"error": f"Recurring expense #{recurring_id} not found"}
        conn.execute(
            "UPDATE recurring_expenses SET is_active = ? WHERE id = ?",
            (1 if active else 0, recurring_id),
        )
        return {
            "success": True,
            "message": f"'{row['description']}' {'activated' if active else 'paused'}",
        }


# ═══════════════════════════════════════════════════════════════════════════════
# SEARCH & TAGS
# ═══════════════════════════════════════════════════════════════════════════════

@mcp.tool
def search_expenses(query: str, limit: int = 20) -> dict:
    """
    Full-text search across expense descriptions, notes, and tags.

    Args:
        query: Search string.
        limit: Max results (default 20).
    """
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT e.*, c.name AS category_name, c.icon AS category_icon
            FROM expenses e LEFT JOIN categories c ON e.category_id = c.id
            WHERE e.description LIKE ? OR e.notes LIKE ? OR e.tags LIKE ?
            ORDER BY e.date DESC LIMIT ?
            """,
            (f"%{query}%", f"%{query}%", f"%{query}%", limit),
        ).fetchall()

        results = []
        for row in rows:
            item = row_to_dict(row)
            item["tags"] = json.loads(item["tags"])
            results.append(item)

        return {"query": query, "results": results, "count": len(results)}


@mcp.tool
def get_expenses_by_tag(tag: str, limit: int = 50) -> dict:
    """
    Fetch all expenses carrying a specific tag.

    Args:
        tag: Exact tag string to match.
        limit: Max results.
    """
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT e.*, c.name AS category_name, c.icon AS category_icon
            FROM expenses e LEFT JOIN categories c ON e.category_id = c.id
            WHERE e.tags LIKE ?
            ORDER BY e.date DESC LIMIT ?
            """,
            (f'%"{tag}"%', limit),
        ).fetchall()

        results = []
        for row in rows:
            item = row_to_dict(row)
            item["tags"] = json.loads(item["tags"])
            results.append(item)

        return {
            "tag":          tag,
            "expenses":     results,
            "count":        len(results),
            "total_amount": round(sum(r["amount"] for r in results), 2),
        }


# ═══════════════════════════════════════════════════════════════════════════════
# IMPORT / EXPORT
# ═══════════════════════════════════════════════════════════════════════════════

@mcp.tool
def export_expenses_csv(start_date: str = "", end_date: str = "") -> dict:
    """
    Export expenses as a CSV string suitable for spreadsheets.

    Args:
        start_date: YYYY-MM-DD (defaults to all time).
        end_date:   YYYY-MM-DD (defaults to today).
    """
    end_date = end_date or datetime.now().strftime("%Y-%m-%d")
    conditions = ["e.date <= ?"]
    params: list = [end_date]
    if start_date:
        conditions.append("e.date >= ?")
        params.append(start_date)

    where = f"WHERE {' AND '.join(conditions)}"

    with get_db() as conn:
        rows = conn.execute(
            f"""
            SELECT e.id, e.date, e.amount, e.currency,
                   c.name AS category, e.description,
                   e.payment_method, e.tags, e.notes, e.created_at
            FROM expenses e LEFT JOIN categories c ON e.category_id = c.id
            {where}
            ORDER BY e.date ASC
            """,
            params,
        ).fetchall()

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow([
            "id", "date", "amount", "currency", "category",
            "description", "payment_method", "tags", "notes", "created_at",
        ])
        for row in rows:
            r    = row_to_dict(row)
            tags = ";".join(json.loads(r["tags"]))
            writer.writerow([
                r["id"], r["date"], r["amount"], r["currency"],
                r["category"], r["description"], r["payment_method"],
                tags, r["notes"], r["created_at"],
            ])

        return {
            "csv_data":  output.getvalue(),
            "row_count": len(rows),
            "start_date": start_date or "all time",
            "end_date":   end_date,
        }


@mcp.tool
def import_expenses_csv(csv_data: str, dry_run: bool = False) -> dict:
    """
    Import expenses from CSV text.

    Required columns: date, amount, category
    Optional columns: currency, description, payment_method, tags (semicolon-separated), notes

    Args:
        csv_data: Raw CSV content as a string.
        dry_run:  When True, validate only — nothing is written.
    """
    reader = csv.DictReader(io.StringIO(csv_data))
    errors: list[str] = []
    valid_rows: list[dict] = []

    for i, row in enumerate(reader, start=1):
        missing = {"date", "amount", "category"} - set(row.keys())
        if missing:
            errors.append(f"Row {i}: missing columns {missing}")
            continue
        try:
            amt = float(row["amount"])
            if amt <= 0:
                raise ValueError
        except ValueError:
            errors.append(f"Row {i}: invalid amount '{row.get('amount')}'")
            continue
        try:
            datetime.strptime(row["date"], "%Y-%m-%d")
        except ValueError:
            errors.append(f"Row {i}: invalid date '{row.get('date')}'")
            continue
        valid_rows.append(row)

    if dry_run:
        return {"dry_run": True, "valid_rows": len(valid_rows), "errors": errors}

    imported = 0
    import_errors = list(errors)

    with get_db() as conn:
        for i, row in enumerate(valid_rows, start=1):
            cat_id = resolve_category(conn, row["category"])
            if cat_id is None:
                import_errors.append(f"Row {i}: category '{row['category']}' not found — skipped")
                continue
            tags_raw = row.get("tags", "")
            tags_json = json.dumps([t.strip() for t in tags_raw.split(";") if t.strip()])
            conn.execute(
                """
                INSERT INTO expenses
                    (amount, currency, category_id, description, date, payment_method, tags, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    float(row["amount"]),
                    row.get("currency", "USD").upper(),
                    cat_id,
                    row.get("description", ""),
                    row["date"],
                    row.get("payment_method", "cash"),
                    tags_json,
                    row.get("notes", ""),
                ),
            )
            imported += 1

    return {
        "imported": imported,
        "errors":   import_errors,
        "message":  f"Imported {imported} expense(s). {len(import_errors)} error(s).",
    }


# ═══════════════════════════════════════════════════════════════════════════════
# HOUSEKEEPING
# ═══════════════════════════════════════════════════════════════════════════════

@mcp.tool
def purge_old_expenses(before_date: str, dry_run: bool = True) -> dict:
    """
    Delete expenses older than a given date.
    Defaults to dry_run=True so nothing is removed without confirmation.

    Args:
        before_date: Delete expenses strictly before this date (YYYY-MM-DD).
        dry_run: When True (default), only report what would be deleted.
    """
    try:
        datetime.strptime(before_date, "%Y-%m-%d")
    except ValueError:
        return {"error": "before_date must be YYYY-MM-DD"}

    with get_db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(ROUND(SUM(amount),2),0) AS total "
            "FROM expenses WHERE date < ?",
            (before_date,),
        ).fetchone()
        count = row["n"]
        total = row["total"]

        if dry_run:
            return {
                "dry_run":     True,
                "would_delete": count,
                "total_amount": total,
                "before_date":  before_date,
                "message": (
                    f"Dry run: {count} expense(s) totalling {total} would be removed. "
                    "Pass dry_run=False to confirm."
                ),
            }

        conn.execute("DELETE FROM expenses WHERE date < ?", (before_date,))
        return {
            "success":      True,
            "deleted":      count,
            "total_amount": total,
            "message":      f"Deleted {count} expense(s) totalling {total} before {before_date}.",
        }


if __name__ == "__main__":
    mcp.run(transport="http",host="0.0.0.0",port=8000)
