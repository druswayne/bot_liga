from aiogram.fsm.state import State, StatesGroup


class AuthStates(StatesGroup):
    password = State()


class ReplyStates(StatesGroup):
    collecting = State()
