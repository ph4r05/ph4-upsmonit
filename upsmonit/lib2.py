import socket
import time

import psutil


def silent_close(c):
    try:
        if c is not None:
            c.close()
    except:
        pass


def is_port_listening(port, tcp=True):
    """Returns a connection if the given port is listening, None otherwise"""
    conns = psutil.net_connections('tcp' if tcp else 'udp')
    for con in conns:
        if con.laddr[1] == port and (not tcp or (con.status is not None and con.status.upper() == 'LISTEN')):
            return con
    return None


def test_port_open(host='127.0.0.1', port=80, timeout=15, attempts=3, tcp=True, read_header=False, write_payload=None):
    idx = 0
    while idx < attempts:
        sock = None
        try:
            if tcp:
                sock = socket.create_connection((host, port), timeout=timeout)
            else:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.settimeout(timeout)

            read_data = None
            if read_header:
                read_data = sock.recv(512)
            elif write_payload:
                if tcp:
                    sock.sendall(write_payload)
                else:
                    sock.sendto(write_payload, (host, port))
                read_data = sock.recv(512)

            silent_close(sock)
            sock = None

            return True, idx, read_data

        except:
            time.sleep(0.1)
            pass

        finally:
            idx += 1
            silent_close(sock)

    return False, idx, None
