"""FSM-состояния владельца."""

from aiogram.fsm.state import State, StatesGroup


class OwnerReplyStates(StatesGroup):
    waiting_for_message = State()
