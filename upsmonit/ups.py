#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import asyncio
import json
import logging
import os
import platform
import queue
import select
import signal
import socket
import socketserver
import threading
import time
from queue import Queue

import coloredlogs
import jwt
from jsonpath_ng import parse
from ph4runner import AsyncRunner, install_sarge_filter
from telegram import Update, User
from telegram.error import TelegramError
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler

logger = logging.getLogger(__name__)
coloredlogs.install(level=logging.INFO)


def try_fnc(fnc):
    try:
        return fnc()
    except:
        pass


def jsonpath(path, obj, allow_none=False):
    r = [m.value for m in parse(path).find(obj)]
    return r[0] if not allow_none else (r[0] if r else None)


def listize(obj):
    return obj if (obj is None or isinstance(obj, list)) else [obj]


def get_runner(cli, args=None, cwd=None, shell=False, env=None):
    async_runner = AsyncRunner(cli, args=args, cwd=cwd, shell=shell, env=env)
    async_runner.log_out_after = False
    async_runner.log_out_during = False
    async_runner.preexec_setgrp = True
    return async_runner


def parse_ups(log: str):
    float_fields = [
        'battery.charge',
        'battery.charge.low',
        'battery.charge.warning',
        'battery.runtime',
        'battery.runtime.low',
        'battery.voltage',
        'battery.voltage.nominal',
        'driver.parameter.pollfreq',
        'driver.parameter.pollinterval',
        'input.transfer.high',
        'input.transfer.low',
        'input.voltage',
        'input.voltage.nominal',
        'output.voltage',
        'ups.delay.shutdown',
        'ups.delay.start',
        'ups.load',
        'ups.realpower.nominal',
        'ups.timer.shutdown',
        'ups.timer.start',
    ]

    ret = {}
    set_float_fields = set(float_fields)
    lines = log.split('\n')
    for ix, line in enumerate(lines):
        p1, p2 = [x.strip() for x in line.split(':', 1)]
        val = p2

        if p1 in set_float_fields:
            try:
                val = float(p2)
            except Exception as e:
                logger.warning(f'Float conversion failed for {p1}', exc_info=e)

        ret[p1] = val
    return ret


class UpsMonit:
    def __init__(self):
        self.args = {}
        self.ups_name = 'servers'
        self.config = {}
        self.jwt_key = None
        self.bot_apikey = None
        self.server_fifo = None
        self.server_port = None
        self.server_host = '127.0.0.1'
        self.allowed_usernames = []
        self.allowed_userids = []
        self.registered_chat_ids = []
        self.registered_chat_ids_set = set()
        self.use_server = True
        self.use_fifo = False

        self.task_queue = Queue()
        self.is_running = True
        self.status_thread = None
        self.status_thread_last_check = 0
        self.last_ups_status = None
        self.last_ups_status_time = 0
        self.fifo_thread = None
        self.server_thread = None
        self.server_tcp = None
        self.main_loop = None
        self.start_error = None

        self.bot_app = None
        self.bot_thread = None

        self.is_on_bat = False
        self.last_bat_report = 0
        self.last_norm_report = 0
        self.last_cmd_status = 0

    def argparser(self):
        parser = argparse.ArgumentParser(description='UPS monitoring')

        parser.add_argument('--debug', dest='debug', action='store_const', const=True,
                            help='enables debug mode')
        parser.add_argument('-c', '--config', dest='config',
                            help='Config file to load')
        parser.add_argument('-n', '--name', dest='ups_name', default='servers',
                            help='UPS name to check')
        parser.add_argument('-e', '--event', dest='event', action='store_const', const=True,
                            help='Event from the nut daemon')
        parser.add_argument('-u', '--users', dest='users', nargs=argparse.ZERO_OR_MORE,
                            help='Allowed user names')
        parser.add_argument('--user-ids', dest='user_ids', nargs=argparse.ZERO_OR_MORE, type=int,
                            help='Allowed user IDs')
        parser.add_argument('-t', '--chat-id', dest='chat_ids', nargs=argparse.ZERO_OR_MORE, type=int,
                            help='Pre-Registered chat IDs')
        parser.add_argument('-p', '--port', dest='server_port', type=int, default='9139',
                            help='UDP server port')
        parser.add_argument('-f', '--fifo', dest='server_fifo', default='/tmp/ups-monitor-fifo',
                            help='Server fifo')
        return parser

    def load_config(self):
        self.bot_apikey = os.getenv('BOT_APIKEY', None)
        self.jwt_key = os.getenv('JWT_KEY', None)
        self.ups_name = os.getenv('UPS_NAME', None) or self.args.ups_name

        if not self.args.config:
            return

        try:
            with open(self.args.config) as fh:
                dt = fh.read()
                self.config = json.loads(dt)

            bot_apikey = jsonpath('$.bot_apikey', self.config, True)
            if not self.bot_apikey:
                self.bot_apikey = bot_apikey

            jwt_key = jsonpath('$.jwt_key', self.config, True) or 'default-jwt-key-0x043719de'
            if not self.jwt_key:
                self.jwt_key = jwt_key

            ups_name = jsonpath('$.ups_name', self.config, True)
            if not self.ups_name:
                self.ups_name = ups_name

            server_port = jsonpath('$.server_port', self.config, True)
            if not self.server_port:
                self.server_port = server_port

            server_fifo = jsonpath('$.server_fifo', self.config, True)
            if not self.server_fifo:
                self.server_fifo = server_fifo

            allowed_usernames = jsonpath('$.allowed_usernames', self.config, True)
            if allowed_usernames:
                self.allowed_usernames += allowed_usernames

            allowed_userids = jsonpath('$.allowed_userids', self.config, True)
            if allowed_userids:
                self.allowed_userids += allowed_userids

            registered_chat_ids = jsonpath('$.registered_chat_ids', self.config, True)
            if registered_chat_ids:
                self.registered_chat_ids += registered_chat_ids

        except Exception as e:
            logger.error("Could not load config %s at %s" % (e, self.args.config), exc_info=e)

    def _stop_app_on_signal(self):
        logger.info(f'Signal received')
        self.is_running = False

    def init_signals(self):
        stop_signals = (signal.SIGINT, signal.SIGTERM, signal.SIGABRT) if platform.system() != "Windows" else []
        loop = asyncio.get_event_loop()
        for sig in stop_signals or []:
            loop.add_signal_handler(sig, self._stop_app_on_signal)

    def init_bot(self):
        self.bot_app = ApplicationBuilder().token(self.bot_apikey).build()
        start_handler = CommandHandler('start', self.bot_cmd_start)
        stop_handler = CommandHandler('stop', self.bot_cmd_stop)
        status_handler = CommandHandler('status', self.bot_cmd_status)
        self.bot_app.add_handler(start_handler)
        self.bot_app.add_handler(stop_handler)
        self.bot_app.add_handler(stop_handler)
        self.bot_app.add_handler(status_handler)

    def load_bot_thread(self):
        """Running bot in a separate thread. Experimental method.
        Message handling does not work"""
        if not self.bot_apikey:
            logger.info('Telegram bot API key not configured')
            return

        self.init_bot()

        def looper(loop):
            logger.debug('Starting looper for loop %s' % (loop,))
            asyncio.set_event_loop(loop)
            loop.run_forever()

        worker_loop = asyncio.new_event_loop()
        worker_thread = threading.Thread(
            target=looper, args=(worker_loop,)
        )
        worker_thread.daemon = True
        worker_thread.start()

        logger.info(f'Starting bot thread')

        async def main_coro():
            logger.info('Main bot coroutine started')
            await self.bot_app.updater.start_polling()
            logger.info('Main bot coroutine finished')

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

    async def load_bot_async(self):
        if not self.bot_apikey:
            logger.info('Telegram bot API key not configured')
            return

        def error_callback(exc: TelegramError) -> None:
            logger.info(f'Error callback {exc}')
            self.bot_app.create_task(self.bot_app.process_error(error=exc, update=None))

        try:
            self.init_bot()
            await self.bot_app.initialize()
            if self.bot_app.post_init:
                await self.bot_app.post_init(self.bot_app)
            await self.bot_app.updater.start_polling(error_callback=error_callback)

            logger.info('Bot app start')
            await self.bot_app.start()
            logger.info('Bot started')

        except Exception as e:
            logger.error(f'Error starting telegram bot {e}', exc_info=e)
            self.start_error = e
            raise

    async def stop_bot(self):
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

    def start_status_thread(self):
        def status_internal():
            logger.info(f'Starting status thread')
            while self.is_running:
                try:
                    t = time.time()
                    if t - self.status_thread_last_check < 2000.5:
                        continue
                    r = self.get_ups_state()
                    self.last_ups_status = r
                    self.last_ups_status_time = t
                    self.status_thread_last_check = t

                except Exception as e:
                    logger.error(f'Status thread exception: {e}', exc_info=e)
                    time.sleep(0.5)
                finally:
                    time.sleep(0.1)
            logger.info(f'Stopping status thread')

        self.status_thread = threading.Thread(target=status_internal, args=())
        self.status_thread.daemon = False
        self.status_thread.start()

    def start_fifo_thread(self):
        main_loop = asyncio.get_running_loop()

        def fifo_internal():
            logger.info('Starting fifo thread')
            try:
                self.destroy_fifo()
                self.create_fifo()
                with open(self.server_fifo) as fifo:
                    pass
            except Exception as e:
                logger.error(f'Error starting server fifo: {e}', exc_info=e)
                self.start_error = e
                return

            with open(self.server_fifo) as fifo:
                while self.is_running:
                    try:
                        select.select([fifo], [], [fifo])
                        data = fifo.read()
                        if not data:
                            continue

                        self.process_fifo_data(data)
                        # main_loop.call_soon(self.process_fifo_data, data)
                        # main_loop.run_until_complete(self.process_fifo_data(data))

                    except Exception as e:
                        logger.error(f'Fifo thread exception: {e}', exc_info=e)
                        time.sleep(0.1)
            logger.info('Stopping fifo thread')

        self.fifo_thread = threading.Thread(target=fifo_internal, args=())
        self.fifo_thread.daemon = False
        self.fifo_thread.start()

    def start_server(self):
        monit = self

        class TcpServerHandler(socketserver.BaseRequestHandler):
            def handle(self):
                try:
                    data = self.request.recv(8192).strip()
                    r = monit.process_server_data(data.decode())
                    self.request.sendall(r)
                except Exception as e:
                    logger.warning(f'Exception processing server message {e}', exc_info=e)
                    self.request.sendall(json.dumps({'response': 500}).encode())

        def server_internal():
            logger.info('Starting server thread')
            try:
                self.server_tcp = socketserver.TCPServer((self.server_host, self.server_port), TcpServerHandler)
                self.server_tcp.allow_reuse_address = True
                self.server_tcp.serve_forever()
            except Exception as e:
                self.start_error = e
                logger.error(f'Error in starting server thread {e}', exc_info=e)
            finally:
                logger.info('Stopping server thread')

        self.server_thread = threading.Thread(target=server_internal, args=())
        self.server_thread.daemon = False
        self.server_thread.start()

    def stop_server(self):
        if self.server_tcp:
            self.server_tcp.shutdown()

    def create_fifo(self):
        if not self.server_fifo:
            return
        os.mkfifo(self.server_fifo)

    def destroy_fifo(self):
        if not self.server_fifo:
            return
        try:
            if os.path.exists(self.server_fifo):
                os.unlink(self.server_fifo)
        except Exception as e:
            logger.warning(f'Error unlinking fifo {e}', exc_info=e)

    def create_jwt(self, payload):
        return jwt.encode(payload=payload, key=self.jwt_key, algorithm="HS256")

    def decode_jwt(self, payload):
        return jwt.decode(payload, self.jwt_key, algorithms=["HS256"])

    def main(self):
        install_sarge_filter()
        logger.debug('App started')

        parser = self.argparser()
        self.args = parser.parse_args()
        if self.args.debug:
            coloredlogs.install(level=logging.DEBUG)

        self.ups_name = self.args.ups_name
        self.allowed_usernames = self.args.users or []
        self.allowed_userids = self.args.user_ids or []
        self.registered_chat_ids = self.args.chat_ids or []
        self.server_fifo = self.args.server_fifo
        self.server_port = self.args.server_port
        self.main_loop = asyncio.get_event_loop()

        self.load_config()
        self.registered_chat_ids_set = set(self.registered_chat_ids)

        # Async switch
        try:
            loop = asyncio.get_running_loop()
        except Exception as e:
            loop = asyncio.new_event_loop()

        loop.set_debug(True)
        loop.run_until_complete(self.main_async())
        self.is_running = False

    async def main_async(self):
        logger.info('Async main started')

        # UPS generated event, should send notification anyway
        if self.args.event:
            return await self.event_handler()

        # Normal daemon mode
        try:
            self.init_signals()
            self.start_status_thread()
            if self.use_fifo:
                self.start_fifo_thread()
            if self.use_server:
                self.start_server()
            await self.load_bot_async()

            if self.start_error:
                logger.error(f'Cannot continue, start error: {self.start_error}')
                raise self.start_error

            r = await self.main_handler()

        finally:
            if self.use_fifo:
                try_fnc(lambda: self.destroy_fifo())
            if self.use_server:
                try_fnc(lambda: self.stop_server())
            await self.stop_bot()

        return r

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
        logger.info(f'New "{method}" message with chat_id: {update.effective_chat.id}, from {update.message.from_user}, '
                    f'allowed {user_allowed}')
        if not user_allowed:
            await context.bot.send_message(chat_id=update.effective_chat.id, text=f"Fuck off")
            return False
        return True

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

    async def bot_cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self.check_user("status", update, context):
            return

        if time.time() - self.last_cmd_status < 3:
            await context.bot.send_message(chat_id=update.effective_chat.id,
                                           text=f"Status too often {time.time() - self.last_cmd_status} s")
            return

        self.last_cmd_status = time.time()
        r = self.last_ups_status
        status_age = time.time() - self.last_ups_status_time
        logger.info(f"Sending status response with age {status_age} s: {self.last_ups_status}")
        await context.bot.send_message(chat_id=update.effective_chat.id,
                                       text=f"Status: {json.dumps(r, indent=2)}, {status_age} s old")

    async def event_handler(self):
        notif_type = os.getenv('NOTIFYTYPE')
        logger.info(f'Event OS: {os.environ}, notif type: {notif_type}')
        payload = {'type': 'event', 'notif': notif_type}

        self.send_daemon_message(payload)

    async def main_handler(self):
        while self.is_running:
            try:
                next_task = self.task_queue.get(False)
                if not next_task:
                    await asyncio.sleep(0.01)
                await next_task
            except queue.Empty:
                await asyncio.sleep(0.02)
        logger.info(f'Main thread finishing')

    async def send_telegram_notif(self, notif):
        for chat_id in self.registered_chat_ids_set:
            logger.info(f'Sending telegram notif {notif}, chat id: {chat_id}')
            await self.bot_app.bot.send_message(chat_id, notif)

    def send_daemon_message(self, payload):
        if self.use_server:
            self.send_server_msg(payload)
        elif self.use_fifo:
            self.send_fifo_msg(payload)
        else:
            raise Exception('No connection method to the daemon')

    def send_fifo_msg(self, payload):
        with open(self.server_fifo, 'w') as f:
            token = self.create_jwt(payload)
            f.write(token + '\n')
            f.flush()

    def send_server_msg(self, payload):
        tcp_client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            data = self.create_jwt(payload)
            tcp_client.connect((self.server_host, self.server_port))
            tcp_client.sendall((data + '\n').encode())

            # Read data from the TCP server and close the connection
            received = tcp_client.recv(8192).decode()
            return received
        finally:
            tcp_client.close()

    def process_fifo_data(self, data):
        logger.info(f'Data read from fifo: {data}, len: {len(data)}')
        self.process_message(data)

    def process_server_data(self, data):
        logger.info(f'TCP server data received: {data}')
        self.process_message(data)
        return json.dumps({'response': 200}).encode()

    def process_message(self, data):
        lines = data.splitlines(False)
        for line in lines:
            try:
                js = self.decode_jwt(line)
                if 'type' not in js:
                    continue

                js_type = js['type']
                if js_type == 'event':
                    notif = js['notif']

                    self.task_queue.put(self.send_telegram_notif(f'UPS event: {notif}'))

            except Exception as e:
                logger.warning(f'Exception in processing server fifo: {e}', exc_info=e)

    def get_ups_state(self):
        runner = get_runner([f'/usr/bin/upsc', self.ups_name], shell=False)
        runner.start(wait_running=True, timeout=3.0)
        runner.wait(timeout=3.0)

        out = "\n".join(runner.out_acc)
        ret = parse_ups(out)
        return ret


if __name__ == '__main__':
    monit = UpsMonit()
    monit.main()
