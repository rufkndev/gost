"""Статусы заказа и допустимые переходы (spec, раздел 8.2). Любой другой переход — исключение."""

from __future__ import annotations

from enum import StrEnum


class OrderStatus(StrEnum):
    CREATED = "created"
    ANALYZING = "analyzing"
    ANALYZED = "analyzed"
    REJECTED = "rejected"
    AWAITING_PAYMENT = "awaiting_payment"
    PAID = "paid"
    EXPIRED = "expired"
    CANCELED = "canceled"
    PROCESSING = "processing"
    AWAITING_USER_INPUT = "awaiting_user_input"
    DONE = "done"
    FAILED = "failed"
    REFUNDED = "refunded"
    COMPLAINT = "complaint"


S = OrderStatus

TRANSITIONS: dict[OrderStatus, frozenset[OrderStatus]] = {
    S.CREATED: frozenset({S.ANALYZING}),
    S.ANALYZING: frozenset({S.ANALYZED, S.REJECTED}),
    # analyzed → paid — только для бета-пользователей, с событием beta_free.
    S.ANALYZED: frozenset({S.AWAITING_PAYMENT, S.PAID}),
    S.AWAITING_PAYMENT: frozenset({S.PAID, S.EXPIRED, S.CANCELED}),
    S.EXPIRED: frozenset({S.AWAITING_PAYMENT}),
    S.CANCELED: frozenset({S.AWAITING_PAYMENT}),
    S.PAID: frozenset({S.PROCESSING}),
    S.PROCESSING: frozenset({S.DONE, S.AWAITING_USER_INPUT, S.FAILED}),
    S.AWAITING_USER_INPUT: frozenset({S.PROCESSING}),
    S.FAILED: frozenset({S.REFUNDED}),
    S.DONE: frozenset({S.COMPLAINT}),
    S.COMPLAINT: frozenset({S.DONE, S.REFUNDED}),
    S.REJECTED: frozenset(),
    S.REFUNDED: frozenset(),
}


class InvalidTransition(Exception):
    def __init__(self, current: OrderStatus, target: OrderStatus) -> None:
        super().__init__(f"order transition {current} -> {target} is not allowed")
        self.current = current
        self.target = target


def can_transition(current: OrderStatus, target: OrderStatus) -> bool:
    return target in TRANSITIONS[current]


def check_transition(current: OrderStatus, target: OrderStatus) -> None:
    if not can_transition(current, target):
        raise InvalidTransition(current, target)
