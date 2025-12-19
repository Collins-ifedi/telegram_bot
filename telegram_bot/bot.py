# bot.py
"""
Production-grade Telegram Bot Entry Point.
Handles application startup, handler registration, and global error management.
Updated for async runtime compatibility (v20+).
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

async def main():
    """
    Initialize and run the bot application using explicit async lifecycle.
    """
    # 1. Initialize Database
    try:
        logger.info("Initializing database schema...")
        await init_db() # Awaited directly in async main
        logger.info("Database initialized successfully.")
    except Exception as e:
        logger.critical(f"Failed to initialize database: {e}")
        return

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
        return

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

    # 4. Start Polling (Explicit Async Lifecycle)
    logger.info(f"Bot '{settings.APP_NAME}' is starting...")
    
    try:
        # Initialize and start the application
        # This prevents "ExtBot is not properly initialized" errors
        await application.initialize()
        await application.start()
        
        # Start the updater to fetch updates
        await application.updater.start_polling()
        
        logger.info("Polling for updates (Async Loop)...")
        
        # Keep the process alive indefinitely
        # This replaces the blocking run_polling() which causes loop conflicts
        stop_signal = asyncio.Event()
        await stop_signal.wait()

    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopping request received...")
    finally:
        # 5. Graceful Shutdown
        if application.updater.running:
            await application.updater.stop()
        if application.running:
            await application.stop()
        await application.shutdown()
        logger.info("Bot stopped successfully.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass