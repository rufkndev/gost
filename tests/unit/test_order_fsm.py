import pytest

from app.db.order_fsm import TRANSITIONS, InvalidTransition, OrderStatus, check_transition

S = OrderStatus

ALLOWED = [
    (S.CREATED, S.ANALYZING),
    (S.ANALYZING, S.ANALYZED),
    (S.ANALYZING, S.REJECTED),
    (S.ANALYZED, S.AWAITING_PAYMENT),
    (S.ANALYZED, S.PAID),  # бета
    (S.AWAITING_PAYMENT, S.PAID),
    (S.AWAITING_PAYMENT, S.EXPIRED),
    (S.AWAITING_PAYMENT, S.CANCELED),
    (S.EXPIRED, S.AWAITING_PAYMENT),
    (S.CANCELED, S.AWAITING_PAYMENT),
    (S.PAID, S.PROCESSING),
    (S.PROCESSING, S.DONE),
    (S.PROCESSING, S.AWAITING_USER_INPUT),
    (S.AWAITING_USER_INPUT, S.PROCESSING),
    (S.PROCESSING, S.FAILED),
    (S.FAILED, S.REFUNDED),
    (S.DONE, S.COMPLAINT),
    (S.COMPLAINT, S.DONE),
    (S.COMPLAINT, S.REFUNDED),
]


def test_every_status_has_entry() -> None:
    assert set(TRANSITIONS) == set(OrderStatus)


def test_transition_table_matches_spec_exactly() -> None:
    table = {(a, b) for a, targets in TRANSITIONS.items() for b in targets}
    assert table == set(ALLOWED)


@pytest.mark.parametrize(("current", "target"), ALLOWED)
def test_allowed(current: OrderStatus, target: OrderStatus) -> None:
    check_transition(current, target)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (S.AWAITING_PAYMENT, S.AWAITING_PAYMENT),  # повторная проверка не должна «переоплатить»
        (S.PAID, S.PAID),
        (S.CREATED, S.PAID),
        (S.DONE, S.REFUNDED),
        (S.REFUNDED, S.DONE),
        (S.REJECTED, S.ANALYZING),
        (S.PAID, S.DONE),
    ],
)
def test_forbidden(current: OrderStatus, target: OrderStatus) -> None:
    with pytest.raises(InvalidTransition):
        check_transition(current, target)
