from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import ROLES, STATUS_IN_PROGRESS, STATUS_PENDING, STATUS_PROCESSED


class RoleCB(CallbackData, prefix="role"):
    key: str


class EmailCB(CallbackData, prefix="em"):
    act: str
    eid: int


class IgCB(CallbackData, prefix="ig"):
    act: str
    eid: int


class ReplyCB(CallbackData, prefix="rp"):
    act: str


class CloseViewCB(CallbackData, prefix="cv"):
    token: str


def role_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for role in ROLES.values():
        builder.button(text=role["name"], callback_data=RoleCB(key=role["key"]))
    builder.adjust(1)
    return builder.as_markup()


def email_keyboard(email: dict) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    eid = email["id"]
    status = email.get("status")
    builder.button(text="Просмотреть", callback_data=EmailCB(act="view", eid=eid))
    if status == STATUS_PENDING:
        builder.button(text="Взять в работу", callback_data=EmailCB(act="take", eid=eid))
    if status == STATUS_IN_PROGRESS:
        builder.button(text="Передумал", callback_data=EmailCB(act="release", eid=eid))
    if status != STATUS_PROCESSED:
        builder.button(text="Ответить", callback_data=EmailCB(act="reply", eid=eid))
    builder.button(text="Удалить", callback_data=EmailCB(act="delete", eid=eid))
    builder.adjust(1)
    return builder.as_markup()


def ig_keyboard(item: dict) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    eid = item["id"]
    status = item.get("status")
    builder.button(text="Просмотреть", callback_data=IgCB(act="view", eid=eid))
    if status == STATUS_PENDING:
        builder.button(text="Взять в работу", callback_data=IgCB(act="take", eid=eid))
    if status == STATUS_IN_PROGRESS:
        builder.button(text="Передумал", callback_data=IgCB(act="release", eid=eid))
    if status != STATUS_PROCESSED:
        builder.button(text="Ответить", callback_data=IgCB(act="reply", eid=eid))
    builder.button(text="Удалить", callback_data=IgCB(act="delete", eid=eid))
    builder.adjust(1)
    return builder.as_markup()


def reply_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Отправить", callback_data=ReplyCB(act="send"))
    builder.button(text="Отмена", callback_data=ReplyCB(act="cancel"))
    builder.adjust(2)
    return builder.as_markup()


def close_view_keyboard(token: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Закрыть", callback_data=CloseViewCB(token=token))
    return builder.as_markup()
