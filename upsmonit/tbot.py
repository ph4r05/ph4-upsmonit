#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import asyncio
import logging
import threading

from telegram import Update, User
from telegram.error import TelegramError
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler

logger = logging.getLogger(__name__)


class TelegramBot:
    def __init__(self, api_key=None):
        self.bot_app = None
        self.bot_thread = None

        self.bot_apikey = api_key
        self.allowed_usernames = []
        self.allowed_userids = []
        self.registered_chat_ids = []
        self.registered_chat_ids_set = set()
        self.start_error = None
        self.start_finished = False
        self.help_commands = [
            '/start - register',
            '/stop - deregister',
        ]

    def init_bot(self):
        self.bot_app = ApplicationBuilder().token(self.bot_apikey).build()
        help_handler = CommandHandler('help', self.bot_cmd_help)
        start_handler = CommandHandler('start', self.bot_cmd_start)
        stop_handler = CommandHandler('stop', self.bot_cmd_stop)

        self.bot_app.add_handler(help_handler)
        self.bot_app.add_handler(start_handler)
        self.bot_app.add_handler(stop_handler)

    def add_handler(self, handler):
        self.bot_app.add_handler(handler)

    def add_handlers(self, handlers):
        self.bot_app.add_handlers(handlers)

    def load_bot_thread(self):
        """Running bot in a separate thread. Experimental method.
        Message handling does not work"""
        if not self.bot_apikey:
            logger.info('Telegram bot API key not configured')
            return

        self.init_bot()

        def looper(cloop):
            logger.debug('Starting looper for loop %s' % (cloop,))
            asyncio.set_event_loop(cloop)
            cloop.run_forever()

        worker_loop = asyncio.new_event_loop()
        worker_thread = threading.Thread(
            target=looper, args=(worker_loop,)
        )
        worker_thread.daemon = True
        worker_thread.start()

        logger.info(f'Starting bot thread')

        # async def main_coro():
        #     logger.info('Main bot coroutine started')
        #     await self.bot_app.updater.start_polling()
        #     logger.info('Main bot coroutine finished')

        # r = asyncio.run_coroutine_threadsafe(main_coro(), worker_loop)
        # logger.info(f'Bot coroutine submitted {r}')
        loop = asyncio.new_event_loop()

        def error_callback(exc: TelegramError) -> None:
            logger.info(f'Error callback {exc}')
            self.bot_app.create_task(self.bot_app.process_error(error=exc, update=None))

        # This method does not support message handling for some reason
        def bot_internal():
            logger.info(f'Starting bot thread')
            asyncio.set_event_loop(loop)

            loop.run_until_complete(self.bot_app.initialize())
            if self.bot_app.post_init:
                loop.run_until_complete(self.bot_app.post_init(self.bot_app))
            loop.run_until_complete(
                self.bot_app.updater.start_polling(error_callback=error_callback)
            )  # one of updater.start_webhook/polling

            logger.info('Bot app start')
            loop.run_until_complete(self.bot_app.start())
            logger.info('Bot running forever')
            loop.run_forever()
            logger.info(f'Stopping bot thread')

        self.bot_thread = threading.Thread(target=bot_internal, args=())
        self.bot_thread.daemon = False
        self.bot_thread.start()

        if False:
            self.bot_app.run_polling()

    async def start_bot_async(self):
        if not self.bot_apikey:
            logger.warning('Telegram bot API key not configured')
            return

        def error_callback(exc: TelegramError) -> None:
            logger.info(f'Error callback {exc}')
            self.bot_app.create_task(self.bot_app.process_error(error=exc, update=None))

        try:
            if not self.bot_app:
                self.init_bot()
            await self.bot_app.initialize()
            if self.bot_app.post_init:
                await self.bot_app.post_init(self.bot_app)
            await self.bot_app.updater.start_polling(error_callback=error_callback)

            logger.info('Bot app start')
            await self.bot_app.start()
            logger.info('Bot started')
            self.start_finished = True

        except Exception as e:
            logger.error(f'Error starting telegram bot {e}', exc_info=e)
            self.start_error = e
            raise

    async def stop_bot_async(self):
        if not self.bot_app:
            return

        # We arrive here either by catching the exceptions above or if the loop gets stopped
        logger.info(f'Stopping telegram bot')
        try:
            # Mypy doesn't know that we already check if updater is None
            if self.bot_app.updater.running:  # type: ignore[union-attr]
                await self.bot_app.updater.stop()  # type: ignore[union-attr]
            if self.bot_app.running:
                await self.bot_app.stop()
            await self.bot_app.shutdown()
            if self.bot_app.post_shutdown:
                await self.bot_app.post_shutdown(self.bot_app)

        except Exception as e:
            logger.warning(f'Exception in closing the bot {e}', exc_info=e)

    def is_user_allowed(self, user: User):
        if not user:
            return False

        if user.id in self.allowed_userids:
            return True

        if user.username in self.allowed_usernames:
            return True
        return False

    async def reject_user(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await context.bot.send_message(chat_id=update.effective_chat.id, text=f"Fuck off")

    async def check_user(self, method, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_allowed = self.is_user_allowed(update.message.from_user)
        logger.info(f'New "{method}" message with chat_id: {update.effective_chat.id}, from {update.message.from_user}'
                    f', allowed {user_allowed}')
        if not user_allowed:
            await self.reject_user(update, context)
            return False
        return True

    async def bot_cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self.check_user("start", update, context):
            return

        help_txt = "Help: \n" + "\n".join(self.help_commands)
        await context.bot.send_message(chat_id=update.effective_chat.id, text=help_txt)

    async def bot_cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self.check_user("start", update, context):
            return

        await context.bot.send_message(chat_id=update.effective_chat.id, text="Registered")
        self.registered_chat_ids_set.add(update.effective_chat.id)

    async def bot_cmd_stop(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self.check_user("stop", update, context):
            return

        await context.bot.send_message(chat_id=update.effective_chat.id, text="Deregistering you")
        self.registered_chat_ids_set.remove(update.effective_chat.id)

    async def reply_msg(self, text, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await context.bot.send_message(chat_id=update.effective_chat.id, text=text)

    async def send_telegram_notif(self, notif):
        for chat_id in self.registered_chat_ids_set:
            logger.info(f'Sending telegram notif {notif}, chat id: {chat_id}')
            await self.bot_app.bot.send_message(chat_id, notif)

    def handler_helper(self, mtype, update: Update, context: ContextTypes.DEFAULT_TYPE) -> "CmdHelper":
        return CmdHelper(mtype, update, context, self)


class CmdHelper:
    def __init__(self, mtype, update: Update, context: ContextTypes.DEFAULT_TYPE, tbot: TelegramBot):
        self.mtype = mtype
        self.update = update
        self.context = context
        self.tbot = tbot
        self.auth_ok = False

    async def __aenter__(self) -> "CmdHelper":
        self.auth_ok = await self.tbot.check_user(self.mtype, self.update, self.context)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass

    async def reply_msg(self, text):
        await self.tbot.reply_msg(text, self.update, self.context)
