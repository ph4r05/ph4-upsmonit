#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import asyncio
import json
import logging
import os
import queue
import select
import smtplib
import socket
import socketserver
import ssl
import threading
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from queue import Queue
from typing import List

import coloredlogs
from jsonpath_ng import parse
from ph4runner import AsyncRunner

logger = logging.getLogger(__name__)


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


class AsyncWorker:
    def __init__(self, running_fnc=None):
        self.task_queue = Queue()
        self.is_running = True
        self.is_running_fnc = running_fnc

    def _check_is_running(self):
        if self.is_running_fnc:
            if not self.is_running_fnc():
                return False
        return self.is_running

    async def work(self):
        while self._check_is_running():
            try:
                next_task = self.task_queue.get(False)
                if not next_task:
                    await asyncio.sleep(0.01)
                await next_task
            except queue.Empty:
                await asyncio.sleep(0.02)

    def enqueue(self, task):
        """Enqueues coroutine function for execution"""
        self.task_queue.put(task)


class Worker:
    def __init__(self, running_fnc=None):
        self.worker_thread = None
        self.worker_queue = Queue()
        self.is_running = True
        self.is_running_fnc = running_fnc

    def _check_is_running(self):
        if self.is_running_fnc:
            if not self.is_running_fnc():
                return False
        return self.is_running

    def start_worker_thread(self):
        def worker_internal():
            logger.info(f'Starting worker thread')
            while self._check_is_running():
                try:
                    next_task = self.worker_queue.get(False)
                    if not next_task:
                        time.sleep(0.02)
                    try:
                        next_task()
                    except Exception as e:
                        logger.error(f'Top-level error at worker thread {e}', exc_info=e)
                except queue.Empty:
                    time.sleep(0.02)
            logger.info(f'Stopping worker thread')

        self.worker_thread = threading.Thread(target=worker_internal, args=())
        self.worker_thread.daemon = False
        self.worker_thread.start()

    def enqueue(self, task):
        """Enqueues lambda function for execution"""
        self.worker_queue.put(task)


class FiFoComm:
    def __init__(self, fifo_path=None, handler=None, running_fnc=None):
        self.is_running = True
        self.is_running_fnc = running_fnc
        self.fifo_path = fifo_path
        self.fifo_thread = None
        self.handler = handler
        self.start_error = None
        self.start_finished = False

    def _check_is_running(self):
        if self.is_running_fnc:
            if not self.is_running_fnc():
                return False
        return self.is_running

    def create_fifo(self):
        if not self.fifo_path:
            return
        os.mkfifo(self.fifo_path)

    def destroy_fifo(self):
        if not self.fifo_path:
            return
        try:
            if os.path.exists(self.fifo_path):
                os.unlink(self.fifo_path)
        except Exception as e:
            logger.warning(f'Error unlinking fifo {e}', exc_info=e)

    def start(self):
        self.start_finished = False
        if not self.handler:
            logger.warning(f'FiFo comm has no handler set')

        def fifo_internal():
            logger.info('Starting fifo thread')
            try:
                self.destroy_fifo()
                self.create_fifo()
                with open(self.fifo_path) as _:
                    pass

            except Exception as e:
                logger.error(f'Error starting server fifo: {e}', exc_info=e)
                self.start_error = e
                return
            finally:
                self.start_finished = True

            with open(self.fifo_path) as fifo:
                while self._check_is_running():
                    try:
                        select.select([fifo], [], [fifo])
                        data = fifo.read()
                        if not data:
                            continue

                        if self.handler:
                            self.handler(data)
                        else:
                            logger.warning(f'FiFo comm has no handler set')

                    except Exception as e:
                        logger.error(f'Fifo thread exception: {e}', exc_info=e)
                        time.sleep(0.1)
            logger.info('Stopping fifo thread')

        self.fifo_thread = threading.Thread(target=fifo_internal, args=())
        self.fifo_thread.daemon = False
        self.fifo_thread.start()

        ttime = time.time()
        while not self.start_finished:
            if self.start_error:
                raise self.start_error
            if time.time() - ttime > 30.0:
                raise ValueError('Init error: timeout')
            time.sleep(0.01)

    def stop(self):
        self.is_running = False
        try_fnc(lambda: self.destroy_fifo())

    def send_message(self, payload):
        """Send message to the fifo, as a client"""
        with open(self.fifo_path, 'w') as f:
            f.write(payload + '\n')
            f.flush()


class TcpComm:
    def __init__(self, host='127.0.0.1', port=9333, handler=None, running_fnc=None):
        self.is_running = True
        self.is_running_fnc = running_fnc

        self.server_host = host
        self.server_port = port
        self.handler = handler

        self.server_thread = None
        self.server_tcp = None
        self.start_error = None
        self.start_finished = False

    def _check_is_running(self):
        if self.is_running_fnc:
            if not self.is_running_fnc():
                return False
        return self.is_running

    def start(self):
        if not self.handler:
            logger.warning(f'TcpComm has no handler set')

        sself = self
        self.start_finished = False

        class TcpServerHandler(socketserver.BaseRequestHandler):
            def handle(self):
                try:
                    data = self.request.recv(8192).strip()
                    if sself.handler:
                        r = sself.handler(data.decode())
                        self.request.sendall(r)
                    else:
                        logger.warning(f'TcpComm has no handler set')

                except Exception as e:
                    logger.warning(f'Exception processing server message {e}', exc_info=e)
                    self.request.sendall(json.dumps({'response': 500}).encode())

        def server_internal():
            logger.info('Starting server thread')
            try:
                self.server_tcp = socketserver.TCPServer((self.server_host, self.server_port), TcpServerHandler)
                self.server_tcp.allow_reuse_address = True

                self.start_finished = True
                self.server_tcp.serve_forever()
            except Exception as e:
                self.start_error = e
                logger.error(f'Error in starting server thread {e}', exc_info=e)
            finally:
                logger.info('Stopping server thread')

        self.server_thread = threading.Thread(target=server_internal, args=())
        self.server_thread.daemon = False
        self.server_thread.start()

        time.sleep(0.5)
        ttime = time.time()
        while not self.start_finished:
            if self.start_error:
                raise self.start_error
            if time.time() - ttime > 30.0:
                raise ValueError('Init error: timeout')
            time.sleep(0.01)

    def stop(self):
        if not self.server_tcp:
            return
        try:
            self.server_tcp.shutdown()
        except Exception as e:
            logger.error(f'Error in TCP server shutdown {e}', exc_info=e)

    def send_message(self, payload):
        """Send a message to the TCP server, as a client"""
        tcp_client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            tcp_client.connect((self.server_host, self.server_port))
            tcp_client.sendall((payload + '\n').encode())

            # Read data from the TCP server and close the connection
            received = tcp_client.recv(8192).decode()
            return received
        finally:
            tcp_client.close()


class NotifyEmail:
    def __init__(self, server=None, user=None, passwd=None, port=587, timeout=20.0):
        self.server = server
        self.user = user
        self.passwd = passwd
        self.port = port
        self.timeout = timeout

    def send_notify_email(self, recipients: List[str], txt_message: str, subject: str):
        if not self.server or not self.user or not self.passwd:
            return

        server = None
        context = ssl.create_default_context()

        try:
            logger.info(f'Sending email notification via {self.user}, msg: {txt_message[:80]}...')
            server = smtplib.SMTP(self.server, self.port, timeout=self.timeout)
            server.ehlo()
            server.starttls(context=context)
            server.ehlo()
            server.login(self.user, self.passwd)

            for recipient in recipients:
                message = MIMEMultipart("alternative")
                message["Subject"] = subject
                message["From"] = self.user
                message["To"] = recipient
                part1 = MIMEText(txt_message, "plain")
                message.attach(part1)
                server.sendmail(self.user, recipient, message.as_string())
            return True

        except Exception as e:
            logger.warning(f'Exception when sending email {e}', exc_info=e)
            return e

        finally:
            try_fnc(lambda: server.quit())
