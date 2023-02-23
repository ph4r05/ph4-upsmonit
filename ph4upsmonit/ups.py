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
import re
import signal
import threading
import time
from datetime import datetime
from typing import List

import coloredlogs
import jwt
from ph4monitlib import jsonpath, try_fnc, get_runner, defvalkey
from ph4monitlib.comm import FiFoComm, TcpComm
from ph4monitlib.notif import NotifyEmail
from ph4monitlib.tbot import TelegramBot
from ph4monitlib.worker import Worker, AsyncWorker
from ph4runner import install_sarge_filter
from telegram import Update
from telegram.ext import ContextTypes, CommandHandler

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


class TelegramNotifMsg:
    def __init__(self, msg, mtype, t):
        self.msg = msg
        self.mtype = mtype
        self.time = t

    def __repr__(self) -> str:
        return f'TelegramNotifMsg({self.msg}, {self.mtype}, {self.time})'


class UpsState:
    def __init__(self):
        self.last_ups_status = None
        self.last_ups_status_time = 0
        self.is_on_bat = False
        self.last_ups_state_txt = None
        self.last_ups_status_change = 0
        self.last_bat_report = 0
        self.last_norm_report = 0


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
        self.notif_telegram_last_messages = {}
        self.notif_telegram_update_messages = collections.defaultdict(lambda: dict())

        self.status_thread = None
        self.status_thread_last_check = 0
        self.main_loop = None
        self.start_error = None

        self.do_email_reports = True
        self.notif_telegram_edit_time = 5 * 60
        self.last_cmd_status = 0

        self.event_log_deque = collections.deque([], 5_000)
        self.log_report_len = 7

        self.ups_statuses = collections.defaultdict(lambda: UpsState())

    def argparser(self):
        parser = argparse.ArgumentParser(description='UPS monitoring')

        parser.add_argument('--debug', dest='debug', action='store_const', const=True,
                            help='enables debug mode')
        parser.add_argument('-c', '--config', dest='config',
                            help='Config file to load')
        parser.add_argument('-n', '--name', dest='ups_name',
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
        parser.add_argument('-l', '--log', dest='json_log',
                            help='File where to store JSON logs from events')
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
        self.asyncWorker.stop()
        self.worker.stop()

    def get_ups_state(self, ups_name) -> UpsState:
        return self.ups_statuses[ups_name]

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
            '/log - log of latest events',
            '/noemail - disable email reporting',
            '/doemail - enable email reporting',
            '/doedit <time> - edit last status message instead of sending a new one. Time to edit the old message in '
            'seconds. ',
        ]

        status_handler = CommandHandler('status', self.bot_cmd_status)
        full_status_handler = CommandHandler('full_status', self.bot_cmd_full_status)
        log_handler = CommandHandler('log', self.bot_cmd_log)
        noemail_handler = CommandHandler('noemail', self.bot_cmd_noemail)
        doemail_handler = CommandHandler('doemail', self.bot_cmd_doemail)
        doedit_handler = CommandHandler('doedit', self.bot_cmd_doedit)
        self.notifier_telegram.add_handlers([
            status_handler, full_status_handler, log_handler, noemail_handler, doemail_handler, doedit_handler,
        ])

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

                    for ups_name in self.get_ups_names():
                        r = self.fetch_ups_state(ups_name)
                        st = self.get_ups_state(ups_name)

                        st.last_ups_status = r
                        st.last_ups_status_time = t
                        st.last_ups_status['meta.time_check'] = t
                        st.last_ups_status['meta.dt_check'] = datetime.now().strftime("%m/%d/%Y, %H:%M:%S")
                        st.last_ups_status['meta.ups_name'] = ups_name
                        if 'battery.runtime' in r:
                            st.last_ups_status['meta.battery.runtime.m'] = round(100 * (r['battery.runtime'] / 60)) / 100
                        try_fnc(lambda: self.on_new_ups_state(ups_name, st.last_ups_status))

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

    def start_fifo_comm(self):
        self.fifo_comm.start()

    def start_server(self):
        self.tcp_comm.start()
        logger.info(f'TCP server started @ {self.tcp_comm.server_host}:{self.tcp_comm.server_port}')

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

        self.load_config()
        self.notifier_telegram.registered_chat_ids_set = set(self.notifier_telegram.registered_chat_ids)

        # Async switch
        try:
            self.main_loop = asyncio.get_event_loop()
        except Exception as e:
            self.main_loop = asyncio.new_event_loop()
            logger.info(f'Created new runloop {self.main_loop}')

        self.main_loop.set_debug(True)
        self.main_loop.run_until_complete(self.main_async())
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
                await self.main_loop.run_in_executor(None, self.start_fifo_comm)
            if self.use_server:
                await self.main_loop.run_in_executor(None, self.start_server)
            await self.start_bot_async()

            if self.start_error:
                logger.error(f'Cannot continue, start error: {self.start_error}')
                raise self.start_error

            r = await self.main_handler()

        finally:
            if self.use_fifo:
                await self.main_loop.run_in_executor(None, try_fnc, lambda: self.fifo_comm.stop())
            if self.use_server:
                await self.main_loop.run_in_executor(None, try_fnc, lambda: self.stop_server())
            await self.stop_bot()

        return r

    async def bot_cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        async with self.notifier_telegram.handler_helper("status", update, context) as hlp:
            if not hlp.auth_ok:
                return

            self.last_cmd_status = time.time()
            acc = []
            for ups in self.get_ups_names():
                st = self.get_ups_state(ups)
                r = self.shorten_status(st.last_ups_status)
                r['meta.status_age'] = time.time() - st.last_ups_status_time
                acc.append(r)

            logger.info(f"Sending status s: {acc}")
            await hlp.reply_msg(f"Status: {json.dumps(acc, indent=2)}")

    async def bot_cmd_full_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        async with self.notifier_telegram.handler_helper("full_status", update, context) as hlp:
            if not hlp.auth_ok:
                return

            self.last_cmd_status = time.time()
            acc = []
            for ups in self.get_ups_names():
                st = self.get_ups_state(ups)
                r = dict(st.last_ups_status or {})
                r['meta.status_age'] = time.time() - st.last_ups_status_time
                acc.append(r)

            logger.info(f"Sending full status response: {acc}")
            await hlp.reply_msg(f"Status: {json.dumps(acc, indent=2)}")

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

    async def bot_cmd_noemail(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        async with self.notifier_telegram.handler_helper("noemail", update, context) as hlp:
            if not hlp.auth_ok:
                return

            self.do_email_reports = False
            await hlp.reply_msg(f"OK")

    async def bot_cmd_doemail(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        async with self.notifier_telegram.handler_helper("doemail", update, context) as hlp:
            if not hlp.auth_ok:
                return

            self.do_email_reports = True
            await hlp.reply_msg(f"OK")

    async def bot_cmd_doedit(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        async with self.notifier_telegram.handler_helper("doedit", update, context) as hlp:
            if not hlp.auth_ok:
                return

            try:
                cmd_rest = update.message.text.split(' ', 1)
                self.notif_telegram_edit_time = float(cmd_rest[1])
                await hlp.reply_msg(f"OK: {self.notif_telegram_edit_time}")

            except Exception as e:
                await hlp.reply_msg(f"Fail: {e}")

    async def event_handler(self):
        notif_type = os.getenv('NOTIFYTYPE')
        logger.info(f'Event OS: {os.environ}, notif type: {notif_type}')
        payload = {'type': 'event', 'notif': notif_type, 'msg': self.args.message}

        self.send_daemon_message(payload)

    async def main_handler(self):
        await self.asyncWorker.work()
        logger.info(f'Main thread finishing')

    async def send_telegram_notif_with_edit(self, notif, state_key):
        to_edit = {}
        t = time.time()

        for chat_id in (self.notif_telegram_update_messages[state_key] if self.notif_telegram_edit_time > 0 else {}):
            last_state_msg = self.notif_telegram_update_messages[state_key][chat_id]
            last_msg = defvalkey(self.notif_telegram_last_messages, chat_id)
            if not last_msg or not last_state_msg:
                continue
            # if last_msg.msg.message_id != last_state_msg.msg.message_id:
            #     continue
            if t - last_state_msg.time >= self.notif_telegram_edit_time:
                continue

            to_edit[chat_id] = last_state_msg.msg

        msgs = await self.notifier_telegram.send_telegram_notif(notif, to_edit)

        self.update_last_telegram_messages(msgs, state_key)
        for chat_id in msgs:
            self.notif_telegram_update_messages[state_key][chat_id] = TelegramNotifMsg(msgs[chat_id], state_key, t)

        logger.info(f'Messages: {msgs}')

    async def send_telegram_notif(self, notif):
        msgs = await self.notifier_telegram.send_telegram_notif(notif)
        self.update_last_telegram_messages(msgs)

    def update_last_telegram_messages(self, msgs, state_key=None):
        for chat_id in msgs:
            self.notif_telegram_last_messages[chat_id] = TelegramNotifMsg(msgs[chat_id], state_key, time.time())

    def send_telegram_notif_on_main(self, notif):
        # asyncio.run_coroutine_threadsafe(self.send_telegram_notif(notif), self.main_loop)
        self.asyncWorker.enqueue_on_main(self.send_telegram_notif(notif), self.main_loop)

    def send_telegram_notif_with_edit_on_main(self, *args):
        self.asyncWorker.enqueue_on_main(self.send_telegram_notif_with_edit(*args), self.main_loop)

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
                logger.warning(f'Exception in processing server message: {e}', exc_info=e)

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

        self.wait_next_status_check()
        ups_name = self._extract_ups_name_from_msg(msg)
        st = self.get_ups_state(ups_name)

        status = json.dumps(self.shorten_status(st.last_ups_status) or {}, indent=2)
        msg = f'UPS event: {ups_name}: {notif}, message: {msg}, status: {status}'
        logger.info(msg)

        self.add_log(msg, mtype='ups-event')
        self.add_log_rec({
            'evt': 'ups-event',
            'ups_name': ups_name,
            'notif': notif,
            'status': st.last_ups_status,
            'last_bat_report': st.last_bat_report,
        })

        # self.send_telegram_notif_on_main(msg)  # TODO: revert
        self.send_telegram_notif_with_edit_on_main(msg, notif)
        self.notify_via_email_async(msg, f'UPS event {ups_name} - {datetime.now().strftime("%m/%d/%Y, %H:%M:%S")}')

    def on_new_ups_state(self, ups_name, r):
        """
        https://github.com/networkupstools/nut/blob/03c3bbe8df9a2caf3c09c120ae7045d35af99b76/drivers/apcupsd-ups.h
        """

        t = time.time()
        st = self.get_ups_state(ups_name)
        ups_state = r['ups.status']

        state_comp = ups_state.split(' ', 2)
        is_online = state_comp[0] == 'OL'
        is_charging = ups_state == 'OL CHRG'
        is_on_bat = not is_online and not is_charging  # simplification
        state_key = f'{ups_name}:{ups_state}'

        do_report = False
        in_state_report = False
        if st.last_ups_state_txt != ups_state:
            old_state_change = st.last_ups_status_change
            logger.info(f'Detected UPS change detected {ups_name}: {ups_state}, last state change: {t - old_state_change}')
            st.last_ups_state_txt = ups_state
            st.last_ups_status_change = t
            st.is_on_bat = is_on_bat
            do_report = True

            status = json.dumps(self.shorten_status(st.last_ups_status) or {}, indent=2)
            msg = f'UPS state report for {ups_name} [{ups_state}, age={"%.2f" % (t - st.last_bat_report)}]: {status}'
            self.add_log(msg, mtype='ups-change')
            self.add_log_rec({
                'evt': 'ups-state',
                'ups_name': ups_name,
                'ups_state': ups_state,
                'status': st.last_ups_status,
                'last_bat_report': st.last_bat_report,
            })

        if st.is_on_bat:
            t_diff = t - st.last_bat_report
            is_fast = st.last_bat_report == 0 or t_diff < 5 * 60
            in_state_report = (is_fast and t_diff >= self.report_interval_fast) or \
                              (not is_fast and t_diff >= self.report_interval_slow)

        if do_report or in_state_report:
            t_diff = t - st.last_ups_status_change
            status = json.dumps(self.shorten_status(st.last_ups_status) or {}, indent=2)
            txt_msg = f'UPS state report for {ups_name} [{ups_state}, age={"%.2f" % t_diff}]: {status}'
            self.send_telegram_notif_with_edit_on_main(txt_msg, state_key)
            if do_report:
                self.notify_via_email_async(txt_msg, f'UPS state change {ups_name} {datetime.now().strftime("%m/%d/%Y, %H:%M:%S")}')
            st.last_bat_report = t

    def plan_status_check(self):
        self.status_thread_last_check = 0

    def wait_next_status_check(self, timeout=10):
        self.plan_status_check()
        tstart = time.time()
        while self.status_thread_last_check == 0 and (timeout is None or time.time() - tstart < timeout):
            time.sleep(0.1)
        return self.status_thread_last_check > 0

    def fetch_ups_state(self, name=None):
        runner = get_runner([f'/usr/bin/upsc', name], shell=False)
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
        if not self.do_email_reports:
            return
        return self.send_notify_email(self.email_notif_recipients, txt_message, subject)

    def send_notify_email(self, recipients: List[str], txt_message: str, subject: str):
        self.notifier_email.send_notify_email(recipients, txt_message, subject)

    def add_log_rec(self, rec):
        time_fmt = datetime.now().strftime("%m/%d/%Y, %H:%M:%S")
        time_now = time.time()
        return self.add_log_line(json.dumps({
            'time': time_now,
            'time_fmt': time_fmt,
            **rec,
        }))

    def add_log_line(self, line):
        if not self.args.json_log:
            return

        try:
            with open(self.args.json_log, 'a+') as fh:
                fh.write(line)
                fh.write("\n")
        except Exception as e:
            logger.warning(f'Error writing log to the file {e}', exc_info=e)

    def _get_tuple(self, key, dct, default=None):
        return key, dct[key] if key in dct else default

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
                self._get_tuple('meta.ups_name', status),
                self._get_tuple('meta.status_age', status),
            ])
            return r
        except Exception as e:
            logger.warning(f'Exception shortening the status {e}', exc_info=e)
            return status

    def get_ups_names(self):
        return [self.ups_name] if isinstance(self.ups_name, str) else self.ups_name

    def _match_ups_name(self, candidate):
        names = self.get_ups_names()
        for cname in names:
            if cname in (candidate, f'{candidate}@localhost') or candidate in (cname, f'{cname}@localhost'):
                return cname
        return candidate

    def _extract_ups_name_from_msg(self, msg):
        m = re.match(r'.*UPS\s(.+?)\s.*', msg)
        if m:
            return self._match_ups_name(m.group(1))
        return None


def main():
    monit = UpsMonit()
    monit.main()


if __name__ == '__main__':
    main()
