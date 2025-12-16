# routes.py
"""
Production-grade Route Handlers for Telegram Bot.
Acts as the Controller layer: parses input -> calls service -> returns view (message).
Fully integrated with LanguageService for multi-language support.
"""

import logging
import os
from datetime import datetime
from typing import List

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaDocument
)
from telegram.ext import ContextTypes
from telegram.constants import ParseMode

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
# MAIN MENU & ENTRY POINTS
# ==============================================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handle /start command.
    Initializes user and shows the main menu with translated buttons.
    """
    user = update.effective_user
    chat_id = update.effective_chat.id

    with get_db() as session:
        # Get or create user in DB
        db_user = UserService.get_or_create_user(
            session=session,
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
            # We don't have user lang here easily without DB, defaulting to English for generic errors
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
    query = update.callback_query
    user_id = query.from_user.id
    
    with get_db() as session:
        user = UserService.get_user_by_telegram_id(session, user_id)
        lang = user.language if user else "en"
        
        # Fetch active products
        products = ProductService.get_available_products(session)
        
        if not products:
            # Although unlikely, if no products exist
            await query.message.edit_text(
                LanguageService.t(lang, "out_of_stock"),
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙", callback_data="main_menu")]])
            )
            return

        # Create buttons for each product
        keyboard = []
        for p in products:
            # Check stock
            stock = ProductService.get_stock_count(session, p.id)
            
            # Translate Product Name (name in DB acts as key, e.g., 'product_60_uc')
            display_name = LanguageService.t(lang, p.name)
            
            if stock > 0:
                btn_text = f"{display_name} - ${p.price_usd:.2f} ({stock})"
                callback = f"buy:{p.id}"
            else:
                btn_text = f"{display_name} - ❌ {LanguageService.t(lang, 'out_of_stock')}"
                callback = "ignore" # Or show alert
                
            keyboard.append([InlineKeyboardButton(btn_text, callback_data=callback)])
            
        keyboard.append([InlineKeyboardButton("🔙 " + LanguageService.t(lang, "menu_stock"), callback_data="main_menu")])

        await query.message.edit_text(
            LanguageService.t(lang, "buy_product_selection_message"),
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN
        )


async def initiate_purchase(update: Update, context: ContextTypes.DEFAULT_TYPE, product_id: int):
    """
    Step 1: Check balance/stock, deduct balance, create order.
    Then ask for delivery method.
    """
    query = update.callback_query
    user_id = query.from_user.id
    
    with get_db() as session:
        user = UserService.get_user_by_telegram_id(session, user_id)
        lang = user.language
        product = ProductService.get_product(session, product_id)
        
        # Validation
        if not product or not product.is_active:
            await query.message.reply_text(LanguageService.t(lang, "out_of_stock"))
            return

        # Attempt to create order (Atomic Transaction)
        order, error_key = OrderService.create_order(session, user, product)
        
        # --- ERROR HANDLING ---
        if error_key == "insufficient_balance":
            msg = LanguageService.t(lang, "insufficient_balance")
            # Suggest Top Up
            kb = [
                [InlineKeyboardButton(LanguageService.t(lang, "profile_add_balance_btn"), callback_data="profile:add_balance")],
                [InlineKeyboardButton("🔙", callback_data="menu:products")]
            ]
            await query.message.edit_text(msg, reply_markup=InlineKeyboardMarkup(kb))
            return
            
        elif error_key == "out_of_stock":
            msg = LanguageService.t(lang, "out_of_stock")
            await query.message.reply_text(msg)
            return
            
        elif error_key != "success" or not order:
            await query.message.reply_text(LanguageService.t(lang, "generic_error"))
            return
            
        # --- SUCCESS: Order Created ---
        # Ask for delivery method
        msg_text = LanguageService.t(lang, "choose_delivery")
        
        keyboard = [
            [InlineKeyboardButton(LanguageService.t(lang, "delivery_text_btn"), callback_data=f"delivery:text:{order.id}")],
            [InlineKeyboardButton(LanguageService.t(lang, "delivery_file_btn"), callback_data=f"delivery:file:{order.id}")]
        ]
        
        await query.message.edit_text(
            f"✅ **#{order.id}**\n\n{msg_text}",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN
        )


async def deliver_order(update: Update, context: ContextTypes.DEFAULT_TYPE, order_id: int, method: str):
    """
    Final Step: Send code via chosen method and show receipt.
    """
    query = update.callback_query
    
    with get_db() as session:
        order = OrderService.get_order(session, order_id)
        if not order:
            await query.message.edit_text("Order not found.")
            return

        user = UserService.get_user_by_telegram_id(session, query.from_user.id)
        lang = user.language
        code_content = OrderService.get_code_content(session, order_id)
        
        # Translate Product Name
        product_name = LanguageService.t(lang, order.product.name)

        # 1. DELIVER CONTENT
        if method == "text":
            text_header = LanguageService.t(lang, "code_sent_text")
            final_msg = f"{text_header}\n\n`{code_content}`"
            await query.message.edit_text(final_msg, parse_mode=ParseMode.MARKDOWN)
            
        elif method == "file":
            file_header = LanguageService.t(lang, "code_sent_file")
            # Create temp file
            file_path = OrderService.create_txt_file(code_content, order.id, lang)
            
            try:
                await query.message.delete()
                await context.bot.send_message(chat_id=user.telegram_id, text=file_header)
                await context.bot.send_document(
                    chat_id=user.telegram_id,
                    document=open(file_path, "rb"),
                    filename=f"Order_{order.id}.txt"
                )
            except Exception as e:
                logger.error(f"File sending failed: {e}")
                await context.bot.send_message(chat_id=user.telegram_id, text=LanguageService.t(lang, "generic_error"))
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
    query = update.callback_query
    
    with get_db() as session:
        user = UserService.get_user_by_telegram_id(session, query.from_user.id)
        lang = user.language
        
        # Translate labels
        lbl_header = LanguageService.t(lang, "profile_header")
        lbl_user = LanguageService.t(lang, "profile_username_label")
        lbl_id = LanguageService.t(lang, "profile_userid_label")
        lbl_bal = LanguageService.t(lang, "profile_balance_label")
        
        text = (
            f"{lbl_header}\n"
            f"━━━━━━━━━━━━━━━\n"
            f"**{lbl_id}:** `{user.telegram_id}`\n"
            f"**{lbl_user}:** @{user.username}\n"
            f"**{lbl_bal}:** `${user.balance_usd:.2f}`\n"
        )
        
        keyboard = [
            [InlineKeyboardButton(LanguageService.t(lang, "profile_add_balance_btn"), callback_data="profile:add_balance")],
            [InlineKeyboardButton(LanguageService.t(lang, "profile_topup_history_btn"), callback_data="profile:history")],
            [InlineKeyboardButton("🔙", callback_data="main_menu")]
        ]
        
        await query.message.edit_text(
            text, 
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN
        )

async def add_balance_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id

    with get_db() as session:
        user = UserService.get_user_by_telegram_id(session, user_id)
        lang = user.language

        keyboard = [
            [InlineKeyboardButton(LanguageService.t(lang, "payment_binance_btn"), callback_data="pay:binance")],
            [InlineKeyboardButton(LanguageService.t(lang, "payment_bybit_btn"), callback_data="pay:bybit")],
            [InlineKeyboardButton(LanguageService.t(lang, "payment_usdt_btn"), callback_data="pay:usdt")],
            [InlineKeyboardButton("🔙", callback_data="menu:profile")]
        ]
        
        await query.message.edit_text(
            LanguageService.t(lang, "payment_selection_message"),
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN
        )

async def show_payment_address(update: Update, context: ContextTypes.DEFAULT_TYPE, method: str):
    query = update.callback_query
    user_id = query.from_user.id
    
    with get_db() as session:
        user = UserService.get_user_by_telegram_id(session, user_id)
        lang = user.language
        
        # Retrieve address text
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
    
    await query.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN
    )

async def confirm_payment_request(update: Update, context: ContextTypes.DEFAULT_TYPE, method: str):
    query = update.callback_query
    user_id = query.from_user.id
    
    with get_db() as session:
        user = UserService.get_user_by_telegram_id(session, user_id)
        lang = user.language
        
        # Create pending topup record
        PaymentService.create_topup_request(session, user_id, method, "Manual Confirmation Required")
        
        confirmation_text = LanguageService.t(lang, "topup_submitted")
        
        # Notify Admin (Translate notification template)
        if settings.ADMIN_CHAT_ID:
            try:
                # Format: 🔔 New TopUp Request from User @{username} | TXID/Note: {txid_note}
                msg_template = LanguageService.t("en", "admin_new_topup_notification")
                admin_msg = msg_template.format(username=user.username, txid_note=f"{method.upper()} Manual Click")
                
                await context.bot.send_message(
                    chat_id=settings.ADMIN_CHAT_ID,
                    text=admin_msg
                )
            except Exception as e:
                logger.error(f"Failed to notify admin: {e}")

    await query.message.edit_text(
        confirmation_text,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠", callback_data="main_menu")]])
    )

async def topup_history_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    
    with get_db() as session:
        user = UserService.get_user_by_telegram_id(session, user_id)
        lang = user.language
        
        history = PaymentService.get_user_topup_history(session, user_id)
        
        header = LanguageService.t(lang, "profile_topup_history_btn")
        
        if not history:
            text = f"📜 **{header}**\n\n{LanguageService.t(lang, 'profile_no_topup_history')}"
        else:
            text = f"📜 **{header}**\n\n"
            for t in history:
                status_icon = "✅" if t.status == "approved" else "⏳" if t.status == "pending" else "❌"
                date_str = t.created_at.strftime("%Y-%m-%d %H:%M")
                amount = f"${t.amount_usd:.2f}" if t.amount_usd > 0 else "(PENDING)"
                # Just show the status and date/amount nicely
                text += f"{status_icon} `{date_str}` | {amount}\n"

    keyboard = [[InlineKeyboardButton("🔙", callback_data="menu:profile")]]
    
    await query.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN
    )

# ==============================================================================
# 3. STATISTICS, LANGUAGES, INFO, CONTACT
# ==============================================================================

async def statistics_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    
    with get_db() as session:
        user = UserService.get_user_by_telegram_id(session, user_id)
        # Service handles translation logic for statistics
        stats_text = OrderService.get_user_statistics(session, user_id, user.language)
        
    keyboard = [[InlineKeyboardButton("🔙", callback_data="main_menu")]]
    
    await query.message.edit_text(
        stats_text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN
    )

async def languages_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    
    # Retrieve current language for header
    with get_db() as session:
        user = UserService.get_user_by_telegram_id(session, user_id)
        header = LanguageService.t(user.language, "lang_selection_header")
        
        # Build buttons using translation keys from Service
        keyboard = [
            [InlineKeyboardButton(LanguageService.t(user.language, "lang_english_btn"), callback_data="lang:en")],
            [InlineKeyboardButton(LanguageService.t(user.language, "lang_russian_btn"), callback_data="lang:ru")],
            [InlineKeyboardButton(LanguageService.t(user.language, "lang_arabic_btn"), callback_data="lang:ar")],
            [InlineKeyboardButton("🔙", callback_data="main_menu")]
        ]
    
    await query.message.edit_text(header, reply_markup=InlineKeyboardMarkup(keyboard))

async def set_language(update: Update, context: ContextTypes.DEFAULT_TYPE, lang_code: str):
    query = update.callback_query
    user_id = query.from_user.id
    
    with get_db() as session:
        UserService.set_language(session, user_id, lang_code)
        confirmation = LanguageService.t(lang_code, "lang_changed_confirmation")
        
    await query.answer(confirmation)
    # Refresh menu in new language
    await start(update, context)

async def info_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    
    with get_db() as session:
        user = UserService.get_user_by_telegram_id(session, user_id)
        lang = user.language
        
        header = LanguageService.t(lang, "info_header")
        desc = LanguageService.t(lang, "info_bot_description")
        how = LanguageService.t(lang, "info_how_it_works")
        methods = LanguageService.t(lang, "info_delivery_methods")
        refund = LanguageService.t(lang, "info_refund_policy")
        
        text = f"ℹ **{header}**\n\n{desc}\n\n{how}\n\n{methods}\n\n{refund}"
    
    keyboard = [[InlineKeyboardButton("🔙", callback_data="main_menu")]]
    await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)

async def contact_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    
    with get_db() as session:
        user = UserService.get_user_by_telegram_id(session, user_id)
        lang = user.language
        
        header = LanguageService.t(lang, "contact_header")
        msg = LanguageService.t(lang, "contact_manager_msg")
        
        # Admin availability check (mock logic or real if needed)
        # Using hardcoded admin username for now, or fetch from config
        admin_username = "SupportAdmin" 
        
        text = (
            f"📞 **{header}**\n\n"
            f"{msg}\n\n"
            f"👤 **Manager:** @{admin_username}"
        )
    
    keyboard = [[InlineKeyboardButton("🔙", callback_data="main_menu")]]
    await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)

# ==============================================================================
# COMMAND DISPATCHER (For /buy, /balance, etc.)
# ==============================================================================

async def handle_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Routes commands like /buy, /balance to their respective menu functions.
    Simulates a callback query for consistency.
    """
    if not update.message or not update.message.text:
        return

    command = update.message.text.lower().split()[0] # e.g., "/buy"
    
    # Mock a callback query object so we can reuse the existing async menu functions
    # We assign a dummy 'answer' method to prevent crashes when functions call query.answer()
    update.callback_query = type('obj', (object,), {
        'message': update.message, 
        'from_user': update.effective_user,
        'data': 'command_simulated',
        'answer': lambda *args, **kwargs: None
    })
    
    try:
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