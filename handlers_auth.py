from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

import config
import db
from keyboards import RoleCB, role_keyboard
from states import AuthStates

router = Router()


def _welcome(role_key: str, user_id: int | None = None) -> str:
    name = config.ROLES[role_key]["name"]
    text = (
        f"Вы вошли как {name}.\n"
        "Новые письма и сообщения Instagram будут приходить в этот чат.\n\n"
        "/list — список писем и сообщений Instagram\n"
        "/reset_role — сменить пользователя\n"
        "/help — список команд"
    )
    if user_id == config.IG_ADMIN_ID:
        text += (
            "\n\nInstagram:\n"
            "/ig_status — статус входа\n"
            "/ig_login — войти в Instagram\n"
            "/ig_code — отправить код подтверждения"
        )
    return text


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, db_user: dict | None):
    await state.clear()
    if db_user and db_user["authorized"] and db_user.get("role"):
        await message.answer(_welcome(db_user["role"], message.from_user.id if message.from_user else None))
        return
    if db_user and db_user["authorized"]:
        await message.answer("Выберите пользователя:", reply_markup=role_keyboard())
        return
    if not config.ADMIN_PASSWORD:
        await message.answer("ADMIN_PASSWORD не задан в файле .env")
        return
    await state.set_state(AuthStates.password)
    await message.answer("Введите пароль администратора:")


@router.message(AuthStates.password)
async def check_password(message: Message, state: FSMContext):
    if message.text:
        try:
            await message.delete()
        except Exception:
            pass
    if not message.text or message.text.strip() != config.ADMIN_PASSWORD:
        await message.answer("Неверный пароль. Попробуйте ещё раз.")
        return
    await db.upsert_user(message.from_user.id, message.from_user.username, authorized=1)
    await state.clear()
    await message.answer("Пароль принят. Выберите пользователя:", reply_markup=role_keyboard())


@router.callback_query(RoleCB.filter())
async def choose_role(query: CallbackQuery, callback_data: RoleCB, db_user: dict | None):
    if callback_data.key not in config.ROLES:
        await query.answer("Неизвестный пользователь", show_alert=True)
        return
    if not db_user or not db_user["authorized"]:
        await query.answer("Сначала введите пароль", show_alert=True)
        return
    await db.set_role(query.from_user.id, callback_data.key)
    await query.answer()
    await query.message.edit_text(_welcome(callback_data.key, query.from_user.id if query.from_user else None))


@router.message(Command("reset_role"))
async def reset_role(message: Message, state: FSMContext):
    await state.clear()
    await db.set_role(message.from_user.id, None)
    await message.answer("Роль сброшена. Выберите пользователя:", reply_markup=role_keyboard())


@router.message(Command("help"))
async def cmd_help(message: Message):
    text = (
        "Команды бота:\n"
        "/start — вход в бота и выбор пользователя\n"
        "/list — показать письма и сообщения Instagram, которые уже есть в боте\n"
        "/reset_role — сменить пользователя (Андрей / Олег / Марина)\n"
        "/help — эта справка"
    )
    if message.from_user and message.from_user.id == config.IG_ADMIN_ID:
        text += (
            "\n\nКоманды Instagram (только администратор):\n"
            "/ig_status — статус авторизации: вошёл ли Instagram, есть ли сохранённая сессия, "
            "последняя ошибка\n"
            "/ig_login — вручную войти в Instagram. Бот сам вход не выполняет. "
            "После успеха сессия сохраняется и после перезапуска входить снова не нужно\n"
            "/ig_code 123456 — отправить код подтверждения, если Instagram его запросил "
            "(из SMS, почты или приложения 2FA)"
        )
    await message.answer(text)
