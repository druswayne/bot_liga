from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

import db
from keyboards import RoleCB
from states import AuthStates, ReplyStates


class AccessMiddleware(BaseMiddleware):
    async def __call__(self, handler, event: TelegramObject, data: dict):
        from_user = getattr(event, "from_user", None)
        if from_user is None:
            return await handler(event, data)

        user = await db.get_user(from_user.id)
        data["db_user"] = user

        text = event.text if isinstance(event, Message) else None
        is_start = bool(text and text.startswith("/start"))
        is_help = bool(text and text.startswith("/help"))
        is_reset = bool(text and text.startswith("/reset_role"))
        is_ig_auth = bool(
            text
            and (
                text.startswith("/ig_code")
                or text.startswith("/ig_login")
                or text.startswith("/ig_status")
                or text.startswith("/ig_session")
            )
        )

        if is_start:
            return await handler(event, data)

        state = data.get("state")
        current = await state.get_state() if state else None

        if current == AuthStates.password and isinstance(event, Message):
            return await handler(event, data)

        if not user or not user["authorized"]:
            if isinstance(event, Message):
                await event.answer("Сначала выполните /start и введите пароль.")
            elif isinstance(event, CallbackQuery):
                await event.answer("Сначала авторизуйтесь через /start", show_alert=True)
            return

        if is_reset or is_ig_auth or is_help:
            return await handler(event, data)

        if not user.get("role"):
            if isinstance(event, CallbackQuery) and event.data:
                try:
                    RoleCB.unpack(event.data)
                    return await handler(event, data)
                except Exception:
                    pass
            if isinstance(event, Message):
                await event.answer("Сначала выберите пользователя.")
            elif isinstance(event, CallbackQuery):
                await event.answer("Сначала выберите пользователя", show_alert=True)
            return

        if current == ReplyStates.collecting:
            return await handler(event, data)

        return await handler(event, data)
