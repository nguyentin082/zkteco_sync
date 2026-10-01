"""No SDK session is left open on the terminal, even when connecting fails.

A ZKTeco terminal serves one SDK session at a time. pyzk's connect() sends
CMD_CONNECT (the terminal allots a session id right then) and, when the
handshake then fails, raises without sending CMD_EXIT or closing its socket.
The terminal held that half-open session until it timed it out, refusing the
next connect meanwhile — typically the retry after fixing a wrong comm key.

Runs against a fake terminal on a loopback UDP socket — no device is dialled.

    python -m unittest tests.test_device_hangup -v
"""

import os
import socket
import struct
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("APP_ENV", "development")
os.environ.setdefault("SECRET_KEY", "x" * 48)

from zk import ZK, const                                   # noqa: E402
from zk.exception import ZKErrorResponse, ZKNetworkError  # noqa: E402

from app.errors import AppError                            # noqa: E402
from app.services import poller, sdk                       # noqa: E402

SESSION_ID = 4242


class FakeTerminal:
    """Answers pyzk over UDP and records every command it receives.

    `replies` maps a command to the response code sent back; a command not in
    it gets no answer at all.
    """

    def __init__(self, replies):
        self.replies = replies
        self.received = []  # (command, session_id)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(0.2)
        self.port = self.sock.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while not self._stop.is_set():
            try:
                data, addr = self.sock.recvfrom(1024)
            except socket.timeout:
                continue
            except OSError:
                return
            command, _, session_id, reply_id = struct.unpack("<4H", data[:8])
            self.received.append((command, session_id))
            code = self.replies.get(command)
            if code is not None:
                self.sock.sendto(struct.pack("<4H", code, 0, SESSION_ID, reply_id), addr)

    def commands(self):
        return [c for c, _ in self.received]

    def close(self):
        self._stop.set()
        self._thread.join()
        self.sock.close()


def _zk(terminal, timeout=1):
    return ZK("127.0.0.1", port=terminal.port, timeout=timeout, password=1,
              force_udp=True, ommit_ping=True)


def _socket_closed(zk):
    return zk._ZK__sock.fileno() == -1


class DialTest(unittest.TestCase):
    def _terminal(self, replies):
        terminal = FakeTerminal(replies)
        self.addCleanup(terminal.close)
        return terminal

    def test_a_refused_comm_key_hangs_up_the_session_it_was_given(self):
        terminal = self._terminal({
            const.CMD_CONNECT: const.CMD_ACK_UNAUTH,
            const.CMD_AUTH: const.CMD_ACK_UNAUTH,
            const.CMD_EXIT: const.CMD_ACK_OK,
        })
        zk = _zk(terminal)

        with self.assertRaisesRegex(ZKErrorResponse, "Unauthenticated"):
            poller.dial(zk)

        self.assertEqual(terminal.commands(),
                         [const.CMD_CONNECT, const.CMD_AUTH, const.CMD_EXIT])
        self.assertEqual(terminal.received[-1][1], SESSION_ID)
        self.assertTrue(_socket_closed(zk))

    def test_a_silent_terminal_still_gets_its_socket_closed(self):
        terminal = self._terminal({})
        zk = _zk(terminal)

        with self.assertRaises(ZKNetworkError):
            poller.dial(zk)

        # Nobody answered, so there is no session to say CMD_EXIT to.
        self.assertEqual(terminal.commands(), [const.CMD_CONNECT])
        self.assertTrue(_socket_closed(zk))

    def test_a_good_handshake_is_returned_connected(self):
        terminal = self._terminal({const.CMD_CONNECT: const.CMD_ACK_OK})
        zk = _zk(terminal)

        conn = poller.dial(zk)

        self.assertTrue(conn.is_connect)
        self.assertEqual(terminal.commands(), [const.CMD_CONNECT])
        self.assertFalse(_socket_closed(zk))
        zk._ZK__sock.close()


class HangUpTest(unittest.TestCase):
    def test_an_unanswered_exit_still_closes_the_socket(self):
        terminal = FakeTerminal({const.CMD_CONNECT: const.CMD_ACK_OK})
        self.addCleanup(terminal.close)
        conn = poller.dial(_zk(terminal))

        poller.hang_up(conn)  # pyzk's disconnect() raises here; hang_up must not

        self.assertEqual(terminal.commands(), [const.CMD_CONNECT, const.CMD_EXIT])
        self.assertTrue(_socket_closed(conn))

    def test_a_fake_conn_without_a_pyzk_socket_is_fine(self):
        conn = mock.Mock()
        poller.hang_up(conn)
        conn.disconnect.assert_called_once_with()


class CallersTest(unittest.TestCase):
    """The two _connect wrappers and the enrolment task go through dial()."""

    def setUp(self):
        self.terminal = FakeTerminal({
            const.CMD_CONNECT: const.CMD_ACK_UNAUTH,
            const.CMD_AUTH: const.CMD_ACK_UNAUTH,
            const.CMD_EXIT: const.CMD_ACK_OK,
        })
        self.addCleanup(self.terminal.close)
        self.device = SimpleNamespace(
            serial_number="ATT0000000001", ip_address="127.0.0.1",
            port=self.terminal.port, comm_key=1, force_udp=1,
        )
        # The wrappers ping first; loopback is reachable but `ping` may not be installed.
        patcher = mock.patch("zk.base.ZK_helper.test_ping", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_poller_connect_hangs_up_on_a_wrong_key(self):
        with self.assertRaisesRegex(ZKErrorResponse, "comm key"):
            poller._connect(self.device)
        self.assertEqual(self.terminal.commands()[-1], const.CMD_EXIT)

    def test_device_connection_hangs_up_and_frees_the_terminal(self):
        with self.assertRaises(AppError) as caught:
            with sdk.device_connection(self.device):
                self.fail("connected with a refused key")
        self.assertEqual(caught.exception.status_code, 403)
        self.assertEqual(self.terminal.commands()[-1], const.CMD_EXIT)
        self.assertFalse(poller.is_pulling(self.device.serial_number))

    def test_enroll_task_hangs_up_on_a_wrong_key(self):
        de = SimpleNamespace(uid=7)
        db = mock.Mock()
        db.query.return_value.filter_by.return_value.first.side_effect = [self.device, de]
        with mock.patch.object(sdk, "SessionLocal", return_value=db):
            sdk.enroll_user_task(self.device.serial_number, "7", 0)
        self.assertEqual(self.terminal.commands()[-1], const.CMD_EXIT)
        self.assertFalse(poller.is_pulling(self.device.serial_number))


if __name__ == "__main__":
    unittest.main()
