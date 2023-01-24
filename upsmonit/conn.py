#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import asyncio
import collections
import copy
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
from ph4monitlib import jsonpath, defvalkey
from ph4monitlib.net import test_port_open, is_port_listening
from ph4monitlib.notif import NotifyEmail
from ph4monitlib.tbot import TelegramBot
from ph4monitlib.worker import Worker, AsyncWorker
from ph4runner import install_sarge_filter, try_fnc
from telegram import Update
from telegram.ext import ContextTypes, CommandHandler

logger = logging.getLogger(__name__)
coloredlogs.install(level=logging.INFO)


class ConnectionMonit:
    def __init__(self):
        self.args = {}
        self.config = {}

        self.watching_connections = []
        self.email_notif_recipients = []
        self.report_interval_fast = 20
        self.report_interval_slow = 5 * 60
        self.conn_timeout = 5.0

        self.is_running = True
        self.worker = Worker(running_fnc=lambda: self.is_running)
        self.asyncWorker = AsyncWorker(running_fnc=lambda: self.is_running)
        self.notifier_email = NotifyEmail()
        self.notifier_telegram = TelegramBot()

        self.status_thread = None
        self.status_thread_last_check = 0
        self.last_con_status = None
        self.last_con_status_time = 0
        self.start_error = None

        self.last_conn_report_txt = None
        self.last_conn_status_change = 0
        self.last_bat_report = 0
        self.last_norm_report = 0

        self.event_log_deque = collections.deque([], 5_000)
        self.log_report_len = 12
        self.do_email_reports = True

    def argparser(self):
        parser = argparse.ArgumentParser(description='Tunnel monitoring')

        parser.add_argument('--debug', dest='debug', action='store_const', const=True,
                            help='enables debug mode')
        parser.add_argument('-c', '--config', dest='config',
                            help='Config file to load')
        parser.add_argument('-u', '--users', dest='users', nargs=argparse.ZERO_OR_MORE,
                            help='Allowed user names')
        parser.add_argument('--user-ids', dest='user_ids', nargs=argparse.ZERO_OR_MORE, type=int,
                            help='Allowed user IDs')
        parser.add_argument('-t', '--chat-id', dest='chat_ids', nargs=argparse.ZERO_OR_MORE, type=int,
                            help='Pre-Registered chat IDs')
        return parser

    def load_config(self):
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
            self.watching_connections = jsonpath('$.watching_tunnels', self.config, True) or []

        except Exception as e:
            logger.error("Could not load config %s at %s" % (e, self.args.config), exc_info=e)

    def _stop_app_on_signal(self):
        logger.info(f'Signal received')
        self.is_running = False
        self.asyncWorker.stop()
        self.worker.stop()

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
            '/noemail',
            '/doemail',
        ]

        status_handler = CommandHandler('status', self.bot_cmd_status)
        full_status_handler = CommandHandler('full_status', self.bot_cmd_full_status)
        log_handler = CommandHandler('log', self.bot_cmd_log)
        noemail_handler = CommandHandler('noemail', self.bot_cmd_noemail)
        doemail_handler = CommandHandler('doemail', self.bot_cmd_doemail)
        self.notifier_telegram.add_handlers([
            status_handler, full_status_handler, log_handler, noemail_handler, doemail_handler
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

                    r = self.check_connections_state()
                    self.last_con_status = r
                    self.last_con_status_time = t
                    self.status_thread_last_check = t
                    self.last_con_status['meta.time_check'] = t
                    self.last_con_status['meta.dt_check'] = datetime.now().strftime("%m/%d/%Y, %H:%M:%S")
                    try_fnc(lambda: self.on_new_conn_state(self.last_con_status))

                except Exception as e:
                    logger.error(f'Status thread exception: {e}', exc_info=e)
                    time.sleep(0.5)
                finally:
                    time.sleep(0.1)
            logger.info(f'Stopping status thread')

        self.status_thread = threading.Thread(target=status_internal, args=())
        self.status_thread.daemon = False
        self.status_thread.start()

    def main(self):
        install_sarge_filter()
        logger.debug('App started')

        parser = self.argparser()
        self.args = parser.parse_args()
        if self.args.debug:
            coloredlogs.install(level=logging.DEBUG)

        self.notifier_telegram.allowed_usernames = self.args.users or []
        self.notifier_telegram.allowed_userids = self.args.user_ids or []
        self.notifier_telegram.registered_chat_ids = self.args.chat_ids or []

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
        try:
            self.init_signals()
            self.start_worker_thread()
            self.start_status_thread()
            await self.start_bot_async()

            if self.start_error:
                logger.error(f'Cannot continue, start error: {self.start_error}')
                raise self.start_error

            r = await self.main_handler()

        finally:
            await self.stop_bot()

        return r

    async def bot_cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        async with self.notifier_telegram.handler_helper("status", update, context) as hlp:
            if not hlp.auth_ok:
                return

            r = self.gen_report(self.last_con_status, extended=True)
            status_age = time.time() - self.last_con_status_time
            logger.info(f"Sending status response with age {status_age} s: \n{r}")
            await hlp.reply_msg(f"Status: {r}, {'%.2f' % status_age} s old")

    async def bot_cmd_full_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        async with self.notifier_telegram.handler_helper("full_status", update, context) as hlp:
            if not hlp.auth_ok:
                return

            r = self.last_con_status
            status_age = time.time() - self.last_con_status_time
            logger.info(f"Sending status response with age {status_age} s: {self.last_con_status}")
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

    async def main_handler(self):
        await self.asyncWorker.work()
        logger.info(f'Main thread finishing')

    async def send_telegram_notif(self, notif):
        await self.notifier_telegram.send_telegram_notif(notif)

    def send_telegram_notif_on_main(self, notif):
        coro = self.send_telegram_notif(notif)
        self.asyncWorker.enqueue(coro)

    def add_log(self, msg, mtype='-'):
        time_fmt = datetime.now().strftime("%m/%d/%Y, %H:%M:%S")
        time_now = time.time()

        self.event_log_deque.append({
            'time': time_now,
            'time_fmt': time_fmt,
            'mtype': mtype,
            'msg': msg
        })

    def on_new_conn_state(self, r):
        t = time.time()
        report = self.gen_report(r)
        report_extended = self.gen_report(r, extended=True)

        do_report = False
        in_state_report = False

        if self.last_conn_report_txt != report:
            old_state_change = self.last_conn_status_change
            logger.info(f'Detected conn change detected, last state change: {t - old_state_change}, report: \n{report_extended}')
            self.last_conn_report_txt = report
            self.last_conn_status_change = t
            do_report = True

            msg = f'Conn state report [age={"%.2f" % (t - self.last_bat_report)}]: \n{report_extended}'
            self.add_log(msg, mtype='conn-change')

        # if self.is_on_bat:
        #     t_diff = t - self.last_bat_report
        #     is_fast = self.last_bat_report == 0 or t_diff < 5 * 60
        #     in_state_report = (is_fast and t_diff >= self.report_interval_fast) or \
        #                       (not is_fast and t_diff >= self.report_interval_slow)

        if do_report or in_state_report:
            t_diff = t - self.last_conn_status_change
            txt_msg = f'Conn state report [age={"%.2f" % t_diff}]: \n{report_extended}'
            self.send_telegram_notif_on_main(txt_msg)
            if do_report and self.do_email_reports:
                self.notify_via_email_async(txt_msg, f'Conn change {datetime.now().strftime("%m/%d/%Y, %H:%M:%S")}')
            self.last_bat_report = t

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

    def check_connections_state(self):
        r = {
            'connections': copy.deepcopy(self.watching_connections),
            'check_pass': True
        }

        for idx, conn in enumerate(self.watching_connections):
            conn_type = conn['type']
            if conn_type != 'ssh':
                logger.warning(f'Unsupported conn type: {conn_type}')
                continue

            conn_obj = r['connections'][idx]
            conn_obj['check_res'] = {}
            check_res = conn_obj['check_res']

            host, port, name, app = conn['host'], conn['port'], conn['name'], conn['app']
            is_local = host in ['127.0.0.1', 'localhost', '::', '']

            try:
                if is_local:
                    listen_conn = is_port_listening(port)
                    check_res['local_listen'] = listen_conn is not None

                is_ssh = app == 'ssh'
                is_http = app in ['http', 'https']

                read_header = is_ssh
                write_payload = None
                if is_http:
                    write_payload = 'GET / HTTP/1.0\n\n'.encode()

                t = time.time()
                is_open = test_port_open(host, port, timeout=self.conn_timeout,
                                         read_header=read_header, write_payload=write_payload)
                check_res['open'] = is_open[0]
                check_res['attempts'] = is_open[1]
                check_res['check_time'] = time.time() - t
                open_data = is_open[2]

                check_res['app'] = None
                if not is_open:
                    check_res['app'] = None

                if is_open and is_ssh:
                    ssh_header = try_fnc(lambda: str(open_data.decode().strip())[:50])
                    check_res['app'] = ssh_header

                if is_open and is_http:
                    http_resp = try_fnc(lambda: str(open_data.decode().strip())[:50])
                    check_res['app'] = http_resp.startswith('HTTP')

                if not is_open[0]:
                    r['check_pass'] = False

            except Exception as e:
                logger.warning(f'Exception checking connection {conn}: {e}', exc_info=e)
                check_res['test_pass'] = False
                check_res['test_status'] = str(e)
                r['check_pass'] = False

        return r

    def gen_report(self, status, extended=False):
        conns = status['connections']
        acc = []
        for conn in conns:
            host, port, name, ctype, app = conn['host'], conn['port'], conn['name'], conn['type'], conn['app']
            check_res = conn['check_res']
            attempts = f' ({defvalkey(check_res, "attempts", "-")}x)' if extended else ''
            elapsed = f', {"%.2f" % defvalkey(check_res, "check_time", 0)}s' if extended else ''

            acc.append(f'{name} @ {host}:{port} - {app} over {ctype}, '
                       f'open: {defvalkey(check_res, "open", False)}{attempts}, '
                       f'app: {defvalkey(check_res, "app", "?")}{elapsed}')
        return "\n".join(acc)


def main():
    monit = ConnectionMonit()
    monit.main()


if __name__ == '__main__':
    main()
