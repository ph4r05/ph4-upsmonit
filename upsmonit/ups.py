#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import asyncio
import collections
import itertools
import json
import logging
import os
import platform
import signal
import threading
import time
from datetime import datetime
from typing import List

import coloredlogs
import jwt
from ph4runner import install_sarge_filter
from telegram import Update
from telegram.ext import ContextTypes, CommandHandler

from upsmonit.lib import Worker, AsyncWorker, FiFoComm, TcpComm, NotifyEmail, jsonpath, try_fnc, get_runner
from upsmonit.tbot import TelegramBot

logger = logging.getLogger(__name__)
coloredlogs.install(level=logging.INFO)


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

        self.email_notif_recipients = []
        self.use_server = True
        self.use_fifo = False
        self.report_interval_fast = 20
        self.report_interval_slow = 5 * 60

        self.is_running = True
        self.worker = Worker(running_fnc=lambda: self.is_running)
        self.asyncWorker = AsyncWorker(running_fnc=lambda: self.is_running)
        self.fifo_comm = FiFoComm(handler=self.process_fifo_data, running_fnc=lambda: self.is_running)
        self.tcp_comm = TcpComm(handler=self.process_server_data, running_fnc=lambda: self.is_running)
        self.notifier_email = NotifyEmail()
        self.notifier_telegram = TelegramBot()

        self.status_thread = None
        self.status_thread_last_check = 0
        self.last_ups_status = None
        self.last_ups_status_time = 0

        self.main_loop = None
        self.start_error = None

        self.is_on_bat = False
        self.last_ups_state_txt = None
        self.last_ups_status_change = 0
        self.last_bat_report = 0
        self.last_norm_report = 0
        self.last_cmd_status = 0
        self.event_log_deque = collections.deque([], 5_000)
        self.log_report_len = 7

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
        parser.add_argument('message', nargs=argparse.ZERO_OR_MORE,
                            help='Text message from notifier')
        return parser

    def load_config(self):
        self.ups_name = os.getenv('UPS_NAME', None) or self.args.ups_name
        self.jwt_key = os.getenv('JWT_KEY', None)
        self.notifier_telegram.bot_apikey = os.getenv('BOT_APIKEY', None)

        if not self.args.config:
            return

        try:
            with open(self.args.config) as fh:
                dt = fh.read()
                self.config = json.loads(dt)

            bot_apikey = jsonpath('$.bot_apikey', self.config, True)
            if not self.notifier_telegram.bot_apikey:
                self.notifier_telegram.bot_apikey = bot_apikey

            jwt_key = jsonpath('$.jwt_key', self.config, True) or 'default-jwt-key-0x043719de'
            if not self.jwt_key:
                self.jwt_key = jwt_key

            ups_name = jsonpath('$.ups_name', self.config, True)
            if not self.ups_name:
                self.ups_name = ups_name

            server_port = jsonpath('$.server_port', self.config, True)
            if not self.tcp_comm.server_port:
                self.tcp_comm.server_port = server_port

            server_fifo = jsonpath('$.server_fifo', self.config, True)
            if not self.fifo_comm.fifo_path:
                self.fifo_comm.fifo_path = server_fifo

            allowed_usernames = jsonpath('$.allowed_usernames', self.config, True)
            if allowed_usernames:
                self.notifier_telegram.allowed_usernames += allowed_usernames

            allowed_userids = jsonpath('$.allowed_userids', self.config, True)
            if allowed_userids:
                self.notifier_telegram.allowed_userids += allowed_userids

            registered_chat_ids = jsonpath('$.registered_chat_ids', self.config, True)
            if registered_chat_ids:
                self.notifier_telegram.registered_chat_ids += registered_chat_ids

            email_notif_recipients = jsonpath('$.email_notif_recipients', self.config, True)
            if email_notif_recipients:
                self.email_notif_recipients += email_notif_recipients

            self.notifier_email.server = jsonpath('$.email_server', self.config, True)
            self.notifier_email.user = jsonpath('$.email_user', self.config, True)
            self.notifier_email.passwd = jsonpath('$.email_pass', self.config, True)

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
        self.notifier_telegram.init_bot()
        self.notifier_telegram.help_commands += [
            '/status - brief status',
            '/full_status - full status',
            '/log - log',
        ]

        status_handler = CommandHandler('status', self.bot_cmd_status)
        full_status_handler = CommandHandler('full_status', self.bot_cmd_full_status)
        log_handler = CommandHandler('log', self.bot_cmd_log)
        self.notifier_telegram.add_handlers([status_handler, full_status_handler, log_handler])

    async def start_bot_async(self):
        self.init_bot()
        await self.notifier_telegram.start_bot_async()

    async def stop_bot(self):
        await self.notifier_telegram.stop_bot_async()

    def start_worker_thread(self):
        self.worker.start_worker_thread()

    def start_status_thread(self):
        def status_internal():
            logger.info(f'Starting status thread')
            while self.is_running:
                try:
                    t = time.time()
                    if t - self.status_thread_last_check < 2.5:
                        continue

                    r = self.get_ups_state()
                    self.last_ups_status = r
                    self.last_ups_status_time = t
                    self.status_thread_last_check = t
                    self.last_ups_status['meta.time_check'] = t
                    self.last_ups_status['meta.dt_check'] = datetime.now().strftime("%m/%d/%Y, %H:%M:%S")
                    if 'battery.runtime' in r:
                        self.last_ups_status['meta.battery.runtime.m'] = round(100 * (r['battery.runtime'] / 60)) / 100
                    try_fnc(lambda: self.on_new_ups_state(self.last_ups_status))

                except Exception as e:
                    logger.error(f'Status thread exception: {e}', exc_info=e)
                    time.sleep(0.5)
                finally:
                    time.sleep(0.1)
            logger.info(f'Stopping status thread')

        self.status_thread = threading.Thread(target=status_internal, args=())
        self.status_thread.daemon = False
        self.status_thread.start()

    def start_fifo_comm(self):
        self.fifo_comm.start()

    def start_server(self):
        self.tcp_comm.start()

    def stop_server(self):
        self.tcp_comm.stop()

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
        self.notifier_telegram.allowed_usernames = self.args.users or []
        self.notifier_telegram.allowed_userids = self.args.user_ids or []
        self.notifier_telegram.registered_chat_ids = self.args.chat_ids or []
        self.fifo_comm.fifo_path = self.args.server_fifo
        self.tcp_comm.server_port = self.args.server_port
        self.main_loop = asyncio.get_event_loop()

        self.load_config()
        self.notifier_telegram.registered_chat_ids_set = set(self.notifier_telegram.registered_chat_ids)

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
            if not self.ups_name:
                raise Exception('UPS name to monitor is not defined')

            self.init_signals()
            self.start_worker_thread()
            self.start_status_thread()
            if self.use_fifo:
                self.start_fifo_comm()
            if self.use_server:
                self.start_server()
            await self.start_bot_async()

            if self.start_error:
                logger.error(f'Cannot continue, start error: {self.start_error}')
                raise self.start_error

            r = await self.main_handler()

        finally:
            if self.use_fifo:
                try_fnc(lambda: self.fifo_comm.stop())
            if self.use_server:
                try_fnc(lambda: self.stop_server())
            await self.stop_bot()

        return r

    async def bot_cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        async with self.notifier_telegram.handler_helper("status", update, context) as hlp:
            if not hlp.auth_ok:
                return

            self.last_cmd_status = time.time()
            r = self.shorten_status(self.last_ups_status)
            status_age = time.time() - self.last_ups_status_time
            logger.info(f"Sending status response with age {status_age} s: {r}")
            await hlp.reply_msg(f"Status: {json.dumps(r, indent=2)}, {'%.2f' % status_age} s old")

    async def bot_cmd_full_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        async with self.notifier_telegram.handler_helper("full_status", update, context) as hlp:
            if not hlp.auth_ok:
                return

            self.last_cmd_status = time.time()
            r = self.last_ups_status
            status_age = time.time() - self.last_ups_status_time
            logger.info(f"Sending status response with age {status_age} s: {self.last_ups_status}")
            await hlp.reply_msg(f"Status: {json.dumps(r, indent=2)}, {'%.2f' % status_age} s old")

    async def bot_cmd_log(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        def _txt_log(r):
            if isinstance(r['msg'], str):
                rr = dict(r)
                del rr['msg']
                return f'{json.dumps(rr, indent=2)}, msg: {r["msg"]}'
            return json.dumps(r, indent=2)

        async with self.notifier_telegram.handler_helper("log", update, context) as hlp:
            if not hlp.auth_ok:
                return

            last_log = list(reversed(list(itertools.islice(reversed(self.event_log_deque), self.log_report_len))))
            last_log_txt = [f' - {_txt_log(x)}' % x for x in last_log]
            last_log_txt = "\n".join(last_log_txt)
            log_msg = f'Last {self.log_report_len} log reports: \n{last_log_txt}'
            await hlp.reply_msg(log_msg)

    async def event_handler(self):
        notif_type = os.getenv('NOTIFYTYPE')
        logger.info(f'Event OS: {os.environ}, notif type: {notif_type}')
        payload = {'type': 'event', 'notif': notif_type, 'msg': self.args.message}

        self.send_daemon_message(payload)

    async def main_handler(self):
        await self.asyncWorker.work()
        logger.info(f'Main thread finishing')

    async def send_telegram_notif(self, notif):
        await self.notifier_telegram.send_telegram_notif(notif)

    def send_telegram_notif_on_main(self, notif):
        coro = self.send_telegram_notif(notif)
        self.asyncWorker.enqueue(coro)

    def send_daemon_message(self, payload):
        if self.use_server:
            self.send_server_msg(payload)
        elif self.use_fifo:
            self.send_fifo_msg(payload)
        else:
            raise Exception('No connection method to the daemon')

    def send_fifo_msg(self, payload):
        return self.fifo_comm.send_message(self.create_jwt(payload))

    def send_server_msg(self, payload):
        return self.tcp_comm.send_message(self.create_jwt(payload))

    def process_fifo_data(self, data):
        logger.debug(f'Data read from fifo: {data}, len: {len(data)}')
        self.process_client_message(data)

    def process_server_data(self, data):
        logger.debug(f'TCP server data received: {data}')
        self.process_client_message(data)
        return json.dumps({'response': 200}).encode()

    def process_client_message(self, data):
        lines = data.splitlines(False)
        for line in lines:
            try:
                js = self.decode_jwt(line)
                if 'type' not in js:
                    continue

                js_type = js['type']
                if js_type == 'event':
                    self.on_ups_event(js)

            except Exception as e:
                logger.warning(f'Exception in processing server fifo: {e}', exc_info=e)

    def add_log(self, msg, mtype='-'):
        time_fmt = datetime.now().strftime("%m/%d/%Y, %H:%M:%S")
        time_now = time.time()

        self.event_log_deque.append({
            'time': time_now,
            'time_fmt': time_fmt,
            'mtype': mtype,
            'msg': msg
        })

    def on_ups_event(self, js):
        self.status_thread_last_check = 0

        notif = js['notif']
        msg = js['msg'] if 'msg' in js else '-'
        if isinstance(msg, list):
            msg = try_fnc(lambda: ' '.join(msg))

        status = json.dumps(self.shorten_status(self.last_ups_status) or {}, indent=2)
        msg = f'UPS event: {notif}, message: {msg}, status: {status}'
        logger.info(msg)

        self.add_log(msg, mtype='ups-event')
        self.send_telegram_notif_on_main(msg)
        self.notify_via_email_async(msg, f'UPS event {datetime.now().strftime("%m/%d/%Y, %H:%M:%S")}')

    def on_new_ups_state(self, r):
        """
        https://github.com/networkupstools/nut/blob/03c3bbe8df9a2caf3c09c120ae7045d35af99b76/drivers/apcupsd-ups.h
        """

        t = time.time()
        ups_state = r['ups.status']

        state_comp = ups_state.split(' ', 2)
        is_online = state_comp[0] == 'OL'
        is_charging = ups_state == 'OL CHRG'
        is_on_bat = not is_online and not is_charging  # simplification

        do_report = False
        in_state_report = False
        if self.last_ups_state_txt != ups_state:
            old_state_change = self.last_ups_status_change
            logger.info(f'Detected UPS change detected {ups_state}, last state change: {t - old_state_change}')
            self.last_ups_state_txt = ups_state
            self.last_ups_status_change = t
            self.is_on_bat = is_on_bat
            do_report = True

            status = json.dumps(self.shorten_status(self.last_ups_status) or {}, indent=2)
            msg = f'UPS state report [{ups_state}, age={"%.2f" % (t - self.last_bat_report)}]: {status}'
            self.add_log(msg, mtype='ups-change')

        if self.is_on_bat:
            t_diff = t - self.last_bat_report
            is_fast = self.last_bat_report == 0 or t_diff < 5 * 60
            in_state_report = (is_fast and t_diff >= self.report_interval_fast) or \
                              (not is_fast and t_diff >= self.report_interval_slow)

        if do_report or in_state_report:
            t_diff = t - self.last_ups_status_change
            status = json.dumps(self.shorten_status(self.last_ups_status) or {}, indent=2)
            txt_msg = f'UPS state report [{ups_state}, age={"%.2f" % t_diff}]: {status}'
            self.send_telegram_notif_on_main(txt_msg)
            if do_report:
                self.notify_via_email_async(txt_msg, f'UPS state change {datetime.now().strftime("%m/%d/%Y, %H:%M:%S")}')
            self.last_bat_report = t

    def get_ups_state(self):
        runner = get_runner([f'/usr/bin/upsc', self.ups_name], shell=False)
        runner.start(wait_running=True, timeout=3.0)
        runner.wait(timeout=3.0)

        out = "\n".join(runner.out_acc)
        ret = parse_ups(out)
        return ret

    def notify_via_email_async(self, txt_message: str, subject: str):
        self.worker.enqueue(lambda: self.notify_via_email(txt_message, subject))

    def notify_via_email(self, txt_message: str, subject: str):
        if not self.email_notif_recipients:
            return
        return self.send_notify_email(self.email_notif_recipients, txt_message, subject)

    def send_notify_email(self, recipients: List[str], txt_message: str, subject: str):
        self.notifier_email.send_notify_email(recipients, txt_message, subject)

    def _get_tuple(self, key, dct):
        return key, dct[key]

    def shorten_status(self, status):
        if not status:
            return status
        try:
            r = collections.OrderedDict([
                self._get_tuple('battery.charge', status),
                self._get_tuple('battery.runtime', status),
                self._get_tuple('battery.voltage', status),
                self._get_tuple('input.voltage', status),
                self._get_tuple('output.voltage', status),
                self._get_tuple('ups.load', status),
                self._get_tuple('ups.status', status),
                self._get_tuple('ups.test.result', status),
                self._get_tuple('meta.battery.runtime.m', status),
                self._get_tuple('meta.time_check', status),
                self._get_tuple('meta.dt_check', status),
            ])
            return r
        except Exception as e:
            logger.warning(f'Exception shortening the status {e}', exc_info=e)
            return status


def main():
    monit = UpsMonit()
    monit.main()


if __name__ == '__main__':
    main()
