# routes.py
"""
Production-grade Route Handlers for Telegram Bot.
Acts as the Controller layer: parses input -> calls service -> returns view (message).
Fully integrated with LanguageService for multi-language support.
Async-compatible version.
"""

import logging
import os
import aiofiles
from datetime import datetime
from typing import List, Optional

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaDocument
)
from telegram.ext import ContextTypes
from telegram.constants import ParseMode
from telegram.helpers import escape_markdown

from db import get_db
from services import (
    UserService,
    ProductService,
    OrderService,
    PaymentService,
    LanguageService
)
from config import settings

logger = logging.getLogger(__name__)

# ==============================================================================
# HELPER FUNCTIONS
# ==============================================================================

def escape_md(text: str) -> str:
    """
    Helper to escape Markdown characters (specifically for legacy Markdown).
    Prevents crashes when usernames contain _, *, etc.
    """
    if not text:
        return ""
    # In legacy ParseMode.MARKDOWN, we mainly need to escape _ and * and `
    return text.replace("_", "\\_").replace("*", "\\*").replace("`", "\\`")

async def reply_or_edit(update: Update, text: str, reply_markup: Optional[InlineKeyboardMarkup] = None):
    """
    Smart helper to handle responses from both Commands (Reply) and Buttons (Edit).
    """
    try:
        if update.callback_query:
            # Try to edit the existing message
            await update.callback_query.message.edit_text(
                text=text,
                reply_markup=reply_markup,
                parse_mode=ParseMode.MARKDOWN
            )
        elif update.message:
            # Reply with a new message
            await update.message.reply_text(
                text=text,
                reply_markup=reply_markup,
                parse_mode=ParseMode.MARKDOWN
            )
    except Exception as e:
        logger.warning(f"Failed to reply/edit message: {e}")
        # Fallback: if edit fails (e.g., message too old), try sending a new one
        if update.effective_chat:
            await update.get_bot().send_message(
                chat_id=update.effective_chat.id,
                text=text,
                reply_markup=reply_markup,
                parse_mode=ParseMode.MARKDOWN
            )

# ==============================================================================
# MAIN MENU & ENTRY POINTS
# ==============================================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handle /start command.
    Initializes user and shows the main menu with translated buttons.
    """
    user = update.effective_user
    chat_id = update.effective_chat.id

    async with get_db() as session:
        # Get or create user in DB
        db_user = await UserService.get_or_create_user(
            session,
            telegram_id=user.id,
            username=user.username or "Unknown"
        )
        
        lang = db_user.language
        
        # Get welcome text
        text = LanguageService.t(lang, "welcome")
        
        # Build Main Menu Keyboard with Translated Keys
        keyboard = [
            [InlineKeyboardButton(LanguageService.t(lang, "menu_stock"), callback_data="menu:products")],
            [
                InlineKeyboardButton(LanguageService.t(lang, "menu_profile"), callback_data="menu:profile"), 
                InlineKeyboardButton(LanguageService.t(lang, "menu_statistics"), callback_data="menu:statistics")
            ],
            [
                InlineKeyboardButton(LanguageService.t(lang, "menu_languages"), callback_data="menu:languages"), 
                InlineKeyboardButton(LanguageService.t(lang, "menu_information"), callback_data="menu:info")
            ],
            [InlineKeyboardButton(LanguageService.t(lang, "menu_contact"), callback_data="menu:contact")],
        ]

        await context.bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN
        )

# ==============================================================================
# CALLBACK QUERY DISPATCHER
# ==============================================================================

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Central dispatcher for all inline button clicks.
    Parses `query.data` and routes to the specific handler function.
    """
    query = update.callback_query
    await query.answer()  # Acknowledge click to stop loading animation
    
    data = query.data
    
    # ------------------ ROUTING LOGIC ------------------
    try:
        if data == "main_menu":
            await start(update, context)
            
        # --- MENUS ---
        elif data == "menu:products":
            await products_menu(update, context)
        elif data == "menu:profile":
            await profile_menu(update, context)
        elif data == "menu:statistics":
            await statistics_menu(update, context)
        elif data == "menu:languages":
            await languages_menu(update, context)
        elif data == "menu:info":
            await info_menu(update, context)
        elif data == "menu:contact":
            await contact_menu(update, context)
            
        # --- ACTIONS: PROFILE & PAYMENTS ---
        elif data == "profile:add_balance":
            await add_balance_menu(update, context)
        elif data == "profile:history":
            await topup_history_menu(update, context)
        elif data.startswith("pay:"):
            # Format: pay:method_name (e.g., pay:binance)
            method = data.split(":")[1]
            await show_payment_address(update, context, method)
        elif data.startswith("paid:"):
            # Format: paid:method_name
            method = data.split(":")[1]
            await confirm_payment_request(update, context, method)
            
        # --- ACTIONS: PRODUCTS & ORDERING ---
        elif data.startswith("buy:"):
            # Format: buy:product_id
            p_id = int(data.split(":")[1])
            await initiate_purchase(update, context, p_id)
        elif data.startswith("delivery:"):
            # Format: delivery:type:order_id (e.g., delivery:text:55)
            _, dtype, order_id = data.split(":")
            await deliver_order(update, context, int(order_id), dtype)
            
        # --- ACTIONS: SETTINGS ---
        elif data.startswith("lang:"):
            # Format: lang:en
            code = data.split(":")[1]
            await set_language(update, context, code)
            
        else:
            logger.warning(f"Unhandled callback data: {data}")
            await query.message.reply_text("⚠ Unknown action.")

    except Exception as e:
        logger.error(f"Error handling callback {data}: {e}", exc_info=True)
        # Try to notify user
        try:
            await query.message.reply_text("An unexpected error occurred. Please try again later.")
        except:
            pass

# ==============================================================================
# 1. PRODUCTS & BUYING FLOW
# ==============================================================================

async def products_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Use effective_user to handle both commands and callbacks
    user_id = update.effective_user.id
    
    # If called via callback, answer it to stop spinner
    if update.callback_query:
        await update.callback_query.answer()
    
    async with get_db() as session:
        user = await UserService.get_user_by_telegram_id(session, user_id)
        lang = user.language if user else "en"
        
        products = await ProductService.get_available_products(session)
        
        if not products:
            msg = LanguageService.t(lang, "out_of_stock")
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙", callback_data="main_menu")]])
            await reply_or_edit(update, msg, kb)
            return

        # Create buttons for each product
        keyboard = []
        for p in products:
            stock = await ProductService.get_stock_count(session, p.id)
            display_name = LanguageService.t(lang, p.name)
            
            if stock > 0:
                btn_text = f"{display_name} - ${p.price_usd:.2f} ({stock})"
                callback = f"buy:{p.id}"
            else:
                btn_text = f"{display_name} - ❌ {LanguageService.t(lang, 'out_of_stock')}"
                callback = "ignore" 
                
            keyboard.append([InlineKeyboardButton(btn_text, callback_data=callback)])
            
        keyboard.append([InlineKeyboardButton("🔙 " + LanguageService.t(lang, "menu_stock"), callback_data="main_menu")])

        await reply_or_edit(
            update,
            LanguageService.t(lang, "buy_product_selection_message"),
            InlineKeyboardMarkup(keyboard)
        )


async def initiate_purchase(update: Update, context: ContextTypes.DEFAULT_TYPE, product_id: int):
    """
    Step 1: Check balance/stock, deduct balance, create order.
    Then ask for delivery method.
    """
    user_id = update.effective_user.id
    
    async with get_db() as session:
        user = await UserService.get_user_by_telegram_id(session, user_id)
        lang = user.language
        product = await ProductService.get_product(session, product_id)
        
        # Validation
        if not product or not product.is_active:
            await reply_or_edit(update, LanguageService.t(lang, "out_of_stock"))
            return

        # Attempt to create order (Atomic Transaction)
        order, error_key = await OrderService.create_order(session, user, product)
        
        # --- ERROR HANDLING ---
        if error_key == "insufficient_balance":
            msg = LanguageService.t(lang, "insufficient_balance")
            kb = [
                [InlineKeyboardButton(LanguageService.t(lang, "profile_add_balance_btn"), callback_data="profile:add_balance")],
                [InlineKeyboardButton("🔙", callback_data="menu:products")]
            ]
            await reply_or_edit(update, msg, InlineKeyboardMarkup(kb))
            return
            
        elif error_key == "out_of_stock":
            msg = LanguageService.t(lang, "out_of_stock")
            await reply_or_edit(update, msg)
            return
            
        elif error_key != "success" or not order:
            await reply_or_edit(update, LanguageService.t(lang, "generic_error"))
            return
            
        # --- SUCCESS: Order Created ---
        msg_text = LanguageService.t(lang, "choose_delivery")
        
        keyboard = [
            [InlineKeyboardButton(LanguageService.t(lang, "delivery_text_btn"), callback_data=f"delivery:text:{order.id}")],
            [InlineKeyboardButton(LanguageService.t(lang, "delivery_file_btn"), callback_data=f"delivery:file:{order.id}")]
        ]
        
        await reply_or_edit(
            update,
            f"✅ **#{order.id}**\n\n{msg_text}",
            InlineKeyboardMarkup(keyboard)
        )


async def deliver_order(update: Update, context: ContextTypes.DEFAULT_TYPE, order_id: int, method: str):
    """
    Final Step: Send code via chosen method and show receipt.
    Uses aiofiles for non-blocking file handling.
    """
    # This is likely only called via callback, but we use safe access just in case
    user_id = update.effective_user.id
    
    async with get_db() as session:
        order = await OrderService.get_order(session, order_id)
        if not order:
            await reply_or_edit(update, "Order not found.")
            return

        user = await UserService.get_user_by_telegram_id(session, user_id)
        lang = user.language
        code_content = await OrderService.get_code_content(session, order_id)
        product_name = LanguageService.t(lang, order.product.name)

        # 1. DELIVER CONTENT
        if method == "text":
            text_header = LanguageService.t(lang, "code_sent_text")
            final_msg = f"{text_header}\n\n`{code_content}`"
            await reply_or_edit(update, final_msg)
            
        elif method == "file":
            file_header = LanguageService.t(lang, "code_sent_file")
            
            # Create temp file synchronously (fast enough) or async if preferred
            file_path = OrderService.create_txt_file(code_content, order.id, lang)
            
            try:
                # If it's a callback, try to delete the menu message to clean up
                if update.callback_query:
                    await update.callback_query.message.delete()
                
                await context.bot.send_message(chat_id=user.telegram_id, text=file_header)
                
                # Async File Sending
                async with aiofiles.open(file_path, "rb") as f:
                    file_data = await f.read()
                    await context.bot.send_document(
                        chat_id=user.telegram_id,
                        document=file_data,
                        filename=f"Order_{order.id}.txt"
                    )
            except Exception as e:
                logger.error(f"File sending failed: {e}")
                await context.bot.send_message(
                    chat_id=user.telegram_id, 
                    text=LanguageService.t(lang, "generic_error")
                )
            finally:
                if os.path.exists(file_path):
                    os.remove(file_path)

        # 2. SEND RECEIPT
        receipt_text = (
            f"━━━━━━━━━━━━━━━\n"
            f"**{LanguageService.t(lang, 'receipt_header')}**\n"
            f"━━━━━━━━━━━━━━━\n"
            f"🆔 **{LanguageService.t(lang, 'receipt_order_id')}:** `{order.id}`\n"
            f"📦 **{LanguageService.t(lang, 'receipt_product')}:** {product_name}\n"
            f"💵 **{LanguageService.t(lang, 'receipt_price')}:** ${order.price_usd:.2f}\n"
            f"📨 **{LanguageService.t(lang, 'receipt_delivery_type')}:** {method.upper()}\n"
            f"✅ **{LanguageService.t(lang, 'receipt_status')}:** {LanguageService.t(lang, 'receipt_status_completed')}\n"
        )
        
        await context.bot.send_message(
            chat_id=user.telegram_id,
            text=receipt_text,
            parse_mode=ParseMode.MARKDOWN
        )

# ==============================================================================
# 2. PROFILE & PAYMENTS
# ==============================================================================

async def profile_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if update.callback_query:
        await update.callback_query.answer()
    
    async with get_db() as session:
        user = await UserService.get_user_by_telegram_id(session, user_id)
        lang = user.language
        
        lbl_header = LanguageService.t(lang, "profile_header")
        lbl_user = LanguageService.t(lang, "profile_username_label")
        lbl_id = LanguageService.t(lang, "profile_userid_label")
        lbl_bal = LanguageService.t(lang, "profile_balance_label")

        # FIX: Escape username to prevent Markdown errors with underscores
        safe_username = escape_md(user.username or "Unknown")
        
        text = (
            f"{lbl_header}\n"
            f"━━━━━━━━━━━━━━━\n"
            f"**{lbl_id}:** `{user.telegram_id}`\n"
            f"**{lbl_user}:** @{safe_username}\n"
            f"**{lbl_bal}:** `${user.balance_usd:.2f}`\n"
        )
        
        keyboard = [
            [InlineKeyboardButton(LanguageService.t(lang, "profile_add_balance_btn"), callback_data="profile:add_balance")],
            [InlineKeyboardButton(LanguageService.t(lang, "profile_topup_history_btn"), callback_data="profile:history")],
            [InlineKeyboardButton("🔙", callback_data="main_menu")]
        ]
        
        await reply_or_edit(update, text, InlineKeyboardMarkup(keyboard))

async def add_balance_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if update.callback_query:
        await update.callback_query.answer()

    async with get_db() as session:
        user = await UserService.get_user_by_telegram_id(session, user_id)
        lang = user.language

        keyboard = [
            [InlineKeyboardButton(LanguageService.t(lang, "payment_binance_btn"), callback_data="pay:binance")],
            [InlineKeyboardButton(LanguageService.t(lang, "payment_bybit_btn"), callback_data="pay:bybit")],
            [InlineKeyboardButton(LanguageService.t(lang, "payment_usdt_btn"), callback_data="pay:usdt")],
            [InlineKeyboardButton("🔙", callback_data="menu:profile")]
        ]
        
        await reply_or_edit(
            update,
            LanguageService.t(lang, "payment_selection_message"),
            InlineKeyboardMarkup(keyboard)
        )

async def show_payment_address(update: Update, context: ContextTypes.DEFAULT_TYPE, method: str):
    user_id = update.effective_user.id
    if update.callback_query:
        await update.callback_query.answer()
    
    async with get_db() as session:
        user = await UserService.get_user_by_telegram_id(session, user_id)
        lang = user.language
        
        # Pure logic call (no DB)
        address_info = PaymentService.get_payment_address(method, lang)
        instruction = LanguageService.t(lang, "topup_instructions")
    
    text = (
        f"{address_info}\n\n"
        f"⚠ {instruction}"
    )
    
    keyboard = [
        [InlineKeyboardButton(LanguageService.t(lang, "payment_i_paid_btn"), callback_data=f"paid:{method}")],
        [InlineKeyboardButton("🔙", callback_data="profile:add_balance")]
    ]
    
    await reply_or_edit(update, text, InlineKeyboardMarkup(keyboard))

async def confirm_payment_request(update: Update, context: ContextTypes.DEFAULT_TYPE, method: str):
    user_id = update.effective_user.id
    if update.callback_query:
        await update.callback_query.answer()
    
    async with get_db() as session:
        user = await UserService.get_user_by_telegram_id(session, user_id)
        lang = user.language
        
        # Create request
        await PaymentService.create_topup_request(session, user_id, method, "Manual Confirmation Required")
        
        confirmation_text = LanguageService.t(lang, "topup_submitted")
        
        # Notify Admin (Async)
        if settings.ADMIN_CHAT_ID:
            try:
                msg_template = LanguageService.t("en", "admin_new_topup_notification")
                # FIX: Escape username here as well to prevent Admin notification failure
                safe_username = escape_md(user.username or "Unknown")
                admin_msg = msg_template.format(username=safe_username, txid_note=f"{method.upper()} Manual Click")
                
                await context.bot.send_message(
                    chat_id=settings.ADMIN_CHAT_ID,
                    text=admin_msg,
                    parse_mode=ParseMode.MARKDOWN
                )
            except Exception as e:
                logger.error(f"Failed to notify admin: {e}")

    await reply_or_edit(
        update, 
        confirmation_text,
        InlineKeyboardMarkup([[InlineKeyboardButton("🏠", callback_data="main_menu")]])
    )

async def topup_history_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if update.callback_query:
        await update.callback_query.answer()
    
    async with get_db() as session:
        user = await UserService.get_user_by_telegram_id(session, user_id)
        lang = user.language
        history = await PaymentService.get_user_topup_history(session, user_id)
        
        header = LanguageService.t(lang, "profile_topup_history_btn")
        
        if not history:
            text = f"📜 **{header}**\n\n{LanguageService.t(lang, 'profile_no_topup_history')}"
        else:
            text = f"📜 **{header}**\n\n"
            for t in history:
                status_icon = "✅" if t.status == "approved" else "⏳" if t.status == "pending" else "❌"
                date_str = t.created_at.strftime("%Y-%m-%d %H:%M")
                amount = f"${t.amount_usd:.2f}" if t.amount_usd > 0 else "(PENDING)"
                text += f"{status_icon} `{date_str}` | {amount}\n"

    keyboard = [[InlineKeyboardButton("🔙", callback_data="menu:profile")]]
    
    await reply_or_edit(update, text, InlineKeyboardMarkup(keyboard))

# ==============================================================================
# 3. STATISTICS, LANGUAGES, INFO, CONTACT
# ==============================================================================

async def statistics_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if update.callback_query:
        await update.callback_query.answer()
    
    async with get_db() as session:
        user = await UserService.get_user_by_telegram_id(session, user_id)
        stats_text = await OrderService.get_user_statistics(session, user_id, user.language)
        
    keyboard = [[InlineKeyboardButton("🔙", callback_data="main_menu")]]
    await reply_or_edit(update, stats_text, InlineKeyboardMarkup(keyboard))

async def languages_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if update.callback_query:
        await update.callback_query.answer()
    
    async with get_db() as session:
        user = await UserService.get_user_by_telegram_id(session, user_id)
        header = LanguageService.t(user.language, "lang_selection_header")
        
        keyboard = [
            [InlineKeyboardButton(LanguageService.t(user.language, "lang_english_btn"), callback_data="lang:en")],
            [InlineKeyboardButton(LanguageService.t(user.language, "lang_russian_btn"), callback_data="lang:ru")],
            [InlineKeyboardButton(LanguageService.t(user.language, "lang_arabic_btn"), callback_data="lang:ar")],
            [InlineKeyboardButton("🔙", callback_data="main_menu")]
        ]
    
    await reply_or_edit(update, header, InlineKeyboardMarkup(keyboard))

async def set_language(update: Update, context: ContextTypes.DEFAULT_TYPE, lang_code: str):
    user_id = update.effective_user.id
    # No reply_or_edit needed here as we usually just answer and redirect to start
    if update.callback_query:
        async with get_db() as session:
            await UserService.set_language(session, user_id, lang_code)
            confirmation = LanguageService.t(lang_code, "lang_changed_confirmation")
        
        await update.callback_query.answer(confirmation)
        await start(update, context)

async def info_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if update.callback_query:
        await update.callback_query.answer()
    
    async with get_db() as session:
        user = await UserService.get_user_by_telegram_id(session, user_id)
        lang = user.language
        
        header = LanguageService.t(lang, "info_header")
        desc = LanguageService.t(lang, "info_bot_description")
        how = LanguageService.t(lang, "info_how_it_works")
        methods = LanguageService.t(lang, "info_delivery_methods")
        refund = LanguageService.t(lang, "info_refund_policy")
        
        text = f"ℹ **{header}**\n\n{desc}\n\n{how}\n\n{methods}\n\n{refund}"
    
    keyboard = [[InlineKeyboardButton("🔙", callback_data="main_menu")]]
    await reply_or_edit(update, text, InlineKeyboardMarkup(keyboard))

async def contact_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Shows contact information, fetching the specific Admin username from Config.
    """
    user_id = update.effective_user.id
    if update.callback_query:
        await update.callback_query.answer()
    
    async with get_db() as session:
        user = await UserService.get_user_by_telegram_id(session, user_id)
        lang = user.language
        
        header = LanguageService.t(lang, "contact_header")
        msg = LanguageService.t(lang, "contact_manager_msg")
        
        # 1. Fetch
        raw_username = settings.SUPPORTUSERNAME
        safe_username = raw_username.lstrip('@') if raw_username else "Support"

        # 2. Escape for Markdown to prevent "Can't parse entities" errors
        safe_username_escaped = escape_md(safe_username)
        
        text = (
            f"📞 **{header}**\n\n"
            f"{msg}\n\n"
            f"👤 **Manager:** @{safe_username_escaped}"
        )
    
    keyboard = [[InlineKeyboardButton("🔙", callback_data="main_menu")]]
    await reply_or_edit(update, text, InlineKeyboardMarkup(keyboard))

# ==============================================================================
# COMMAND DISPATCHER (For /buy, /balance, etc.)
# ==============================================================================

async def handle_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Routes commands like /buy, /balance to their respective menu functions.
    No longer requires hacking the Update object; handlers now use `effective_user`.
    """
    if not update.message or not update.message.text:
        return

    command = update.message.text.lower().split()[0] # e.g., "/buy"
    
    try:
        # Route directly to the function. 
        # The functions now use `update.effective_user` which works for both.
        if command == "/start":
            await start(update, context)
        elif command == "/buy":
            await products_menu(update, context)
        elif command == "/balance":
            await profile_menu(update, context)
        elif command == "/support":
            await contact_menu(update, context)
        elif command == "/lang":
            await languages_menu(update, context)
    except Exception as e:
        logger.error(f"Command handler error: {e}")