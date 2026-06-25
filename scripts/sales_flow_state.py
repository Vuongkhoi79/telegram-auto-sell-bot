from __future__ import annotations

import logging
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from repository.store_repository import ACCOUNT_PRODUCT_CODE_ALIASES, StoreRepository

CANONICAL_PRODUCT_CODES = {
    "ADOBE",
    "ARTLIST",
    "CANVA",
    "CAPCUT",
    "CHATGPT",
    "CLAUDE",
    "CURSOR",
    "ELEVENLABS",
    "GAMMA",
    "GEMINI",
    "GROK",
    "HEYGEN",
    "HIGGSFIELD",
    "KLING",
    "KREA",
    "OPENART",
    "SUNO",
    "VEO3",
    "VIEWMAX",
}


def canonical_product_code(product_code: str) -> str:
    normalized = str(product_code or "").strip().upper()
    if not normalized:
        return ""
    if normalized in CANONICAL_PRODUCT_CODES:
        return normalized
    for canonical, aliases in ACCOUNT_PRODUCT_CODE_ALIASES.items():
        if normalized == canonical or normalized in {str(alias).strip().upper() for alias in aliases}:
            return canonical if canonical in CANONICAL_PRODUCT_CODES else normalized
    if normalized.startswith("GEM"):
        return "GEMINI"
    if normalized.startswith("CAP"):
        return "CAPCUT"
    if normalized.startswith("CHA"):
        return "CHATGPT"
    if normalized.startswith("GRO"):
        return "GROK"
    if normalized.startswith("CUR"):
        return "CURSOR"
    if normalized.startswith("CLA"):
        return "CLAUDE"
    return normalized


@dataclass(frozen=True)
class SalesFlowState:
    stage: str
    callback_data: str
    product_code: str
    package_id: str
    quantity: int
    product_exists: bool
    package_resolves_to_canonical: bool
    available_count: int
    reserved_count: int
    delivered_count: int
    disabled_count: int
    total_count: int
    order_status: str
    reserved_item_ids: tuple[str, ...]
    can_reserve: bool
    expected_available_after: int
    expected_reserved_after: int
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _product_row_id(repo: StoreRepository, product_code: str) -> tuple[bool, str, str]:
    canonical_code = canonical_product_code(product_code)
    details = repo.get_product_details(canonical_code) if canonical_code else None
    if details:
        return True, canonical_code, str(details["id"])
    if not product_code:
        return False, canonical_code, ""
    with repo._session() as connection:  # noqa: SLF001 - diagnostic helper only
        row = connection.execute(
            "SELECT id, code FROM products WHERE UPPER(code) = ?",
            (str(product_code).strip().upper(),),
        ).fetchone()
        if row:
            return True, canonical_code or str(row["code"]).upper(), str(row["id"])
    return False, canonical_code, ""


def snapshot_sales_state(
    database_path: Path | str,
    product_code: str,
    order_id: str | None = None,
    *,
    callback_data: str = "",
    package_id: str = "",
    quantity: int = 0,
) -> SalesFlowState:
    repo = StoreRepository(database_path)
    product_exists, canonical_code, product_row_id = _product_row_id(repo, product_code)
    package_resolves_to_canonical = bool(package_id) and canonical_product_code(package_id) == canonical_code
    available_count = reserved_count = delivered_count = disabled_count = total_count = 0
    if product_row_id:
        with repo._session() as connection:  # noqa: SLF001 - diagnostic helper only
            counts = connection.execute(
                """
                SELECT
                    SUM(CASE WHEN status = 'available' THEN 1 ELSE 0 END) AS available_count,
                    SUM(CASE WHEN status = 'reserved' THEN 1 ELSE 0 END) AS reserved_count,
                    SUM(CASE WHEN status = 'delivered' THEN 1 ELSE 0 END) AS delivered_count,
                    SUM(CASE WHEN status = 'disabled' THEN 1 ELSE 0 END) AS disabled_count,
                    COUNT(*) AS total_count
                FROM inventory_items
                WHERE product_id = ?
                """,
                (product_row_id,),
            ).fetchone()
            available_count = int(counts["available_count"] or 0)
            reserved_count = int(counts["reserved_count"] or 0)
            delivered_count = int(counts["delivered_count"] or 0)
            disabled_count = int(counts["disabled_count"] or 0)
            total_count = int(counts["total_count"] or 0)
    order_status = ""
    reserved_item_ids: tuple[str, ...] = ()
    if order_id:
        order = repo.find_order(order_id)
        if order:
            order_status = str(order.get("order_status", "") or order.get("payment_status", "") or "")
        with repo._session() as connection:  # noqa: SLF001 - diagnostic helper only
            rows = connection.execute(
                """
                SELECT i.id
                FROM inventory_items AS i
                JOIN products AS p ON p.id = i.product_id
                WHERE i.reserved_order_id = ?
                  AND UPPER(p.code) = ?
                ORDER BY i.created_at, i.id
                """,
                (order_id, canonical_code or str(product_code).strip().upper()),
            ).fetchall()
            reserved_item_ids = tuple(str(row["id"]) for row in rows)
    can_reserve = quantity > 0 and available_count >= quantity
    expected_available_after = available_count - quantity if can_reserve else available_count
    expected_reserved_after = reserved_count + quantity if can_reserve else reserved_count
    reason = "" if can_reserve else f"available_before={available_count} < requested_quantity={quantity}"
    return SalesFlowState(
        stage="snapshot",
        callback_data=callback_data,
        product_code=canonical_code or str(product_code).strip().upper(),
        package_id=str(package_id or ""),
        quantity=int(quantity or 0),
        product_exists=product_exists,
        package_resolves_to_canonical=package_resolves_to_canonical,
        available_count=available_count,
        reserved_count=reserved_count,
        delivered_count=delivered_count,
        disabled_count=disabled_count,
        total_count=total_count,
        order_status=order_status,
        reserved_item_ids=reserved_item_ids,
        can_reserve=can_reserve,
        expected_available_after=expected_available_after,
        expected_reserved_after=expected_reserved_after,
        reason=reason,
    )


def validate_stock_invariant(state: SalesFlowState) -> None:
    assert state.available_count + state.reserved_count + state.delivered_count + state.disabled_count == state.total_count, (
        f"stock invariant failed for {state.product_code}: "
        f"{state.available_count}+{state.reserved_count}+{state.delivered_count}+{state.disabled_count} != {state.total_count}"
    )


def assert_reservation_transition(before: SalesFlowState, after: SalesFlowState, quantity: int) -> None:
    assert before.available_count >= quantity, f"available_before={before.available_count} < requested_quantity={quantity}"
    assert after.reserved_count == before.reserved_count + quantity, (
        f"reserved_after={after.reserved_count} != reserved_before+q={before.reserved_count + quantity}"
    )
    assert after.available_count == before.available_count - quantity, (
        f"available_after={after.available_count} != available_before-q={before.available_count - quantity}"
    )
    validate_stock_invariant(before)
    validate_stock_invariant(after)


def log_sales_state(
    stage: str,
    product_code: str,
    order_id: str | None = None,
    *,
    database_path: Path | str,
    callback_data: str = "",
    package_id: str = "",
    quantity: int = 0,
) -> dict[str, Any]:
    state = snapshot_sales_state(
        database_path,
        product_code,
        order_id,
        callback_data=callback_data,
        package_id=package_id,
        quantity=quantity,
    )
    payload = state.to_dict()
    payload["stage"] = stage
    logging.getLogger(__name__).debug(
        "SALES_STATE stage=%s callback_data=%s product_code=%s package_id=%s quantity=%s available_count=%s reserved_count=%s delivered_count=%s disabled_count=%s order_status=%s reserved_item_ids=%s",
        stage,
        state.callback_data,
        state.product_code,
        state.package_id,
        state.quantity,
        state.available_count,
        state.reserved_count,
        state.delivered_count,
        state.disabled_count,
        state.order_status,
        list(state.reserved_item_ids),
    )
    return payload
