"""Статус требования: agreed только по реплике заказчика без оговорок."""
from __future__ import annotations

from app.app.services import contract_text as ct


def ev(side, quote="Да, делаем."):
    return [{"side": side, "quote": quote}]


def test_agreed_only_with_customer_evidence():
    assert ct.requirement_status("decision", "unknown", ev("customer")) == ("agreed", "customer_evidence")
    assert ct.requirement_status("decision", "unknown", ev("vendor")) == ("proposed", "decision_by_vendor_only")
    assert ct.requirement_status("decision", "unknown", ev("unknown")) == ("proposed", "decision_side_unknown")
    assert ct.requirement_status("decision", "unknown", []) == ("proposed", "decision_side_unknown")


def test_conditional_customer_reply_is_not_agreement():
    assert ct.requirement_status("decision", "unknown", ev("customer", "Возможно, сделаем позже"))[0] == "proposed"
    assert ct.requirement_status("decision", "unknown", ev("customer", "Это по дооценке"))[1] == "customer_conditional"


def test_tasks_and_cancelled():
    assert ct.requirement_status("task", "open", ev("customer")) == ("proposed", "task_not_confirmed")
    assert ct.requirement_status("task", "cancelled", []) == ("rejected", "task_cancelled")
