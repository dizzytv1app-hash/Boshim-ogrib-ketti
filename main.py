"""
Telegram clothing-shop admin bot (single-file implementation).

Dependencies:
    pip install aiogram aiohttp python-dotenv

Required environment variables:
    BOT_TOKEN, API_BASE_URL, BOT_API_SECRET, ADMIN_IDS

Optional:
    MINI_APP_URL, SHOP_NAME, WELCOME_TEXT, API_TIMEOUT,
    NOTIFICATION_POLL_SECONDS

The bot deliberately does not store shop data locally. It expects the API
contract documented in API_PATHS below, so the separate backend can implement
these routes and PostgreSQL remains the source of truth.

Expected route methods:
    GET /admin/stats
    GET, POST /admin/products; GET, PATCH, DELETE /admin/products/{id}
    GET /admin/orders; GET, PATCH /admin/orders/{id}
    GET /admin/help-requests; GET, PATCH /admin/help-requests/{id}
    GET, POST /admin/discounts; GET, PATCH, DELETE /admin/discounts/{id}
    GET, PATCH /admin/app-info and /admin/settings
    GET, POST /admin/channels; DELETE /admin/channels/{telegram_chat_id}
    GET, POST /admin/channel-posts
    GET /bot/notifications?after_id=...&type=new_order
    POST /help-requests

Collection responses may be a JSON array or {items: [...]} / {products: [...]}.
Single-item responses may be an object or {product: {...}} (same pattern for
orders, help_request, discount, and app_info). Product writes use
image_file_id, name, price, colors, sizes, stock, and description.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any
from urllib.parse import quote

import aiohttp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    WebAppInfo,
)

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    # Environment variables can also be supplied directly by the host.
    pass


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("telegram_shop_bot")

BOT_TOKEN = os.getenv("BOT_TOKEN", "8924504263:AAGjChzbRvlKWztR8vP9s_FADlZzbK4huz0").strip()
API_BASE_URL = os.getenv("API_BASE_URL", "").strip().rstrip("/")
BOT_API_SECRET = os.getenv("BOT_API_SECRET", "").strip()
ADMIN_IDS = {
    int(value.strip())
    for value in os.getenv("ADMIN_IDS", "1476624803").split(",")
    if value.strip().isdigit()
}
MINI_APP_URL = os.getenv("MINI_APP_URL", "").strip()
SHOP_NAME = os.getenv("SHOP_NAME", "Clothing Shop").strip()
WELCOME_TEXT = os.getenv(
    "WELCOME_TEXT", "Welcome! Browse our products in the Mini App."
).strip()
API_TIMEOUT = float(os.getenv("API_TIMEOUT", "20"))
NOTIFICATION_POLL_SECONDS = int(os.getenv("NOTIFICATION_POLL_SECONDS", "15"))

# Backend/API contract expected by this bot. All admin routes receive
# X-Bot-Secret and X-Admin-Id. User help requests receive X-Bot-Secret and
# X-Telegram-Id. The backend should return JSON objects or arrays.
API_PATHS = {
    "stats": "/admin/stats",
    "products": "/admin/products",
    "orders": "/admin/orders",
    "help_requests": "/admin/help-requests",
    "discounts": "/admin/discounts",
    "app_info": "/admin/app-info",
    "settings": "/admin/settings",
    "channels": "/admin/channels",
    "channel_posts": "/admin/channel-posts",
    "notifications": "/bot/notifications",
    "help_submit": "/help-requests",
}


class APIError(Exception):
    """An API error safe to show to a bot user."""


class HelpFlow(StatesGroup):
    message = State()


class ProductFlow(StatesGroup):
    image = State()
    name = State()
    price = State()
    colors = State()
    sizes = State()
    stock = State()
    description = State()
    edit_value = State()


class DiscountFlow(StatesGroup):
    name = State()
    percent = State()


class ChannelFlow(StatesGroup):
    channel = State()


class PostFlow(StatesGroup):
    media = State()
    title = State()
    details = State()
    channel = State()
    edit_caption = State()


class TextEditFlow(StatesGroup):
    value = State()


class ShopAPI:
    def __init__(self) -> None:
        self.session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        timeout = aiohttp.ClientTimeout(total=API_TIMEOUT)
        self.session = aiohttp.ClientSession(timeout=timeout)

    async def close(self) -> None:
        if self.session and not self.session.closed:
            await self.session.close()

    async def request(
        self,
        method: str,
        path: str,
        *,
        admin_id: int | None = None,
        telegram_id: int | None = None,
        params: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> Any:
        if not self.session:
            raise APIError("API connection is not ready.")
        headers = {"X-Bot-Secret": BOT_API_SECRET}
        if admin_id is not None:
            headers["X-Admin-Id"] = str(admin_id)
        if telegram_id is not None:
            headers["X-Telegram-Id"] = str(telegram_id)
        url = f"{API_BASE_URL}{path}"
        try:
            async with self.session.request(
                method,
                url,
                headers=headers,
                params=params,
                json=payload,
            ) as response:
                raw = await response.text()
                try:
                    body = await response.json(content_type=None) if raw else {}
                except (ValueError, aiohttp.ContentTypeError):
                    body = {"message": raw[:500]}
                if response.status >= 400:
                    log.warning("API %s %s returned HTTP %s", method, path, response.status)
                    if response.status in (401, 403):
                        raise APIError("API access was denied. Check the bot/API admin settings.")
                    if response.status == 404:
                        raise APIError("That record was not found.")
                    if response.status == 409:
                        raise APIError("The request conflicts with existing data.")
                    if response.status == 422:
                        detail = body.get("message") if isinstance(body, dict) else None
                        raise APIError(str(detail or "Some information was invalid."))
                    raise APIError("The shop API is temporarily unavailable. Try again shortly.")
                if isinstance(body, dict) and body.get("success") is False:
                    raise APIError(str(body.get("message") or "The API could not complete that request."))
                return body
        except asyncio.TimeoutError as exc:
            raise APIError("The shop API took too long to respond. Try again.") from exc
        except aiohttp.ClientError as exc:
            log.warning("API connection error for %s %s: %s", method, path, exc)
            raise APIError("Could not connect to the shop API. Please try again later.") from exc


api = ShopAPI()
router = Router()
dp = Dispatcher()
dp.include_router(router)
bot: Bot | None = None


def items_from(data: Any, *keys: str) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        for key in (*keys, "items", "results", "data"):
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
            if isinstance(value, dict):
                for nested_key in ("items", "results"):
                    nested = value.get(nested_key)
                    if isinstance(nested, list):
                        return [item for item in nested if isinstance(item, dict)]
    return []


def record_from(data: Any, *keys: str) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {}
    for key in (*keys, "item", "data", "result"):
        value = data.get(key)
        if isinstance(value, dict):
            return value
    return data


def display(value: Any, fallback: str = "—") -> str:
    if value is None or value == "":
        return fallback
    if isinstance(value, (list, tuple)):
        return ", ".join(str(part) for part in value) or fallback
    return str(value)


def ident(item: dict[str, Any]) -> str:
    return str(item.get("id") or item.get("product_id") or item.get("order_id") or "")


def admin_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text="📊 Statistika", callback_data="adm:stats"),
            InlineKeyboardButton(text="👕 Mahsulotlar", callback_data="adm:products"),
        ],
        [
            InlineKeyboardButton(text="🧾 Buyurtmalar", callback_data="adm:orders"),
            InlineKeyboardButton(text="🆘 Yordam", callback_data="adm:helps"),
        ],
        [
            InlineKeyboardButton(text="🏷 Chegirmalar", callback_data="adm:discounts"),
            InlineKeyboardButton(text="📣 Kanallar/postlar", callback_data="adm:channels"),
        ],
        [
            InlineKeyboardButton(text="📱 Ilova ma’lumoti", callback_data="adm:app"),
            InlineKeyboardButton(text="⚙️ Sozlamalar", callback_data="adm:settings"),
        ],
        [InlineKeyboardButton(text="📜 Yuborilgan postlar", callback_data="adm:history")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def back_keyboard(target: str = "home") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Admin panel", callback_data=f"adm:{target}")]]
    )


def home_keyboard() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if MINI_APP_URL:
        if MINI_APP_URL.startswith("https://"):
            rows.append(
                [InlineKeyboardButton(text="🛍 Mini Appni ochish", web_app=WebAppInfo(url=MINI_APP_URL))]
            )
        else:
            rows.append(
                [InlineKeyboardButton(text="🛍 Mini Appni ochish", url=MINI_APP_URL)]
            )
    rows.append([InlineKeyboardButton(text="🆘 Yordam", callback_data="user:help")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def is_admin(user_id: int | None) -> bool:
    return bool(user_id and user_id in ADMIN_IDS)


async def require_admin(event: Message | CallbackQuery) -> bool:
    user = event.from_user
    if is_admin(user.id if user else None):
        return True
    text = "Bu bo‘lim faqat ruxsat berilgan adminlar uchun."
    if isinstance(event, CallbackQuery):
        await event.answer(text, show_alert=True)
    else:
        await event.answer(text)
    return False


async def admin_api(user_id: int, method: str, path: str, **kwargs: Any) -> Any:
    return await api.request(method, path, admin_id=user_id, **kwargs)


async def show_admin_menu(message: Message) -> None:
    await message.answer(f"{SHOP_NAME} — admin panel", reply_markup=admin_keyboard())


async def callback_menu(call: CallbackQuery, text: str, keyboard: InlineKeyboardMarkup | None = None) -> None:
    await call.answer()
    if call.message:
        await call.message.answer(text, reply_markup=keyboard)


async def ask_for_channels(
    user_id: int, state: FSMContext, message: Message
) -> None:
    data = await admin_api(user_id, "GET", API_PATHS["channels"])
    channels = items_from(data, "channels")
    if not channels:
        await message.answer("Avval kanalni ulang: Admin panel → Kanallar/postlar → Kanal ulash.")
        await state.clear()
        return
    buttons = []
    for channel in channels[:20]:
        channel_id = str(channel.get("chat_id") or channel.get("username") or ident(channel))
        label = str(channel.get("title") or channel.get("username") or channel_id)
        if channel_id:
            buttons.append(
                [InlineKeyboardButton(text=label[:60], callback_data=f"post:channel:{channel_id}")]
            )
    buttons.append([InlineKeyboardButton(text="Bekor qilish", callback_data="post:cancel")])
    await state.set_state(PostFlow.channel)
    await message.answer(
        "Qaysi kanalga yuborilsin?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


async def list_products_for_post(user_id: int, message: Message) -> None:
    data = await admin_api(user_id, "GET", API_PATHS["products"], params={"limit": 20})
    products = items_from(data, "products")
    if not products:
        await message.answer("Hozircha mahsulot topilmadi.")
        return
    rows = [
        [InlineKeyboardButton(
            text=f"{p.get('name', 'Mahsulot')} · {display(p.get('price'))}"[:60],
            callback_data=f"post:product:{ident(p)}",
        )]
        for p in products[:20]
        if ident(p)
    ]
    rows.append([InlineKeyboardButton(text="⬅️ Orqaga", callback_data="adm:channels")])
    await message.answer("Kanalga yuboriladigan mahsulotni tanlang:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.message(CommandStart())
async def start_handler(message: Message) -> None:
    await message.answer(
        f"{SHOP_NAME}\n\n{WELCOME_TEXT}",
        reply_markup=home_keyboard(),
    )


@router.message(Command("help"))
async def help_command(message: Message, state: FSMContext) -> None:
    await state.set_state(HelpFlow.message)
    await message.answer("Muammoingizni matn yoki rasm bilan yuboring. Bekor qilish uchun /cancel yozing.")


@router.callback_query(F.data == "user:help")
async def help_button(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await state.set_state(HelpFlow.message)
    if call.message:
        await call.message.answer("Muammoingizni matn yoki rasm bilan yuboring. Bekor qilish uchun /cancel yozing.")


@router.message(Command("cancel"))
async def cancel_handler(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Bekor qilindi.")


@router.message(HelpFlow.message)
async def receive_help_request(message: Message, state: FSMContext) -> None:
    user = message.from_user
    if not user:
        await message.answer("Foydalanuvchi ma’lumotini aniqlab bo‘lmadi.")
        return
    text = message.text or message.caption or ""
    photo_file_id = message.photo[-1].file_id if message.photo else None
    if not text and not photo_file_id:
        await message.answer("Iltimos, muammoni matn yoki rasm ko‘rinishida yuboring.")
        return
    payload = {
        "telegram_user_id": user.id,
        "name": user.full_name,
        "username": user.username,
        "message": text,
        "photo_file_id": photo_file_id,
    }
    try:
        result = await api.request(
            "POST",
            API_PATHS["help_submit"],
            telegram_id=user.id,
            payload=payload,
        )
        await message.answer("Murojaatingiz yuborildi. Adminlar tez orada ko‘rib chiqadi.")
        request_id = record_from(result, "help_request", "request").get("id", "")
        for admin_id in ADMIN_IDS:
            try:
                caption = f"Yangi yordam so‘rovi #{request_id}\nIsm: {user.full_name}"
                if user.username:
                    caption += f"\nUsername: @{user.username}"
                caption += f"\nTelegram ID: {user.id}\n\n{text or 'Rasm yuborildi.'}"
                if photo_file_id:
                    await bot.send_photo(admin_id, photo_file_id, caption=caption)  # type: ignore[union-attr]
                else:
                    await bot.send_message(admin_id, caption)  # type: ignore[union-attr]
            except Exception:
                log.exception("Could not notify admin %s about help request", admin_id)
        await state.clear()
    except APIError as exc:
        await message.answer(f"Murojaatni saqlab bo‘lmadi: {exc}")


@router.message(Command("admin"))
async def admin_command(message: Message) -> None:
    if await require_admin(message):
        await show_admin_menu(message)


@router.callback_query(F.data.startswith("adm:"))
async def admin_menu_callback(call: CallbackQuery, state: FSMContext) -> None:
    if not await require_admin(call):
        return
    user_id = call.from_user.id
    action = call.data.split(":", 1)[1] if call.data else ""
    await call.answer()
    try:
        if action == "home":
            if call.message:
                await call.message.answer(f"{SHOP_NAME} — admin panel", reply_markup=admin_keyboard())
        elif action == "stats":
            data = record_from(await admin_api(user_id, "GET", API_PATHS["stats"]), "stats")
            text = (
                "📊 Statistika\n"
                f"Foydalanuvchilar: {display(data.get('total_users'))}\n"
                f"Mahsulotlar: {display(data.get('total_products'))}\n"
                f"Buyurtmalar: {display(data.get('total_orders'))}\n"
                f"Jami savdo: {display(data.get('total_sales'))}\n"
                f"Bugungi buyurtmalar: {display(data.get('today_orders'))}\n"
                f"Bugungi savdo: {display(data.get('today_sales'))}\n"
                f"Haftalik savdo: {display(data.get('weekly_sales'))}\n"
                f"Oylik savdo: {display(data.get('monthly_sales'))}\n"
                f"Yordam so‘rovlari: {display(data.get('total_help_requests'))} "
                f"(yangi: {display(data.get('new_help_requests'))})"
            )
            if call.message:
                await call.message.answer(text, reply_markup=back_keyboard())
        elif action == "products":
            if call.message:
                await products_menu(call.message, user_id)
        elif action == "orders":
            if call.message:
                await orders_menu(call.message, user_id)
        elif action == "helps":
            if call.message:
                await help_requests_menu(call.message, user_id)
        elif action == "discounts":
            if call.message:
                await discounts_menu(call.message, user_id)
        elif action == "channels":
            if call.message:
                await channels_menu(call.message, user_id)
        elif action == "app":
            if call.message:
                await app_info_menu(call.message, user_id)
        elif action == "settings":
            if call.message:
                await settings_menu(call.message, user_id)
        elif action == "history":
            if call.message:
                await history_menu(call.message, user_id)
    except APIError as exc:
        if call.message:
            await call.message.answer(f"Amal bajarilmadi: {exc}", reply_markup=back_keyboard())


async def products_menu(message: Message, user_id: int) -> None:
    data = await admin_api(user_id, "GET", API_PATHS["products"], params={"limit": 15})
    products = items_from(data, "products")
    rows = [
        [InlineKeyboardButton(text="➕ Mahsulot qo‘shish", callback_data="product:add")]
    ]
    for product in products[:15]:
        pid = ident(product)
        if pid:
            rows.append(
                [InlineKeyboardButton(
                    text=f"{product.get('name', 'Mahsulot')} · {display(product.get('price'))}"[:60],
                    callback_data=f"product:view:{pid}",
                )]
            )
    rows.append([InlineKeyboardButton(text="⬅️ Admin panel", callback_data="adm:home")])
    await message.answer(
        "Mahsulotlar (birinchi 15 ta):" if products else "Mahsulotlar hali yo‘q.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data == "product:add")
async def product_add_start(call: CallbackQuery, state: FSMContext) -> None:
    if not await require_admin(call):
        return
    await call.answer()
    await state.clear()
    await state.set_state(ProductFlow.image)
    if call.message:
        await call.message.answer("Mahsulot rasmini yuboring (Telegram rasmi). Bekor qilish: /cancel")


@router.message(ProductFlow.image)
async def product_add_image(message: Message, state: FSMContext) -> None:
    if not await require_admin(message):
        return
    if not message.photo:
        await message.answer("Rasm yuboring.")
        return
    await state.update_data(image_file_id=message.photo[-1].file_id)
    await state.set_state(ProductFlow.name)
    await message.answer("Mahsulot nomini yozing:")


@router.message(ProductFlow.name)
async def product_add_name(message: Message, state: FSMContext) -> None:
    if not await require_admin(message):
        return
    name = (message.text or "").strip()
    if not name or len(name) > 150:
        await message.answer("Nom 1–150 belgi bo‘lishi kerak.")
        return
    await state.update_data(name=name)
    await state.set_state(ProductFlow.price)
    await message.answer("Narxni raqam bilan kiriting:")


@router.message(ProductFlow.price)
async def product_add_price(message: Message, state: FSMContext) -> None:
    if not await require_admin(message):
        return
    try:
        price = float((message.text or "").replace(",", ".").strip())
        if price < 0:
            raise ValueError
    except ValueError:
        await message.answer("Narx manfiy bo‘lmagan raqam bo‘lishi kerak.")
        return
    await state.update_data(price=price)
    await state.set_state(ProductFlow.colors)
    await message.answer("Ranglarni vergul bilan ajrating (masalan: qora, oq) yoki — yozing:")


@router.message(ProductFlow.colors)
async def product_add_colors(message: Message, state: FSMContext) -> None:
    if not await require_admin(message):
        return
    colors = (message.text or "").strip()
    await state.update_data(colors=[] if colors in ("—", "-", "yo‘q") else [v.strip() for v in colors.split(",") if v.strip()])
    await state.set_state(ProductFlow.sizes)
    await message.answer("O‘lcham/variantlarni vergul bilan ajrating yoki — yozing:")


@router.message(ProductFlow.sizes)
async def product_add_sizes(message: Message, state: FSMContext) -> None:
    if not await require_admin(message):
        return
    sizes = (message.text or "").strip()
    await state.update_data(sizes=[] if sizes in ("—", "-", "yo‘q") else [v.strip() for v in sizes.split(",") if v.strip()])
    await state.set_state(ProductFlow.stock)
    await message.answer("Ombordagi sonini butun raqam bilan kiriting:")


@router.message(ProductFlow.stock)
async def product_add_stock(message: Message, state: FSMContext) -> None:
    if not await require_admin(message):
        return
    try:
        stock = int((message.text or "").strip())
        if stock < 0:
            raise ValueError
    except ValueError:
        await message.answer("Stock 0 yoki undan katta butun son bo‘lishi kerak.")
        return
    await state.update_data(stock=stock)
    await state.set_state(ProductFlow.description)
    await message.answer("Description (ixtiyoriy). O‘tkazib yuborish uchun — yozing:")


@router.message(ProductFlow.description)
async def product_add_description(message: Message, state: FSMContext) -> None:
    if not await require_admin(message):
        return
    data = await state.get_data()
    description = (message.text or "").strip()
    if description in ("—", "-"):
        description = ""
    payload = {
        "name": data["name"],
        "price": data["price"],
        "colors": data["colors"],
        "sizes": data["sizes"],
        "stock": data["stock"],
        "description": description,
        "image_file_id": data["image_file_id"],
    }
    try:
        await admin_api(message.from_user.id, "POST", API_PATHS["products"], payload=payload)  # type: ignore[union-attr]
        await state.clear()
        await message.answer("Mahsulot API orqali saqlandi. Mini App ma’lumotni API’dan oladi.")
    except APIError as exc:
        await message.answer(f"Mahsulot saqlanmadi: {exc}")


@router.callback_query(F.data.startswith("product:view:"))
async def product_view(call: CallbackQuery) -> None:
    if not await require_admin(call):
        return
    await call.answer()
    pid = call.data.rsplit(":", 1)[1]  # type: ignore[union-attr]
    try:
        result = await admin_api(call.from_user.id, "GET", f"{API_PATHS['products']}/{quote(pid)}")
        product = record_from(result, "product")
        text = (
            f"👕 {display(product.get('name'))}\n"
            f"ID: {pid}\nNarx: {display(product.get('price'))}\n"
            f"Ranglar: {display(product.get('colors'))}\n"
            f"O‘lchamlar: {display(product.get('sizes') or product.get('variants'))}\n"
            f"Stock: {display(product.get('stock'))}\n"
            f"Description: {display(product.get('description'))}"
        )
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Tahrirlash", callback_data=f"product:edit:{pid}")],
            [InlineKeyboardButton(text="🗑 O‘chirish", callback_data=f"product:delete:{pid}")],
            [InlineKeyboardButton(text="⬅️ Mahsulotlar", callback_data="adm:products")],
        ])
        if call.message:
            image = product.get("image_file_id") or product.get("image")
            if image:
                try:
                    await call.message.answer_photo(str(image), caption=text, reply_markup=keyboard)
                    return
                except Exception:
                    log.info("Product image could not be sent for %s", pid)
            await call.message.answer(text, reply_markup=keyboard)
    except APIError as exc:
        if call.message:
            await call.message.answer(f"Mahsulotni ochib bo‘lmadi: {exc}")


@router.callback_query(F.data.startswith("product:edit:"))
async def product_edit_start(call: CallbackQuery) -> None:
    if not await require_admin(call):
        return
    await call.answer()
    pid = call.data.rsplit(":", 1)[1]  # type: ignore[union-attr]
    fields = [
        ("name", "Nom"), ("price", "Narx"), ("colors", "Ranglar"),
        ("sizes", "O‘lcham/variant"), ("stock", "Stock"),
        ("description", "Description"), ("image_file_id", "Rasm file_id"),
    ]
    rows = [
        [InlineKeyboardButton(text=label, callback_data=f"product:field:{pid}:{key}")]
        for key, label in fields
    ]
    if call.message:
        await call.message.answer("Qaysi maydonni o‘zgartirasiz?", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("product:field:"))
async def product_edit_field(call: CallbackQuery, state: FSMContext) -> None:
    if not await require_admin(call):
        return
    await call.answer()
    _, _, pid, field = (call.data or "").split(":", 3)
    await state.update_data(edit_product_id=pid, edit_product_field=field)
    await state.set_state(ProductFlow.edit_value)
    if call.message:
        await call.message.answer(f"Yangi {field} qiymatini yuboring. Rang/o‘lchamlar vergul bilan; ro‘yxatni tozalash uchun —.")


@router.message(ProductFlow.edit_value)
async def product_edit_value(message: Message, state: FSMContext) -> None:
    if not await require_admin(message):
        return
    data = await state.get_data()
    value = (message.text or "").strip()
    field = data.get("edit_product_field")
    if not value:
        await message.answer("Qiymat bo‘sh bo‘lmasligi kerak.")
        return
    if field == "price":
        try:
            value = float(value.replace(",", "."))
            if value < 0:
                raise ValueError
        except ValueError:
            await message.answer("Narxni manfiy bo‘lmagan raqamda kiriting.")
            return
    elif field == "stock":
        try:
            value = int(value)
            if value < 0:
                raise ValueError
        except ValueError:
            await message.answer("Stock 0 yoki undan katta butun son bo‘lishi kerak.")
            return
    elif field in ("colors", "sizes"):
        value = [] if value in ("—", "-") else [v.strip() for v in value.split(",") if v.strip()]
    try:
        await admin_api(
            message.from_user.id,  # type: ignore[union-attr]
            "PATCH",
            f"{API_PATHS['products']}/{quote(str(data['edit_product_id']))}",
            payload={field: value},
        )
        await state.clear()
        await message.answer("Mahsulot yangilandi.")
    except APIError as exc:
        await message.answer(f"Mahsulot yangilanmadi: {exc}")


@router.callback_query(F.data.startswith("product:delete:"))
async def product_delete_confirm(call: CallbackQuery) -> None:
    if not await require_admin(call):
        return
    await call.answer()
    pid = call.data.rsplit(":", 1)[1]  # type: ignore[union-attr]
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Ha, o‘chirish", callback_data=f"product:delyes:{pid}"),
        InlineKeyboardButton(text="Bekor qilish", callback_data="adm:products"),
    ]])
    if call.message:
        await call.message.answer(f"{pid} ID’li mahsulotni o‘chirishni tasdiqlaysizmi?", reply_markup=keyboard)


@router.callback_query(F.data.startswith("product:delyes:"))
async def product_delete(call: CallbackQuery) -> None:
    if not await require_admin(call):
        return
    await call.answer()
    pid = call.data.rsplit(":", 1)[1]  # type: ignore[union-attr]
    try:
        await admin_api(call.from_user.id, "DELETE", f"{API_PATHS['products']}/{quote(pid)}")
        if call.message:
            await call.message.answer("Mahsulot o‘chirildi.")
    except APIError as exc:
        if call.message:
            await call.message.answer(f"O‘chirib bo‘lmadi: {exc}")


async def orders_menu(message: Message, user_id: int) -> None:
    result = await admin_api(user_id, "GET", API_PATHS["orders"], params={"limit": 15})
    orders = items_from(result, "orders")
    rows = []
    for order in orders[:15]:
        oid = ident(order)
        if oid:
            label = f"#{oid} · {display(order.get('status'))} · {display(order.get('total_amount') or order.get('total'))}"
            rows.append([InlineKeyboardButton(text=label[:60], callback_data=f"order:view:{oid}")])
    rows.append([InlineKeyboardButton(text="⬅️ Admin panel", callback_data="adm:home")])
    await message.answer("Buyurtmalar:" if orders else "Buyurtmalar hali yo‘q.", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("order:view:"))
async def order_view(call: CallbackQuery) -> None:
    if not await require_admin(call):
        return
    await call.answer()
    oid = call.data.rsplit(":", 1)[1]  # type: ignore[union-attr]
    try:
        data = await admin_api(call.from_user.id, "GET", f"{API_PATHS['orders']}/{quote(oid)}")
        order = record_from(data, "order")
        lines = [
            f"🧾 Buyurtma #{oid}",
            f"Holat: {display(order.get('status'))}",
            f"Foydalanuvchi: {display(order.get('name') or order.get('user_name'))}",
            f"Username: @{order.get('username')}" if order.get("username") else "",
            f"Telegram ID: {display(order.get('telegram_user_id'))}",
            f"Mahsulotlar: {display(order.get('items') or order.get('products'))}",
            f"Jami: {display(order.get('total_amount') or order.get('total'))}",
            f"Sana: {display(order.get('created_at'))}",
        ]
        buttons = [
            [InlineKeyboardButton(text=status, callback_data=f"order:status:{oid}:{status.lower()}")]
            for status in ("New", "Processing", "Completed", "Cancelled")
        ]
        if call.message:
            await call.message.answer("\n".join(line for line in lines if line), reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    except APIError as exc:
        if call.message:
            await call.message.answer(f"Buyurtmani ochib bo‘lmadi: {exc}")


@router.callback_query(F.data.startswith("order:status:"))
async def order_set_status(call: CallbackQuery) -> None:
    if not await require_admin(call):
        return
    await call.answer()
    _, _, oid, status = (call.data or "").split(":", 3)
    try:
        await admin_api(
            call.from_user.id, "PATCH", f"{API_PATHS['orders']}/{quote(oid)}",
            payload={"status": status},
        )
        if call.message:
            await call.message.answer(f"Buyurtma #{oid} holati: {status}")
    except APIError as exc:
        if call.message:
            await call.message.answer(f"Holat o‘zgarmadi: {exc}")


async def help_requests_menu(message: Message, user_id: int) -> None:
    result = await admin_api(user_id, "GET", API_PATHS["help_requests"], params={"limit": 15})
    requests = items_from(result, "help_requests")
    rows = []
    for item in requests[:15]:
        rid = ident(item)
        if rid:
            rows.append([InlineKeyboardButton(
                text=f"#{rid} · {display(item.get('status'))} · {display(item.get('name'))}"[:60],
                callback_data=f"help:view:{rid}",
            )])
    rows.append([InlineKeyboardButton(text="⬅️ Admin panel", callback_data="adm:home")])
    await message.answer("Yordam so‘rovlari:" if requests else "Yordam so‘rovlari yo‘q.", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("help:view:"))
async def help_request_view(call: CallbackQuery) -> None:
    if not await require_admin(call):
        return
    await call.answer()
    rid = call.data.rsplit(":", 1)[1]  # type: ignore[union-attr]
    try:
        result = await admin_api(call.from_user.id, "GET", f"{API_PATHS['help_requests']}/{quote(rid)}")
        item = record_from(result, "help_request", "request")
        text = (
            f"🆘 Murojaat #{rid}\n"
            f"Ism: {display(item.get('name'))}\n"
            f"Username: @{item.get('username')}\n" if item.get("username") else
            f"🆘 Murojaat #{rid}\nIsm: {display(item.get('name'))}\n"
        )
        text += (
            f"Telegram ID: {display(item.get('telegram_user_id'))}\n"
            f"Holat: {display(item.get('status'))}\n"
            f"Sana: {display(item.get('created_at'))}\n\n"
            f"{display(item.get('message'))}"
        )
        rows = [[
            InlineKeyboardButton(text=status, callback_data=f"help:status:{rid}:{status.lower().replace(' ', '_')}")
            for status in ("New", "In Progress", "Resolved")
        ]]
        if call.message:
            image = item.get("photo_file_id") or item.get("image")
            if image:
                try:
                    await call.message.answer_photo(str(image), caption=text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
                    return
                except Exception:
                    pass
            await call.message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    except APIError as exc:
        if call.message:
            await call.message.answer(f"Murojaat ochilmadi: {exc}")


@router.callback_query(F.data.startswith("help:status:"))
async def help_set_status(call: CallbackQuery) -> None:
    if not await require_admin(call):
        return
    await call.answer()
    _, _, rid, status = (call.data or "").split(":", 3)
    try:
        await admin_api(
            call.from_user.id, "PATCH", f"{API_PATHS['help_requests']}/{quote(rid)}",
            payload={"status": status.replace("_", " ")},
        )
        if call.message:
            await call.message.answer(f"Murojaat holati yangilandi: {status.replace('_', ' ')}")
    except APIError as exc:
        if call.message:
            await call.message.answer(f"Holat o‘zgarmadi: {exc}")


async def discounts_menu(message: Message, user_id: int) -> None:
    result = await admin_api(user_id, "GET", API_PATHS["discounts"])
    discounts = items_from(result, "discounts")
    rows = [[InlineKeyboardButton(text="➕ Chegirma yaratish", callback_data="discount:add")]]
    for item in discounts[:15]:
        did = ident(item)
        if did:
            state = "yoqilgan" if item.get("is_active", item.get("active", False)) else "o‘chiq"
            rows.append([InlineKeyboardButton(
                text=f"{item.get('name', 'Chegirma')} · {display(item.get('percent'))}% · {state}"[:60],
                callback_data=f"discount:view:{did}",
            )])
    rows.append([InlineKeyboardButton(text="⬅️ Admin panel", callback_data="adm:home")])
    await message.answer("Chegirmalar:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data == "discount:add")
async def discount_add_start(call: CallbackQuery, state: FSMContext) -> None:
    if not await require_admin(call):
        return
    await call.answer()
    await state.clear()
    await state.set_state(DiscountFlow.name)
    if call.message:
        await call.message.answer("Chegirma nomini kiriting:")


@router.message(DiscountFlow.name)
async def discount_name(message: Message, state: FSMContext) -> None:
    if not await require_admin(message):
        return
    name = (message.text or "").strip()
    if not name:
        await message.answer("Nom bo‘sh bo‘lmasin.")
        return
    await state.update_data(discount_name=name)
    await state.set_state(DiscountFlow.percent)
    await message.answer("Chegirma foizini kiriting (1–100):")


@router.message(DiscountFlow.percent)
async def discount_percent(message: Message, state: FSMContext) -> None:
    if not await require_admin(message):
        return
    try:
        percent = float((message.text or "").replace(",", "."))
        if not 0 < percent <= 100:
            raise ValueError
    except ValueError:
        await message.answer("Foiz 1 dan 100 gacha bo‘lishi kerak.")
        return
    data = await state.get_data()
    try:
        await admin_api(
            message.from_user.id,  # type: ignore[union-attr]
            "POST",
            API_PATHS["discounts"],
            payload={"name": data["discount_name"], "percent": percent, "is_active": True},
        )
        await state.clear()
        await message.answer("Chegirma yaratildi va yoqildi.")
    except APIError as exc:
        await message.answer(f"Chegirma yaratilmadi: {exc}")


@router.callback_query(F.data.startswith("discount:view:"))
async def discount_view(call: CallbackQuery) -> None:
    if not await require_admin(call):
        return
    await call.answer()
    did = call.data.rsplit(":", 1)[1]  # type: ignore[union-attr]
    try:
        result = await admin_api(call.from_user.id, "GET", f"{API_PATHS['discounts']}/{quote(did)}")
        discount = record_from(result, "discount")
        active = bool(discount.get("is_active", discount.get("active", False)))
        rows = [[
            InlineKeyboardButton(text="O‘chirish" if active else "Yoqish", callback_data=f"discount:toggle:{did}:{0 if active else 1}"),
            InlineKeyboardButton(text="🗑 O‘chirish", callback_data=f"discount:delete:{did}"),
        ]]
        if call.message:
            await call.message.answer(
                f"🏷 {display(discount.get('name'))}\nFoiz: {display(discount.get('percent'))}%\nHolat: {'yoqilgan' if active else 'o‘chiq'}",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
            )
    except APIError as exc:
        if call.message:
            await call.message.answer(f"Chegirma ochilmadi: {exc}")


@router.callback_query(F.data.startswith("discount:toggle:"))
async def discount_toggle(call: CallbackQuery) -> None:
    if not await require_admin(call):
        return
    await call.answer()
    _, _, did, active = (call.data or "").split(":", 3)
    try:
        await admin_api(
            call.from_user.id, "PATCH", f"{API_PATHS['discounts']}/{quote(did)}",
            payload={"is_active": bool(int(active))},
        )
        if call.message:
            await call.message.answer("Chegirma holati yangilandi.")
    except APIError as exc:
        if call.message:
            await call.message.answer(f"Holat yangilanmadi: {exc}")


@router.callback_query(F.data.startswith("discount:delete:"))
async def discount_delete(call: CallbackQuery) -> None:
    if not await require_admin(call):
        return
    await call.answer()
    did = call.data.rsplit(":", 1)[1]  # type: ignore[union-attr]
    try:
        await admin_api(call.from_user.id, "DELETE", f"{API_PATHS['discounts']}/{quote(did)}")
        if call.message:
            await call.message.answer("Chegirma o‘chirildi.")
    except APIError as exc:
        if call.message:
            await call.message.answer(f"O‘chirib bo‘lmadi: {exc}")


async def channels_menu(message: Message, user_id: int) -> None:
    result = await admin_api(user_id, "GET", API_PATHS["channels"])
    channels = items_from(result, "channels")
    rows = [
        [InlineKeyboardButton(text="➕ Kanal ulash", callback_data="channel:add")],
        [InlineKeyboardButton(text="📦 Mahsulot posti", callback_data="post:start:product")],
        [InlineKeyboardButton(text="🎌 Anime kiyim posti", callback_data="post:start:anime")],
        [InlineKeyboardButton(text="🖼 Poster yuborish", callback_data="post:start:poster")],
        [InlineKeyboardButton(text="✍️ Oddiy post yuborish", callback_data="post:start:simple")],
    ]
    for channel in channels[:10]:
        cid = str(channel.get("chat_id") or channel.get("username") or ident(channel))
        label = str(channel.get("title") or channel.get("username") or cid)
        if cid:
            rows.append([InlineKeyboardButton(
                text=f"🧪 Test: {label}"[:60],
                callback_data=f"channel:test:{cid}",
            )])
            rows.append([InlineKeyboardButton(
                text=f"🗑 O‘chirish: {label}"[:60],
                callback_data=f"channel:delete:{cid}",
            )])
    rows.append([InlineKeyboardButton(text="⬅️ Admin panel", callback_data="adm:home")])
    await message.answer(
        ("Ulangan kanallar:\n" + "\n".join(
            f"• {c.get('title') or c.get('username') or c.get('chat_id')}"
            for c in channels
        )) if channels else "Hozircha kanal ulanmagan.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data == "channel:add")
async def channel_add_start(call: CallbackQuery, state: FSMContext) -> None:
    if not await require_admin(call):
        return
    await call.answer()
    await state.set_state(ChannelFlow.channel)
    if call.message:
        await call.message.answer("Kanal @username yoki Telegram channel ID’ni yuboring. Bot kanal administratori bo‘lishi kerak.")


@router.message(ChannelFlow.channel)
async def channel_add_save(message: Message, state: FSMContext) -> None:
    if not await require_admin(message):
        return
    if not bot:
        return
    channel_ref = (message.text or "").strip()
    if not channel_ref:
        await message.answer("Kanal username yoki ID kiriting.")
        return
    try:
        chat = await bot.get_chat(channel_ref)
        me = await bot.get_me()
        member = await bot.get_chat_member(chat.id, me.id)
        if member.status not in ("administrator", "creator"):
            await message.answer("Bot bu kanalda administrator emas. Avval botni admin qilib, qayta yuboring.")
            return
        await admin_api(
            message.from_user.id,  # type: ignore[union-attr]
            "POST",
            API_PATHS["channels"],
            payload={
                "chat_id": str(chat.id),
                "username": getattr(chat, "username", None),
                "title": getattr(chat, "title", None),
            },
        )
        await state.clear()
        await message.answer(f"Kanal ulandi: {getattr(chat, 'title', None) or channel_ref}")
    except APIError as exc:
        await message.answer(f"Kanal API’da saqlanmadi: {exc}")
    except Exception as exc:
        log.info("Channel validation failed: %s", exc)
        await message.answer("Kanalni tekshirib bo‘lmadi. Bot kanalga admin qilinganini va kanal ID/username to‘g‘riligini tekshiring.")


@router.callback_query(F.data.startswith("channel:test:"))
async def channel_test(call: CallbackQuery) -> None:
    if not await require_admin(call):
        return
    await call.answer()
    channel_id = call.data.rsplit(":", 1)[1]  # type: ignore[union-attr]
    try:
        if not bot:
            raise APIError("Bot ishga tushmagan.")
        await bot.send_message(channel_id, f"{SHOP_NAME}: test xabari. Kanal ulanishi ishlayapti.")
        if call.message:
            await call.message.answer("Test posti kanalga yuborildi.")
    except APIError as exc:
        if call.message:
            await call.message.answer(f"Test posti yuborilmadi: {exc}")
    except Exception as exc:
        log.info("Channel test failed: %s", exc)
        if call.message:
            await call.message.answer("Bot kanalga post yubora olmadi. Bot admin huquqi va kanalni tekshiring.")


@router.callback_query(F.data.startswith("channel:delete:"))
async def channel_delete(call: CallbackQuery) -> None:
    if not await require_admin(call):
        return
    await call.answer()
    channel_id = call.data.rsplit(":", 1)[1]  # type: ignore[union-attr]
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Ha, uzish", callback_data=f"channel:delyes:{channel_id}"),
        InlineKeyboardButton(text="Bekor qilish", callback_data="adm:channels"),
    ]])
    if call.message:
        await call.message.answer("Kanalni bot sozlamalaridan uzishni tasdiqlaysizmi?", reply_markup=keyboard)


@router.callback_query(F.data.startswith("channel:delyes:"))
async def channel_delete_yes(call: CallbackQuery) -> None:
    if not await require_admin(call):
        return
    await call.answer()
    channel_id = call.data.rsplit(":", 1)[1]  # type: ignore[union-attr]
    try:
        await admin_api(call.from_user.id, "DELETE", f"{API_PATHS['channels']}/{quote(channel_id)}")
        if call.message:
            await call.message.answer("Kanal bot sozlamalaridan uzildi.")
    except APIError as exc:
        if call.message:
            await call.message.answer(f"Kanal uzilmadi: {exc}")


@router.callback_query(F.data.startswith("post:start:"))
async def post_start(call: CallbackQuery, state: FSMContext) -> None:
    if not await require_admin(call):
        return
    await call.answer()
    post_type = call.data.rsplit(":", 1)[1]  # type: ignore[union-attr]
    await state.clear()
    await state.update_data(post_type=post_type)
    if post_type in ("product", "anime"):
        if call.message:
            await list_products_for_post(call.from_user.id, call.message)
    else:
        await state.set_state(PostFlow.media)
        if call.message:
            await call.message.answer("Poster yoki post uchun rasm/video yuboring. Bekor qilish: /cancel")


@router.callback_query(F.data.startswith("post:product:"))
async def post_select_product(call: CallbackQuery, state: FSMContext) -> None:
    if not await require_admin(call):
        return
    await call.answer()
    pid = call.data.rsplit(":", 1)[1]  # type: ignore[union-attr]
    try:
        result = await admin_api(call.from_user.id, "GET", f"{API_PATHS['products']}/{quote(pid)}")
        product = record_from(result, "product")
        if not product:
            raise APIError("Mahsulot topilmadi.")
        post_type = (await state.get_data()).get("post_type", "product")
        caption = (
            f"{'🎌 ' if post_type == 'anime' else '👕 '}{display(product.get('name'))}\n"
            f"Narxi: {display(product.get('price'))}\n"
            f"Ranglar: {display(product.get('colors'))}\n"
            f"O‘lchamlar: {display(product.get('sizes') or product.get('variants'))}\n"
            f"Mavjud: {display(product.get('stock'))}"
        )
        if product.get("description"):
            caption += f"\n{product['description']}"
        file_id = product.get("image_file_id") or product.get("image")
        if not file_id:
            raise APIError("Mahsulotga rasm biriktirilmagan.")
        mini_url = f"{MINI_APP_URL}?product_id={quote(pid)}" if MINI_APP_URL else None
        await state.update_data(
            post_product_id=pid,
            post_caption=caption,
            post_photo=str(file_id),
            post_video=None,
            post_mini_url=mini_url,
            post_title=product.get("name", ""),
        )
        if call.message:
            await ask_for_channels(call.from_user.id, state, call.message)
    except APIError as exc:
        if call.message:
            await call.message.answer(f"Mahsulot posti tayyorlanmadi: {exc}")


@router.message(PostFlow.media)
async def post_receive_media(message: Message, state: FSMContext) -> None:
    if not await require_admin(message):
        return
    if message.photo:
        await state.update_data(post_photo=message.photo[-1].file_id, post_video=None)
    elif message.video:
        await state.update_data(post_video=message.video.file_id, post_photo=None)
    else:
        await message.answer("Iltimos, rasm yoki video yuboring.")
        return
    await state.set_state(PostFlow.title)
    await message.answer("Post sarlavhasini yozing:")


@router.message(PostFlow.title)
async def post_receive_title(message: Message, state: FSMContext) -> None:
    if not await require_admin(message):
        return
    title = (message.text or "").strip()
    if not title:
        await message.answer("Sarlavha bo‘sh bo‘lmasin.")
        return
    await state.update_data(post_title=title)
    await state.set_state(PostFlow.details)
    await message.answer("Qo‘shimcha matnni yozing yoki — bilan o‘tkazing:")


@router.message(PostFlow.details)
async def post_receive_details(message: Message, state: FSMContext) -> None:
    if not await require_admin(message):
        return
    data = await state.get_data()
    details = (message.text or "").strip()
    if details in ("—", "-"):
        details = ""
    caption = str(data.get("post_title", ""))
    if details:
        caption += f"\n\n{details}"
    await state.update_data(post_caption=caption)
    if not message.from_user:
        return
    try:
        await ask_for_channels(message.from_user.id, state, message)
    except APIError as exc:
        await message.answer(f"Kanallar ro‘yxatini olib bo‘lmadi: {exc}")


@router.callback_query(F.data.startswith("post:channel:"))
async def post_select_channel(call: CallbackQuery, state: FSMContext) -> None:
    if not await require_admin(call):
        return
    await call.answer()
    channel_id = call.data.rsplit(":", 1)[1]  # type: ignore[union-attr]
    try:
        result = await admin_api(call.from_user.id, "GET", API_PATHS["channels"])
        channels = items_from(result, "channels")
        channel = next(
            (c for c in channels if str(c.get("chat_id") or c.get("username") or ident(c)) == channel_id),
            {"chat_id": channel_id, "title": channel_id},
        )
        await state.update_data(
            post_channel_id=str(channel.get("chat_id") or channel_id),
            post_channel_name=channel.get("title") or channel.get("username") or channel_id,
        )
        await show_post_preview(call.from_user.id, call.message, state)
    except APIError as exc:
        if call.message:
            await call.message.answer(f"Kanal ma’lumotini olib bo‘lmadi: {exc}")


async def show_post_preview(user_id: int, message: Message | None, state: FSMContext) -> None:
    if not message:
        return
    data = await state.get_data()
    caption = str(data.get("post_caption", ""))
    url = data.get("post_mini_url")
    keyboard_rows = []
    if url:
        keyboard_rows.append([InlineKeyboardButton(text="🛍 Buyurtma berish / Ko‘rish", url=url)])
    keyboard_rows.append([
        InlineKeyboardButton(text="✅ Kanalga yuborish", callback_data="post:send"),
        InlineKeyboardButton(text="✏️ Tahrirlash", callback_data="post:edit"),
    ])
    keyboard_rows.append([InlineKeyboardButton(text="❌ Bekor qilish", callback_data="post:cancel")])
    markup = InlineKeyboardMarkup(inline_keyboard=keyboard_rows)
    await message.answer(f"Preview · {data.get('post_channel_name', '')}\n\n{caption}", reply_markup=markup)
    photo = data.get("post_photo")
    video = data.get("post_video")
    if photo:
        await message.answer_photo(photo, caption="Rasm preview")
    elif video:
        await message.answer_video(video, caption="Video preview")


@router.callback_query(F.data == "post:edit")
async def post_edit_start(call: CallbackQuery, state: FSMContext) -> None:
    if not await require_admin(call):
        return
    await call.answer()
    await state.set_state(PostFlow.edit_caption)
    if call.message:
        await call.message.answer("Post matnining yangi variantini yuboring. Eski sarlavha va matn to‘liq almashtiriladi:")


@router.message(PostFlow.edit_caption)
async def post_edit_caption(message: Message, state: FSMContext) -> None:
    if not await require_admin(message):
        return
    caption = (message.text or "").strip()
    if not caption:
        await message.answer("Matn bo‘sh bo‘lmasin.")
        return
    await state.update_data(post_caption=caption)
    await state.set_state(PostFlow.channel)
    await show_post_preview(message.from_user.id, message, state)  # type: ignore[union-attr]


@router.callback_query(F.data == "post:send")
async def post_send(call: CallbackQuery, state: FSMContext) -> None:
    if not await require_admin(call):
        return
    await call.answer()
    if not bot:
        return
    data = await state.get_data()
    channel_id = data.get("post_channel_id")
    caption = str(data.get("post_caption", ""))
    photo = data.get("post_photo")
    video = data.get("post_video")
    if not channel_id:
        if call.message:
            await call.message.answer("Kanal tanlanmagan. Post bekor qilindi.")
        await state.clear()
        return
    try:
        markup = None
        if data.get("post_mini_url"):
            markup = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🛍 Buyurtma berish / Ko‘rish", url=data["post_mini_url"])
            ]])
        if photo:
            sent = await bot.send_photo(channel_id, photo, caption=caption, reply_markup=markup)
        elif video:
            sent = await bot.send_video(channel_id, video, caption=caption, reply_markup=markup)
        else:
            sent = await bot.send_message(channel_id, caption, reply_markup=markup)
        history = {
            "channel_id": str(channel_id),
            "channel_name": data.get("post_channel_name"),
            "post_type": data.get("post_type", "simple"),
            "post_name": data.get("post_title") or data.get("post_caption", "")[:100],
            "product_id": data.get("post_product_id"),
            "telegram_message_id": sent.message_id,
            "status": "sent",
        }
        try:
            await admin_api(call.from_user.id, "POST", API_PATHS["channel_posts"], payload=history)
        except APIError:
            log.exception("Post was sent but history could not be saved")
        await state.clear()
        if call.message:
            await call.message.answer("Post kanalga yuborildi.")
    except APIError as exc:
        if call.message:
            await call.message.answer(f"API xatosi: {exc}")
    except Exception as exc:
        log.info("Telegram channel post failed: %s", exc)
        try:
            await admin_api(
                call.from_user.id, "POST", API_PATHS["channel_posts"],
                payload={
                    "channel_id": str(channel_id),
                    "post_type": data.get("post_type", "simple"),
                    "post_name": data.get("post_title", ""),
                    "status": "error",
                    "error": "Telegram channel send failed",
                },
            )
        except Exception:
            log.exception("Could not record failed channel post")
        if call.message:
            await call.message.answer("Kanalga yuborilmadi. Bot admin huquqini va rasm/video turini tekshiring.")


@router.callback_query(F.data == "post:cancel")
async def post_cancel(call: CallbackQuery, state: FSMContext) -> None:
    if not await require_admin(call):
        return
    await call.answer("Bekor qilindi")
    await state.clear()
    if call.message:
        await call.message.answer("Post yuborish bekor qilindi.")


async def app_info_menu(message: Message, user_id: int) -> None:
    result = await admin_api(user_id, "GET", API_PATHS["app_info"])
    info = record_from(result, "app_info")
    fields = [
        ("onboarding_text", "Onboarding matni"),
        ("guide", "Guide"),
        ("about", "Do‘kon haqida"),
        ("support", "Support ma’lumoti"),
    ]
    rows = [
        [InlineKeyboardButton(text=f"✏️ {label}", callback_data=f"app:edit:{key}")]
        for key, label in fields
    ]
    rows.append([InlineKeyboardButton(text="⬅️ Admin panel", callback_data="adm:home")])
    await message.answer(
        "Ilova ma’lumoti:\n"
        + "\n".join(f"{label}: {display(info.get(key))}" for key, label in fields),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data.startswith("app:edit:"))
async def app_info_edit_start(call: CallbackQuery, state: FSMContext) -> None:
    if not await require_admin(call):
        return
    await call.answer()
    field = call.data.rsplit(":", 1)[1]  # type: ignore[union-attr]
    await state.update_data(text_edit_kind="app", text_edit_field=field)
    await state.set_state(TextEditFlow.value)
    if call.message:
        await call.message.answer(f"Yangi {field} matnini yuboring:")


async def settings_menu(message: Message, user_id: int) -> None:
    result = await admin_api(user_id, "GET", API_PATHS["settings"])
    settings = record_from(result, "settings")
    rows = [
        [InlineKeyboardButton(text="✏️ Shop nomi", callback_data="settings:edit:shop_name")],
        [InlineKeyboardButton(text="✏️ Welcome matni", callback_data="settings:edit:welcome_text")],
        [InlineKeyboardButton(text="⬅️ Admin panel", callback_data="adm:home")],
    ]
    await message.answer(
        f"⚙️ Sozlamalar\nShop nomi: {display(settings.get('shop_name'), SHOP_NAME)}\n"
        f"Welcome: {display(settings.get('welcome_text'), WELCOME_TEXT)}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data.startswith("settings:edit:"))
async def settings_edit_start(call: CallbackQuery, state: FSMContext) -> None:
    if not await require_admin(call):
        return
    await call.answer()
    field = call.data.rsplit(":", 1)[1]  # type: ignore[union-attr]
    await state.update_data(text_edit_kind="settings", text_edit_field=field)
    await state.set_state(TextEditFlow.value)
    if call.message:
        await call.message.answer(f"Yangi qiymatni yuboring ({field}):")


@router.message(TextEditFlow.value)
async def save_text_edit(message: Message, state: FSMContext) -> None:
    if not await require_admin(message):
        return
    value = (message.text or "").strip()
    if not value:
        await message.answer("Qiymat bo‘sh bo‘lmasin.")
        return
    data = await state.get_data()
    kind = data.get("text_edit_kind")
    field = data.get("text_edit_field")
    path = API_PATHS["app_info"] if kind == "app" else API_PATHS["settings"]
    try:
        await admin_api(
            message.from_user.id,  # type: ignore[union-attr]
            "PATCH",
            path,
            payload={field: value},
        )
        await state.clear()
        await message.answer("Ma’lumot yangilandi.")
    except APIError as exc:
        await message.answer(f"Saqlab bo‘lmadi: {exc}")


async def history_menu(message: Message, user_id: int) -> None:
    result = await admin_api(user_id, "GET", API_PATHS["channel_posts"], params={"limit": 20})
    posts = items_from(result, "posts", "channel_posts")
    lines = []
    for post in posts[:20]:
        lines.append(
            f"• {display(post.get('created_at'))} · {display(post.get('channel_name') or post.get('channel_id'))}"
            f" · {display(post.get('post_type'))} · {display(post.get('post_name'))}"
            f" · {display(post.get('status'))}"
        )
    text = "📜 Yuborilgan postlar\n" + ("\n".join(lines) if lines else "Hozircha tarix yo‘q.")
    await message.answer(text, reply_markup=back_keyboard())


@router.message(F.chat.type == ChatType.PRIVATE)
async def private_fallback(message: Message) -> None:
    if message.text and message.text.startswith("/"):
        await message.answer("Buyruqni tushunmadim. /start yoki /help’dan foydalaning.")


async def order_notification_worker() -> None:
    """Polls an optional API notification feed for newly created Mini App orders."""
    after_id = ""
    while True:
        try:
            result = await api.request(
                "GET",
                API_PATHS["notifications"],
                params={"after_id": after_id, "type": "new_order"},
            )
            notifications = items_from(result, "notifications")
            for notification in notifications:
                order = notification.get("order") if isinstance(notification.get("order"), dict) else notification
                oid = str(order.get("id") or order.get("order_id") or "")
                text = (
                    f"🆕 Yangi buyurtma #{oid}\n"
                    f"Foydalanuvchi: {display(order.get('name') or order.get('user_name'))}\n"
                    f"Telegram ID: {display(order.get('telegram_user_id'))}\n"
                    f"Mahsulotlar: {display(order.get('items') or order.get('products'))}\n"
                    f"Jami: {display(order.get('total_amount') or order.get('total'))}"
                )
                for admin_id in ADMIN_IDS:
                    try:
                        await bot.send_message(admin_id, text)  # type: ignore[union-attr]
                    except Exception:
                        log.exception("Could not notify admin %s of order %s", admin_id, oid)
                marker = notification.get("id") or order.get("id") or order.get("order_id")
                if marker is not None:
                    after_id = str(marker)
        except APIError as exc:
            log.warning("Order notification poll failed: %s", exc)
        except Exception:
            log.exception("Unexpected order notification polling error")
        await asyncio.sleep(max(5, NOTIFICATION_POLL_SECONDS))


async def main() -> None:
    global bot
    missing = [
        name for name, value in (
            ("BOT_TOKEN", BOT_TOKEN),
            ("API_BASE_URL", API_BASE_URL),
            ("BOT_API_SECRET", BOT_API_SECRET),
        )
        if not value
    ]
    if not ADMIN_IDS:
        missing.append("ADMIN_IDS")
    if missing:
        raise RuntimeError("Missing required environment variables: " + ", ".join(missing))

    bot = Bot(token=BOT_TOKEN)
    await api.start()
    poll_task: asyncio.Task[None] | None = None
    try:
        if NOTIFICATION_POLL_SECONDS > 0:
            poll_task = asyncio.create_task(order_notification_worker())
        await dp.start_polling(bot)
    finally:
        if poll_task:
            poll_task.cancel()
            try:
                await poll_task
            except asyncio.CancelledError:
                pass
        await api.close()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Bot stopped")
