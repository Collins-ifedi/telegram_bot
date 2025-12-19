# services.py
"""
Production-grade business logic layer.
Handles complex operations, database transactions, and business rules.
Updated for AsyncSQLAlchemy (v2.0+) compatibility.
"""

import os
import logging
import datetime
from typing import List, Optional, Tuple

# Async imports
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, desc
from sqlalchemy.orm import selectinload

from models import (
    User,
    Product,
    ProductCode,
    Order,
    TopUp,
    TopUpStatus,
    OrderStatus,
    DeliveryType,
    AdminActionLog
)

# Initialize logging
logger = logging.getLogger(__name__)

# Directory for temporary delivery files
TEMP_DIR = "temp_orders"
os.makedirs(TEMP_DIR, exist_ok=True)


# ===============================
# USER SERVICES
# ===============================

class UserService:

    @staticmethod
    async def get_or_create_user(db: AsyncSession, telegram_id: int, username: str) -> User:
        """
        Retrieves a user by Telegram ID or creates a new one if not found.
        Async compatible.
        """
        try:
            # Construct the select statement
            stmt = select(User).where(User.telegram_id == str(telegram_id))
            result = await db.execute(stmt)
            user = result.scalars().first()

            if not user:
                user = User(
                    telegram_id=str(telegram_id),
                    username=username,
                    balance_usd=0.0
                )
                db.add(user)
                await db.commit()
                await db.refresh(user)
                logger.info(f"New user created: {username} ({telegram_id})")
            else:
                # Update username if it changed
                if user.username != username:
                    user.username = username
                    await db.commit()
            return user
        except Exception as e:
            logger.error(f"Error in get_or_create_user: {e}")
            await db.rollback()
            raise

    @staticmethod
    async def get_user_by_telegram_id(db: AsyncSession, telegram_id: int) -> Optional[User]:
        stmt = select(User).where(User.telegram_id == str(telegram_id))
        result = await db.execute(stmt)
        return result.scalars().first()

    @staticmethod
    async def set_language(db: AsyncSession, telegram_id: int, lang_code: str):
        user = await UserService.get_user_by_telegram_id(db, telegram_id)
        if user:
            user.language = lang_code
            await db.commit()


# ===============================
# PRODUCT & STOCK SERVICES
# ===============================

class ProductService:

    @staticmethod
    async def get_available_products(db: AsyncSession) -> List[Product]:
        """
        Returns products that are active. 
        """
        stmt = select(Product).where(Product.is_active == True)
        result = await db.execute(stmt)
        return result.scalars().all()

    @staticmethod
    async def get_product(db: AsyncSession, product_id: int) -> Optional[Product]:
        stmt = select(Product).where(Product.id == product_id)
        result = await db.execute(stmt)
        return result.scalars().first()

    @staticmethod
    async def get_stock_count(db: AsyncSession, product_id: int) -> int:
        stmt = select(func.count()).select_from(ProductCode).where(
            ProductCode.product_id == product_id,
            ProductCode.is_sold == False
        )
        result = await db.execute(stmt)
        return result.scalar()

    @staticmethod
    async def add_product(db: AsyncSession, name: str, price: float) -> Product:
        # Note: 'name' is stored as the translation key (e.g., 'product_60_uc')
        product = Product(name=name, price_usd=price, is_active=True)
        db.add(product)
        await db.commit()
        await db.refresh(product)
        return product

    @staticmethod
    async def add_codes(db: AsyncSession, product_id: int, codes_list: List[str]) -> int:
        """
        Bulk uploads codes. Ignores duplicates if code is unique constraint.
        Returns count of successfully added codes.
        """
        count = 0
        for code_str in codes_list:
            code_str = code_str.strip()
            if not code_str:
                continue
            
            # Async check for existence
            stmt = select(ProductCode).where(ProductCode.code == code_str)
            result = await db.execute(stmt)
            exists = result.scalars().first()

            if not exists:
                new_code = ProductCode(
                    product_id=product_id,
                    code=code_str,
                    is_sold=False
                )
                db.add(new_code)
                count += 1
        await db.commit()
        return count


# ===============================
# ORDER & DELIVERY SERVICES
# ===============================

class OrderService:

    @staticmethod
    async def create_order(db: AsyncSession, user: User, product: Product) -> Tuple[Optional[Order], str]:
        """
        Core Transaction:
        1. Checks balance.
        2. Locks a product code row (to prevent race conditions).
        3. Deducts balance.
        4. Marks code as sold.
        5. Creates order record.
        
        Returns: (Order Object, Error Message Key)
        """
        try:
            # 1. Balance Check
            if user.balance_usd < product.price_usd:
                return None, "insufficient_balance"

            # 2. Find and Lock Unsold Code
            # with_for_update() prevents double-selling race conditions
            stmt = select(ProductCode).where(
                ProductCode.product_id == product.id,
                ProductCode.is_sold == False
            ).with_for_update(skip_locked=True).limit(1)
            
            result = await db.execute(stmt)
            code = result.scalars().first()

            if not code:
                return None, "out_of_stock"

            # 3. Execute Transaction
            user.balance_usd -= product.price_usd
            
            code.is_sold = True
            code.sold_at = datetime.datetime.utcnow()

            order = Order(
                user_id=user.id,
                product_id=product.id,
                product_code_id=code.id,
                price_usd=product.price_usd,
                delivery_type=DeliveryType.TEXT, # Default, updated in handler if needed
                status=OrderStatus.COMPLETED
            )
            
            db.add(order)
            await db.commit()
            await db.refresh(order)
            
            logger.info(f"Order {order.id} created for User {user.id}")
            return order, "success"

        except Exception as e:
            await db.rollback()
            logger.error(f"Transaction failed: {e}")
            return None, "generic_error"

    @staticmethod
    async def get_order(db: AsyncSession, order_id: int) -> Optional[Order]:
        stmt = select(Order).where(Order.id == order_id)
        result = await db.execute(stmt)
        return result.scalars().first()

    @staticmethod
    async def get_code_content(db: AsyncSession, order_id: int) -> str:
        # We need the product_code relationship. 
        # Models should be using lazy="selectin", or we explicitly join here.
        stmt = select(Order).where(Order.id == order_id)
        result = await db.execute(stmt)
        order = result.scalars().first()
        
        if order and order.product_code:
            return order.product_code.code
        return ""

    @staticmethod
    def create_txt_file(code_content: str, order_id: int, lang_code: str) -> str:
        """
        Creates a temporary .txt file for file delivery.
        Returns the file path.
        (Kept sync as file I/O is minimal, but can be switched to aiofiles if needed)
        """
        filename = f"order_{order_id}_code.txt"
        file_path = os.path.join(TEMP_DIR, filename)
        
        thank_you_message = LanguageService.t(lang_code, "file_delivery_thank_you")
        code_label = LanguageService.t(lang_code, "file_delivery_code_label")
        
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(f"{thank_you_message}\n\n{code_label}\n{code_content}")
            
        return file_path

    @staticmethod
    async def get_user_statistics(db: AsyncSession, telegram_id: int, lang_code: str) -> str:
        """
        Aggregates purchase history and top-ups for the 'Statistics' button.
        """
        user = await UserService.get_user_by_telegram_id(db, telegram_id)
        if not user:
            return LanguageService.t(lang_code, "error_user_not_found")

        # Async Purchases Count
        stmt_orders = select(func.count()).select_from(Order).where(Order.user_id == user.id)
        result_orders = await db.execute(stmt_orders)
        total_orders = result_orders.scalar()
        
        # Async Total Spent
        stmt_spent = select(func.coalesce(func.sum(Order.price_usd), 0.0)).where(Order.user_id == user.id)
        result_spent = await db.execute(stmt_spent)
        total_spent = result_spent.scalar()

        # Async Total TopUps
        stmt_topup = select(func.coalesce(func.sum(TopUp.amount_usd), 0.0)).where(
            TopUp.user_id == user.id,
            TopUp.status == TopUpStatus.APPROVED
        )
        result_topup = await db.execute(stmt_topup)
        total_topup = result_topup.scalar()

        if total_orders == 0 and total_topup == 0:
            return LanguageService.t(lang_code, "stats_no_history")

        # Retrieve translated strings
        header = LanguageService.t(lang_code, "stats_header")
        user_label = LanguageService.t(lang_code, "stats_user_label")
        products_bought_label = LanguageService.t(lang_code, "stats_products_bought")
        total_spent_label = LanguageService.t(lang_code, "stats_total_spent")
        total_topup_label = LanguageService.t(lang_code, "stats_total_topup")
        current_balance_label = LanguageService.t(lang_code, "stats_current_balance")

        return (
            f"📊 **{header}**\n\n"
            f"👤 **{user_label}:** @{user.username}\n"
            f"📦 **{products_bought_label}:** {total_orders}\n"
            f"💸 **{total_spent_label}:** ${total_spent:.2f}\n"
            f"💰 **{total_topup_label}:** ${total_topup:.2f}\n"
            f"💳 **{current_balance_label}:** ${user.balance_usd:.2f}"
        )


# ===============================
# PAYMENT SERVICES
# ===============================

class PaymentService:

    @staticmethod
    def get_payment_address(method_key: str, lang_code: str) -> str:
        """
        Returns the payment address/instruction for the given method key.
        Pure logic, no DB required.
        """
        # Labels
        binance_label = LanguageService.t(lang_code, "payment_binance_label")
        bybit_label = LanguageService.t(lang_code, "payment_bybit_label")
        usdt_label = LanguageService.t(lang_code, "payment_usdt_label")
        
        # Notes
        txid_note = LanguageService.t(lang_code, "payment_txid_note")
        network_note = LanguageService.t(lang_code, "payment_usdt_network_note")
        
        addresses = {
            "binance": f"🆔 {binance_label}: `123456789`\n({txid_note})",
            "bybit": f"🆔 {bybit_label}: `987654321`\n({txid_note})",
            "usdt": f"🔗 {usdt_label}: `TWM...ExampleAddress...`\n({network_note})"
        }
        return addresses.get(method_key, LanguageService.t(lang_code, "payment_unavailable"))

    @staticmethod
    async def create_topup_request(db: AsyncSession, telegram_id: int, method: str, txid_note: str) -> TopUp:
        """
        Creates a pending top-up request for Admin review.
        """
        user = await UserService.get_user_by_telegram_id(db, telegram_id)
        if not user:
            raise ValueError("User not found")

        topup = TopUp(
            user_id=user.id,
            amount_usd=0.0, 
            txid_or_note=f"{method.upper()} | {txid_note}",
            status=TopUpStatus.PENDING
        )
        db.add(topup)
        await db.commit()
        await db.refresh(topup)
        
        logger.info(f"TopUp Request created for User {user.id} - TXID: {txid_note}")
        return topup

    @staticmethod
    async def get_user_topup_history(db: AsyncSession, telegram_id: int) -> List[TopUp]:
        user = await UserService.get_user_by_telegram_id(db, telegram_id)
        if not user:
            return []
        
        stmt = select(TopUp).where(
            TopUp.user_id == user.id
        ).order_by(desc(TopUp.created_at)).limit(10)
        
        result = await db.execute(stmt)
        return result.scalars().all()


# ===============================
# ADMIN SERVICES
# ===============================

class AdminService:
    
    @staticmethod
    async def ban_user(db: AsyncSession, target_user_id: int, admin_id: int):
        stmt = select(User).where(User.id == target_user_id)
        result = await db.execute(stmt)
        user = result.scalars().first()
        
        if user:
            user.is_banned = True
            # Log action using English key for consistent admin logs
            log_action = f"{LanguageService.STRINGS['en']['admin_log_banned_user']} {user.username} ({user.telegram_id})"
            log = AdminActionLog(
                admin_id=admin_id, 
                action=log_action
            )
            db.add(log)
            await db.commit()

    @staticmethod
    async def approve_topup(db: AsyncSession, topup_id: int, admin_id: int, actual_amount: float):
        """
        Admin approves a top-up and manually sets the correct amount received.
        """
        stmt = select(TopUp).where(TopUp.id == topup_id)
        result = await db.execute(stmt)
        topup = result.scalars().first()
        
        if topup and topup.status == TopUpStatus.PENDING:
            topup.amount_usd = actual_amount
            topup.status = TopUpStatus.APPROVED
            topup.approved_at = datetime.datetime.utcnow()
            
            # Credit User
            # We need to fetch the user to update balance (if not eagerly loaded)
            # Assuming TopUp -> User is lazy='selectin' in updated models
            if topup.user:
                 topup.user.balance_usd += actual_amount
            
            # Log
            log_action = f"{LanguageService.STRINGS['en']['admin_log_topup_approved']} #{topup.id} for ${actual_amount}"
            log = AdminActionLog(
                admin_id=admin_id,
                action=log_action
            )
            db.add(log)
            await db.commit()
            return True
        return False

# ===============================
# LANGUAGE SERVICE
# ===============================

class LanguageService:
    
    # Complete Translation Dictionary
    STRINGS = {
        "en": {
            # Base Messages
            "welcome": "👋 Welcome to the Digital Store!",
            "out_of_stock": "❌ This product is currently out of stock.",
            "insufficient_balance": "❌ Insufficient balance. Please top up.",
            "generic_error": "An unexpected error occurred. Please try again later.",
            "error_user_not_found": "User not found.",

            # A) MAIN MENU BUTTONS (6)
            "menu_stock": "🛒 STOCKABLE UC CODES",
            "menu_profile": "👤 PROFILE",
            "menu_statistics": "📊 STATISTICS",
            "menu_languages": "🌐 LANGUAGES",
            "menu_information": "💡 INFORMATION",
            "menu_contact": "📞 CONTACT",

            # B) STOCKABLE UC CODES SUB-BUTTONS (6)
            "product_60_uc": "60 UC",
            "product_325_uc": "325 UC",
            "product_660_uc": "660 UC",
            "product_1800_uc": "1800 UC",
            "product_3850_uc": "3850 UC",
            "product_8100_uc": "8100 UC",

            # C) PROFILE SECTION
            "profile_header": "👤 YOUR PROFILE",
            "profile_username_label": "Username",
            "profile_userid_label": "User ID",
            "profile_balance_label": "Balance (USD)",
            "profile_add_balance_btn": "💰 ADD BALANCE",
            "profile_topup_history_btn": "📜 TOP-UP HISTORY",
            "profile_no_topup_history": "No top-up history found.",
            "payment_selection_message": "💳 Please select your preferred payment method:",
            "payment_binance_btn": "Binance Pay",
            "payment_bybit_btn": "Bybit Pay",
            "payment_usdt_btn": "USDT (TRC20)",
            "topup_instructions": "Send payment to the address below, then click 'I Paid'.",
            "payment_i_paid_btn": "✅ I PAID",
            "topup_submitted": "✅ Payment submitted for review! Please wait for admin approval.",
            "topup_pending": "⏳ Your top-up is pending admin approval.",
            "topup_approved": "✅ Your top-up of **${amount:.2f}** has been approved and credited!",
            "topup_rejected": "❌ Your top-up request was rejected. Please contact support.",
            # Payment labels
            "payment_binance_label": "Binance Pay ID",
            "payment_bybit_label": "Bybit UID",
            "payment_usdt_label": "USDT Address",
            "payment_txid_note": "Send payment and copy TXID/Note",
            "payment_usdt_network_note": "Only TRC20 network!",
            "payment_unavailable": "Payment method unavailable.",

            # D) STATISTICS SECTION
            "stats_header": "STATISTICS",
            "stats_user_label": "User",
            "stats_products_bought": "Products Bought",
            "stats_total_spent": "Total Spent",
            "stats_total_topup": "Total Top-Up",
            "stats_current_balance": "Current Balance",
            "stats_no_history": "You have no purchase or top-up history.",

            # E) LANGUAGE SELECTION
            "lang_selection_header": "🌐 Select your preferred language:",
            "lang_english_btn": "English (EN)",
            "lang_russian_btn": "Русский (RU)",
            "lang_arabic_btn": "العربية (AR)",
            "lang_changed_confirmation": "✅ Language changed successfully!",

            # F) INFORMATION SECTION
            "info_header": "💡 INFORMATION",
            "info_bot_description": "We offer instant delivery of digital codes for various games and services.",
            "info_how_it_works": "**How the bot works:**\n1. Select a product.\n2. Choose a delivery method (Text or File).\n3. Code is instantly delivered if stock/balance allows.",
            "info_delivery_methods": "**Delivery Methods:**\n- **Text:** Code is sent directly in a chat message.\n- **File:** Code is sent as a downloadable TXT file.",
            "info_refund_policy": "**Refund Policy:**\nAll digital code sales are final. Refunds are only processed if the code is proven to be invalid *at the time of delivery*.",
            "info_support_instructions": "For any issues, please contact the manager via the CONTACT button.",

            # G) CONTACT SECTION
            "contact_header": "📞 CONTACT",
            "contact_manager_msg": "Connecting you to a manager. Please describe your issue clearly.",
            "contact_admin_unavailable_msg": "The administrator is currently offline. Please try again later or leave a detailed message.",

            # H) BUY FLOW
            "buy_product_selection_message": "Select the product you wish to purchase:",
            "purchase_confirmation_message": "🛒 You are about to purchase **{product_name}** for **${price:.2f}**. Proceed?",
            "choose_delivery": "📬 Choose how you want to receive your code:",
            "delivery_text_btn": "Text Delivery",
            "delivery_file_btn": "TXT File Delivery",
            "code_sent_text": "✅ **Here is your code:**",
            "code_sent_file": "✅ **Here is your code file:**",
            "download_again_message": "You can download the code again from the receipt message.",
            "receipt_header": "🧾 PURCHASE RECEIPT",
            "receipt_order_id": "Order ID",
            "receipt_product": "Product",
            "receipt_price": "Price",
            "receipt_status": "Status",
            "receipt_delivery_type": "Delivery Type",
            "receipt_status_completed": "COMPLETED",
            # File content
            "file_delivery_thank_you": "Thank you for your purchase!",
            "file_delivery_code_label": "Your Code:",

            # I) ADMIN MESSAGES
            "admin_new_order_notification": "🔔 New Order: User @{username} purchased {product_name} for ${price:.2f}",
            "admin_new_topup_notification": "🔔 New TopUp Request from User @{username} | TXID/Note: {txid_note}",
            "admin_low_stock_warning": "⚠️ LOW STOCK ALERT: Product '{product_name}' has only {count} items left.",
            "admin_out_of_stock_alert": "🚫 OUT OF STOCK: Product '{product_name}' is now empty.",
            "admin_user_banned_msg": "❌ User @{username} has been banned.",
            "admin_user_unbanned_msg": "✅ User @{username} has been unbanned.",
            "admin_log_topup_approved": "Approved TopUp",
            "admin_log_topup_rejected": "Rejected TopUp",
            "admin_log_banned_user": "Banned user",

            # J) ERRORS & SYSTEM
            "error_database": "A database error occurred. The transaction has been rolled back.",
            "error_invalid_input": "Invalid input. Please check your message.",
            "error_action_not_allowed": "Action not allowed at this moment.",
            "error_user_banned_notice": "Your account is currently banned. Please contact support.",
        },
        "ru": {
            # Base Messages
            "welcome": "👋 Добро пожаловать в цифровой магазин!",
            "out_of_stock": "❌ Товар закончился.",
            "insufficient_balance": "❌ Недостаточно средств. Пополните баланс.",
            "generic_error": "Произошла непредвиденная ошибка. Пожалуйста, попробуйте позже.",
            "error_user_not_found": "Пользователь не найден.",

            # A) MAIN MENU BUTTONS
            "menu_stock": "🛒 КОДЫ UC В НАЛИЧИИ",
            "menu_profile": "👤 ПРОФИЛЬ",
            "menu_statistics": "📊 СТАТИСТИКА",
            "menu_languages": "🌐 ЯЗЫКИ",
            "menu_information": "💡 ИНФОРМАЦИЯ",
            "menu_contact": "📞 КОНТАКТ",

            # B) STOCKABLE UC CODES
            "product_60_uc": "60 UC",
            "product_325_uc": "325 UC",
            "product_660_uc": "660 UC",
            "product_1800_uc": "1800 UC",
            "product_3850_uc": "3850 UC",
            "product_8100_uc": "8100 UC",

            # C) PROFILE SECTION
            "profile_header": "👤 ВАШ ПРОФИЛЬ",
            "profile_username_label": "Имя пользователя",
            "profile_userid_label": "ID пользователя",
            "profile_balance_label": "Баланс (USD)",
            "profile_add_balance_btn": "💰 ПОПОЛНИТЬ БАЛАНС",
            "profile_topup_history_btn": "📜 ИСТОРИЯ ПОПОЛНЕНИЙ",
            "profile_no_topup_history": "История пополнений не найдена.",
            "payment_selection_message": "💳 Пожалуйста, выберите предпочитаемый способ оплаты:",
            "payment_binance_btn": "Binance Pay",
            "payment_bybit_btn": "Bybit Pay",
            "payment_usdt_btn": "USDT (TRC20)",
            "topup_instructions": "Отправьте оплату по указанному адресу, затем нажмите 'Я оплатил'.",
            "payment_i_paid_btn": "✅ Я ОПЛАТИЛ",
            "topup_submitted": "✅ Оплата отправлена на проверку! Ожидайте подтверждения администратора.",
            "topup_pending": "⏳ Ваше пополнение ожидает подтверждения администратора.",
            "topup_approved": "✅ Ваше пополнение на **${amount:.2f}** одобрено и зачислено!",
            "topup_rejected": "❌ Ваш запрос на пополнение отклонен. Свяжитесь со службой поддержки.",
            # Payment labels
            "payment_binance_label": "ID Binance Pay",
            "payment_bybit_label": "UID Bybit",
            "payment_usdt_label": "Адрес USDT",
            "payment_txid_note": "Отправьте платеж и скопируйте TXID/Примечание",
            "payment_usdt_network_note": "Только сеть TRC20!",
            "payment_unavailable": "Способ оплаты недоступен.",

            # D) STATISTICS SECTION
            "stats_header": "СТАТИСТИКА",
            "stats_user_label": "Пользователь",
            "stats_products_bought": "Куплено товаров",
            "stats_total_spent": "Всего потрачено",
            "stats_total_topup": "Всего пополнено",
            "stats_current_balance": "Текущий баланс",
            "stats_no_history": "У вас нет истории покупок или пополнений.",

            # E) LANGUAGE SELECTION
            "lang_selection_header": "🌐 Выберите предпочитаемый язык:",
            "lang_english_btn": "English (EN)",
            "lang_russian_btn": "Русский (RU)",
            "lang_arabic_btn": "العربية (AR)",
            "lang_changed_confirmation": "✅ Язык успешно изменен!",

            # F) INFORMATION SECTION
            "info_header": "💡 ИНФОРМАЦИЯ",
            "info_bot_description": "Мы предлагаем моментальную доставку цифровых кодов для различных игр и сервисов.",
            "info_how_it_works": "**Как работает бот:**\n1. Выберите продукт.\n2. Выберите способ доставки (Текст или Файл).\n3. Код доставляется мгновенно, если есть наличие/баланс.",
            "info_delivery_methods": "**Способы доставки:**\n- **Текст:** Код отправляется прямо в сообщении чата.\n- **Файл:** Код отправляется в виде загружаемого файла TXT.",
            "info_refund_policy": "**Политика возврата:**\nВсе продажи цифровых кодов являются окончательными. Возврат средств осуществляется только в том случае, если код доказан как недействительный *в момент доставки*.",
            "info_support_instructions": "По любым вопросам обращайтесь к менеджеру через кнопку КОНТАКТ.",

            # G) CONTACT SECTION
            "contact_header": "📞 КОНТАКТ",
            "contact_manager_msg": "Соединяю вас с менеджером. Пожалуйста, четко опишите свою проблему.",
            "contact_admin_unavailable_msg": "Администратор в настоящее время недоступен. Попробуйте позже или оставьте подробное сообщение.",

            # H) BUY FLOW
            "buy_product_selection_message": "Выберите товар, который хотите приобрести:",
            "purchase_confirmation_message": "🛒 Вы собираетесь приобрести **{product_name}** за **${price:.2f}**. Продолжить?",
            "choose_delivery": "📬 Выберите способ получения кода:",
            "delivery_text_btn": "Текстовая доставка",
            "delivery_file_btn": "Доставка файлом TXT",
            "code_sent_text": "✅ **Ваш код:**",
            "code_sent_file": "✅ **Файл с вашим кодом:**",
            "download_again_message": "Вы можете загрузить код еще раз из сообщения с чеком.",
            "receipt_header": "🧾 ЧЕК ПОКУПКИ",
            "receipt_order_id": "ID Заказа",
            "receipt_product": "Товар",
            "receipt_price": "Цена",
            "receipt_status": "Статус",
            "receipt_delivery_type": "Тип доставки",
            "receipt_status_completed": "ЗАВЕРШЕН",
            # File content
            "file_delivery_thank_you": "Спасибо за покупку!",
            "file_delivery_code_label": "Ваш Код:",

            # I) ADMIN MESSAGES
            "admin_new_order_notification": "🔔 Новый Заказ: Пользователь @{username} купил {product_name} за ${price:.2f}",
            "admin_new_topup_notification": "🔔 Новый Запрос на Пополнение от Пользователя @{username} | TXID/Примечание: {txid_note}",
            "admin_low_stock_warning": "⚠️ МАЛО ТОВАРА: У продукта '{product_name}' осталось всего {count} единиц.",
            "admin_out_of_stock_alert": "🚫 НЕТ В НАЛИЧИИ: Продукт '{product_name}' закончился.",
            "admin_user_banned_msg": "❌ Пользователь @{username} заблокирован.",
            "admin_user_unbanned_msg": "✅ Пользователь @{username} разблокирован.",
            "admin_log_topup_approved": "Одобрил Пополнение",
            "admin_log_topup_rejected": "Отклонил Пополнение",
            "admin_log_banned_user": "Заблокировал пользователя",

            # J) ERRORS & SYSTEM
            "error_database": "Произошла ошибка базы данных. Транзакция отменена.",
            "error_invalid_input": "Неверный ввод. Пожалуйста, проверьте ваше сообщение.",
            "error_action_not_allowed": "Действие не разрешено в данный момент.",
            "error_user_banned_notice": "Ваш аккаунт заблокирован. Свяжитесь со службой поддержки.",
        },
        "ar": {
            # Base Messages
            "welcome": "👋 مرحبًا بك في المتجر الرقمي!",
            "out_of_stock": "❌ هذا المنتج غير متوفر حاليًا.",
            "insufficient_balance": "❌ رصيد غير كاف. يرجى الشحن.",
            "generic_error": "حدث خطأ غير متوقع. يرجى المحاولة مرة أخرى لاحقًا.",
            "error_user_not_found": "لم يتم العثور على المستخدم.",

            # A) MAIN MENU BUTTONS
            "menu_stock": "🛒 أكواد UC المتوفرة",
            "menu_profile": "👤 الملف الشخصي",
            "menu_statistics": "📊 الإحصائيات",
            "menu_languages": "🌐 اللغات",
            "menu_information": "💡 معلومات",
            "menu_contact": "📞 اتصال",

            # B) STOCKABLE UC CODES
            "product_60_uc": "60 UC",
            "product_325_uc": "325 UC",
            "product_660_uc": "660 UC",
            "product_1800_uc": "1800 UC",
            "product_3850_uc": "3850 UC",
            "product_8100_uc": "8100 UC",

            # C) PROFILE SECTION
            "profile_header": "👤 ملفك الشخصي",
            "profile_username_label": "اسم المستخدم",
            "profile_userid_label": "معرف المستخدم (ID)",
            "profile_balance_label": "الرصيد (دولار أمريكي)",
            "profile_add_balance_btn": "💰 إضافة رصيد",
            "profile_topup_history_btn": "📜 سجل الشحن",
            "profile_no_topup_history": "لم يتم العثور على سجل شحن.",
            "payment_selection_message": "💳 يرجى اختيار طريقة الدفع المفضلة لديك:",
            "payment_binance_btn": "Binance Pay",
            "payment_bybit_btn": "Bybit Pay",
            "payment_usdt_btn": "USDT (TRC20)",
            "topup_instructions": "أرسل الدفع إلى العنوان أدناه، ثم انقر فوق 'تم الدفع'.",
            "payment_i_paid_btn": "✅ تم الدفع",
            "topup_submitted": "✅ تم إرسال الدفع للمراجعة! يرجى انتظار موافقة المسؤول.",
            "topup_pending": "⏳ عملية الشحن الخاصة بك قيد انتظار موافقة المسؤول.",
            "topup_approved": "✅ تمت الموافقة على شحنك بقيمة **${amount:.2f}** وتم إضافته!",
            "topup_rejected": "❌ تم رفض طلب الشحن الخاص بك. يرجى الاتصال بالدعم.",
            # Payment labels
            "payment_binance_label": "معرف Binance Pay",
            "payment_bybit_label": "معرف Bybit UID",
            "payment_usdt_label": "عنوان USDT",
            "payment_txid_note": "أرسل الدفع وانسخ مُعرف المعاملة (TXID)/الملاحظة",
            "payment_usdt_network_note": "شبكة TRC20 فقط!",
            "payment_unavailable": "طريقة الدفع غير متوفرة.",

            # D) STATISTICS SECTION
            "stats_header": "الإحصائيات",
            "stats_user_label": "المستخدم",
            "stats_products_bought": "المنتجات المشتراة",
            "stats_total_spent": "إجمالي المبلغ المنفق",
            "stats_total_topup": "إجمالي عمليات الشحن",
            "stats_current_balance": "الرصيد الحالي",
            "stats_no_history": "ليس لديك سجل مشتريات أو شحن.",

            # E) LANGUAGE SELECTION
            "lang_selection_header": "🌐 اختر لغتك المفضلة:",
            "lang_english_btn": "English (EN)",
            "lang_russian_btn": "Русский (RU)",
            "lang_arabic_btn": "العربية (AR)",
            "lang_changed_confirmation": "✅ تم تغيير اللغة بنجاح!",

            # F) INFORMATION SECTION
            "info_header": "💡 معلومات",
            "info_bot_description": "نقدم تسليمًا فوريًا للأكواد الرقمية لمختلف الألعاب والخدمات.",
            "info_how_it_works": "**كيف يعمل البوت:**\n1. اختر منتجًا.\n2. اختر طريقة التسليم (نص أو ملف).\n3. يتم تسليم الكود على الفور إذا كان الرصيد/المخزون متاحًا.",
            "info_delivery_methods": "**طرق التسليم:**\n- **نص:** يتم إرسال الكود مباشرة في رسالة الدردشة.\n- **ملف:** يتم إرسال الكود كملف TXT قابل للتحميل.",
            "info_refund_policy": "**سياسة الاسترداد:**\nجميع مبيعات الأكواد الرقمية نهائية. تتم معالجة عمليات الاسترداد فقط إذا ثبت أن الكود غير صالح *وقت التسليم*.",
            "info_support_instructions": "لأية مشكلات، يرجى الاتصال بالمسؤول عبر زر الاتصال.",

            # G) CONTACT SECTION
            "contact_header": "📞 اتصال",
            "contact_manager_msg": "جاري توصيلك بالمسؤول. يرجى وصف مشكلتك بوضوح.",
            "contact_admin_unavailable_msg": "المسؤول غير متصل حاليًا. يرجى المحاولة مرة أخرى لاحقًا أو ترك رسالة مفصلة.",

            # H) BUY FLOW
            "buy_product_selection_message": "اختر المنتج الذي ترغب في شرائه:",
            "purchase_confirmation_message": "🛒 أنت على وشك شراء **{product_name}** مقابل **${price:.2f}**. هل تريد المتابعة؟",
            "choose_delivery": "📬 اختر كيف تريد استلام الكود الخاص بك:",
            "delivery_text_btn": "تسليم نصي",
            "delivery_file_btn": "تسليم ملف TXT",
            "code_sent_text": "✅ **هذا هو الكود الخاص بك:**",
            "code_sent_file": "✅ **ملف الكود الخاص بك:**",
            "download_again_message": "يمكنك تنزيل الكود مرة أخرى من رسالة الإيصال.",
            "receipt_header": "🧾 إيصال الشراء",
            "receipt_order_id": "معرف الطلب",
            "receipt_product": "المنتج",
            "receipt_price": "السعر",
            "receipt_status": "الحالة",
            "receipt_delivery_type": "نوع التسليم",
            "receipt_status_completed": "مكتمل",
            # File content
            "file_delivery_thank_you": "شكرا لك على الشراء!",
            "file_delivery_code_label": "الكود الخاص بك:",

            # I) ADMIN MESSAGES
            "admin_new_order_notification": "🔔 طلب جديد: المستخدم @{username} اشترى {product_name} مقابل ${price:.2f}",
            "admin_new_topup_notification": "🔔 طلب شحن جديد من المستخدم @{username} | TXID/Note: {txid_note}",
            "admin_low_stock_warning": "⚠️ تنبيه انخفاض المخزون: المنتج '{product_name}' يحتوي على {count} عنصر فقط متبقي.",
            "admin_out_of_stock_alert": "🚫 نفاد المخزون: المنتج '{product_name}' فارغ الآن.",
            "admin_user_banned_msg": "❌ تم حظر المستخدم @{username}.",
            "admin_user_unbanned_msg": "✅ تم رفع الحظر عن المستخدم @{username}.",
            "admin_log_topup_approved": "وافق على الشحن",
            "admin_log_topup_rejected": "رفض الشحن",
            "admin_log_banned_user": "حظر المستخدم",

            # J) ERRORS & SYSTEM
            "error_database": "حدث خطأ في قاعدة البيانات. تم التراجع عن المعاملة.",
            "error_invalid_input": "إدخال غير صالح. يرجى التحقق من رسالتك.",
            "error_action_not_allowed": "الإجراء غير مسموح به في هذه اللحظة.",
            "error_user_banned_notice": "حسابك محظور حاليًا. يرجى الاتصال بالدعم.",
        }
    }

    @staticmethod
    def t(lang: str, key: str) -> str:
        """
        Translate a key to the target language.
        Falls back to English if the key is missing in the target language.
        """
        return LanguageService.STRINGS.get(lang, LanguageService.STRINGS["en"]).get(key, key)