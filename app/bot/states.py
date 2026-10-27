from aiogram.fsm.state import State, StatesGroup


class Flow(StatesGroup):
    waiting_file = State()  # data: profile_id
    waiting_email = State()  # data: short (заказ, который оплачиваем), может быть пустым
    complaint_text = State()  # data: short
