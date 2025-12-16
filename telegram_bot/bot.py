# bot.py
"""
Production-grade Telegram Bot Entry Point.
Handles application startup, handler registration, and global error management.
"""

import logging
import asyncio
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

# Import strictly typed settings
from config import settings
from db import init_db
import asyncio
# Import route handlers
from routes import (
    start,
    handle_command,
    handle_callback
)

# Configure logging based on settings
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=settings.LOG_LEVEL
)
logger = logging.getLogger(__name__)

# ==============================================================================
# GLOBAL ERROR HANDLER
# ==============================================================================

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Log the error and notify the admin if configured.
    """
    logger.error(msg="Exception while handling an update:", exc_info=context.error)

    # Optional: Notify Admin (if ADMIN_CHAT_ID is set in config)
    if settings.ADMIN_CHAT_ID:
        try:
            # Short error summary
            error_msg = f"⚠ **Bot Error Occurred**\n\n`{context.error}`"
            await context.bot.send_message(
                chat_id=settings.ADMIN_CHAT_ID,
                text=error_msg,
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.error(f"Failed to send error notification to admin: {e}")

# ==============================================================================
# MAIN APPLICATION
# ==============================================================================

def main():
    """
    Initialize and run the bot application.
    """
    # 1. Initialize Database
    try:
        logger.info("Initializing database schema...")
        asyncio.run(init_db())
        logger.info("Database initialized successfully.")
    except Exception as e:
        logger.critical(f"Failed to initialize database: {e}")
        exit(1)

    # 2. Build Application
    try:
        application = (
            ApplicationBuilder()
            .token(settings.TELEGRAM_TOKEN.get_secret_value())
            .concurrent_updates(True) # Improve performance for multiple users
            .build()
        )
    except Exception as e:
        logger.critical(f"Failed to build application (Check Token): {e}")
        exit(1)

    # 3. Register Handlers
    # Main Entry
    application.add_handler(CommandHandler("start", start))

    # Shortcut Commands (Routed to handle_command dispatcher)
    application.add_handler(CommandHandler("buy", handle_command))
    application.add_handler(CommandHandler("balance", handle_command))
    application.add_handler(CommandHandler("support", handle_command))
    application.add_handler(CommandHandler("lang", handle_command))

    # Universal Callback Dispatcher (Buttons)
    application.add_handler(CallbackQueryHandler(handle_callback))

    # Error Handler
    application.add_error_handler(error_handler)

    # 4. Start Polling
    logger.info(f"Bot '{settings.APP_NAME}' is starting...")
    logger.info("Polling for updates...")
    
    # run_polling handles the event loop and graceful shutdown (SIGINT/SIGTERM) automatically
    application.run_polling()

if __name__ == "__main__":
    main()