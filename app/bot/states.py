from aiogram.fsm.state import State, StatesGroup


class LinkStates(StatesGroup):
    waiting_contact = State()


class QuestionStates(StatesGroup):
    waiting_question = State()
