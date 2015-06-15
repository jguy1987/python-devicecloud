# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
#
# Copyright (c) 2015 Digi International, Inc. All rights reserved.
#
# This code is originally from another Digi Open Source Library:
# https://github.com/digidotcom/idigi-python-monitor-api

import json
import logging
import socket
import struct
from threading import Thread
import errno
import select
import zlib
import ssl

import pkg_resources
from six.moves.queue import Queue, Empty
import six

DEFAULT_CRT_NAME = "devicecloud.crt"

# Push Opcodes.
CONNECTION_REQUEST = 0x01
CONNECTION_RESPONSE = 0x02
PUBLISH_MESSAGE = 0x03
PUBLISH_MESSAGE_RECEIVED = 0x04

# Data has not been completely read.
INCOMPLETE = -1
# No Data Received on Socket.
NO_DATA = -2

# Possible Responses from iDigi with respect to Push.
STATUS_OK = 200
STATUS_UNAUTHORIZED = 403
STATUS_BAD_REQUEST = 400

# Ports to Connect on for Push.
PUSH_OPEN_PORT = 3200
PUSH_SECURE_PORT = 3201


def _read_msg_header(session):
    """
    Perform a read on input socket to consume headers and then return
    a tuple of message type, message length.

    :param session: Push Session to read data for.

    Returns response type (i.e. PUBLISH_MESSAGE) if header was completely
    read, otherwise None if header was not completely read.
    """
    try:
        data = session.socket.recv(6 - len(session.data))
        if len(data) == 0:  # No Data on Socket. Likely closed.
            return NO_DATA
        session.data += data
        # Data still not completely read.
        if len(session.data) < 6:
            return INCOMPLETE

    except ssl.SSLError:
        # This can happen when select gets triggered
        # for an SSL socket and data has not yet been
        # read.
        return INCOMPLETE

    session.message_length = struct.unpack('!i', session.data[2:6])[0]
    response_type = struct.unpack('!H', session.data[0:2])[0]

    # Clear out session data as header is consumed.
    session.data = six.b("")
    return response_type


def _read_msg(session):
    """
    Perform a read on input socket to consume message and then return the
    payload and block_id in a tuple.

    :param session: Push Session to read data for.
    """
    if len(session.data) == session.message_length:
        # Data Already completely read.  Return
        return True

    try:
        data = session.socket.recv(session.message_length - len(session.data))
        if len(data) == 0:
            raise PushException("No Data on Socket!")
        session.data += data
    except ssl.SSLError:
        # This can happen when select gets triggered
        # for an SSL socket and data has not yet been
        # read.  Wait for it to get triggered again.
        return False

    # Whether or not all data was read.
    return len(session.data) == session.message_length


class PushException(Exception):
    """Indicates an issue interacting with Push Functionality."""


class PushSession(object):
    """
    A PushSession is responsible for establishing a socket connection
    with iDigi to receive events generated by Devices connected to
    iDigi.
    """

    def __init__(self, callback, monitor_id, client):
        """Creates a PushSession for use with the device cloud

        :param callback: The callback function to invoke when data received.
            Must have 1 required parameter that will contain the payload.
        :param monitor_id: The id of the Monitor to observe.
        :param client: The client object this session is derived from.
        """
        self.callback = callback
        self.monitor_id = monitor_id
        self.client = client
        self.socket = None
        self.log = logging.getLogger("%s.push_session.%s" % (__name__, monitor_id))

        # Received protocol data holders.
        self.data = six.b("")
        self.message_length = 0

    def send_connection_request(self):
        """
        Sends a ConnectionRequest to the iDigi server using the credentials
        established with the id of the monitor as defined in the monitor
        member.
        """
        try:
            self.log.info("Sending ConnectionRequest for Monitor %s."
                          % self.monitor_id)
            # Send connection request and perform a receive to ensure
            # request is authenticated.
            # Protocol Version = 1.
            payload = struct.pack('!H', 0x01)
            # Username Length.
            payload += struct.pack('!H', len(self.client.username))
            # Username.
            payload += six.b(self.client.username)
            # Password Length.
            payload += struct.pack('!H', len(self.client.password))
            # Password.
            payload += six.b(self.client.password)
            # Monitor ID.
            payload += struct.pack('!L', int(self.monitor_id))

            # Header 6 Bytes : Type [2 bytes] & Length [4 Bytes]
            # ConnectionRequest is Type 0x01.
            data = struct.pack("!HL", CONNECTION_REQUEST, len(payload))

            # The full payload.
            data += payload

            # Send Connection Request.
            self.socket.send(data)

            # Set a 60 second blocking on recv, if we don't get any data
            # within 60 seconds, timeout which will throw an exception.
            self.socket.settimeout(60)

            # Should receive 10 bytes with ConnectionResponse.
            response = self.socket.recv(10)

            # Make socket blocking.
            self.socket.settimeout(0)

            if len(response) != 10:
                raise PushException("Length of Connection Request Response "
                                    "(%d) is not 10." % len(response))

            # Type
            response_type = int(struct.unpack("!H", response[0:2])[0])
            if response_type != CONNECTION_RESPONSE:
                raise PushException(
                    "Connection Response Type (%d) is not "
                    "ConnectionResponse Type (%d)." % (response_type, CONNECTION_RESPONSE))

            status_code = struct.unpack("!H", response[6:8])[0]
            self.log.info("Got ConnectionResponse for Monitor %s. Status %s."
                          % (self.monitor_id, status_code))
            if status_code != STATUS_OK:
                raise PushException("Connection Response Status Code (%d) is "
                                    "not STATUS_OK (%d)." % (status_code, STATUS_OK))
        except Exception as exception:
            # TODO(posborne): This is bad!  It isn't necessarily a socket exception!
            # Likely a socket exception, close it and raise an exception.
            self.socket.close()
            self.socket = None
            raise exception

    def start(self):
        """Creates a TCP connection to the device cloud and sends a ConnectionRequest message"""
        self.log.info("Starting Insecure Session for Monitor %s" % self.monitor_id)
        if self.socket is not None:
            raise Exception("Socket already established for %s." % self)

        try:
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.socket.connect((self.client.hostname, PUSH_OPEN_PORT))
            self.socket.setblocking(0)
        except socket.error as exception:
            self.socket.close()
            self.socket = None
            raise

        self.send_connection_request()

    def stop(self):
        """Stop/Close this session

        Close the socket associated with this session and puts Session
        into a state such that it can be re-established later.
        """
        if self.socket is not None:
            self.socket.close()
            self.socket = None
            self.data = None


class SecurePushSession(PushSession):
    """
    SecurePushSession extends PushSession by wrapping the socket connection
    in SSL.  It expects the certificate to match any of those in the passed
    in ca_certs member file.
    """

    def __init__(self, callback, monitor_id, client, ca_certs=None):
        """
        Creates a PushSession wrapped in SSL for use with interacting with
        the device cloud push functionality.

        :param callback: The callback function to invoke when data is received.
            Must have 1 required parameter that will contain the
            payload.
        :param monitor_id: The id of the Monitor to observe.
        :param client: The client object this session is derived from.
        :param ca_certs: Path to a file containing Certificates.
            If not provided, the devicecloud.crt file provided with the module will
            be used.  In most cases, the devicecloud.crt file should be acceptable.
        """
        PushSession.__init__(self, callback, monitor_id, client)
        # Fall back on devicecloud.crt in the same path as this module if not
        # specified.
        if ca_certs is None:
            ca_certs = pkg_resources.resource_filename("devicecloud.data", DEFAULT_CRT_NAME)
        self.ca_certs = ca_certs

    def start(self):
        """
        Creates a SSL connection to the iDigi Server and sends a
        ConnectionRequest message.
        """
        self.log.info("Starting SSL Session for Monitor %s."
                      % self.monitor_id)
        if self.socket is not None:
            raise Exception("Socket already established for %s." % self)

        try:
            # Create socket, wrap in SSL and connect.
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            # Validate that certificate server uses matches what we expect.
            if self.ca_certs is not None:
                self.socket = ssl.wrap_socket(self.socket,
                                              cert_reqs=ssl.CERT_REQUIRED,
                                              ca_certs=self.ca_certs)
            else:
                self.socket = ssl.wrap_socket(self.socket)

            self.socket.connect((self.client.hostname, PUSH_SECURE_PORT))
            self.socket.setblocking(0)
        except Exception as exception:
            self.socket.close()
            self.socket = None
            raise exception

        self.send_connection_request()


class CallbackWorkerPool(object):
    """
    A Worker Pool implementation that creates a number of predefined threads
    used for invoking Session callbacks.
    """

    def __init__(self, write_queue=None, size=1):
        """
        Creates a Callback Worker Pool for use in invoking Session Callbacks
        when data is received by a push client.

        :param write_queue: Queue used for queueing up socket write events
            for when a payload message is received and processed.
        :param size: The number of worker threads to invoke callbacks.
        """
        # Used to queue up PublishMessageReceived events to be sent back to
        # the iDigi server.
        self._write_queue = write_queue
        # Used to queue up sessions and data to callback with.
        self._queue = Queue(size)
        # Number of workers to create.
        self.size = size
        self.log = logging.getLogger('{}.callback_worker_pool'.format(__name__))

        for _ in range(size):
            worker = Thread(target=self._consume_queue)
            worker.daemon = True
            worker.start()

    def _consume_queue(self):
        """
        Continually blocks until data is on the internal queue, then calls
        the session's registered callback and sends a PublishMessageReceived
        if callback returned True.
        """
        while True:
            session, block_id, raw_data = self._queue.get()
            data = json.loads(raw_data.decode('utf-8'))  # decode as JSON
            try:
                result = session.callback(data)
                if result is None:
                    self.log.warn("Callback %r returned None, expected boolean.  Messages "
                                  "are not marked as received unless True is returned", session.callback)
                elif result:
                    # Send a Successful PublishMessageReceived with the
                    # block id sent in request
                    if self._write_queue is not None:
                        response_message = struct.pack('!HHH',
                                                       PUBLISH_MESSAGE_RECEIVED,
                                                       block_id, 200)
                        self._write_queue.put((session.socket, response_message))
            except Exception as exception:
                self.log.exception(exception)

            self._queue.task_done()

    def queue_callback(self, session, block_id, data):
        """
        Queues up a callback event to occur for a session with the given
        payload data.  Will block if the queue is full.

        :param session: the session with a defined callback function to call.
        :param block_id: the block_id of the message received.
        :param data: the data payload of the message received.
        """
        self._queue.put((session, block_id, data))


class TCPClientManager(object):
    """A Client for the 'Push' feature in the device cloud"""

    def __init__(self, conn, secure=True, ca_certs=None, workers=1):
        """
        Arbitrator for multiple TCP Client Sessions

        :param conn: The :class:`devicecloud.DeviceCloudConnection` to use
        :param secure: Whether or not to create a secure SSL wrapped session.
        :param ca_certs: Path to a file containing Certificates.
            If not provided, the devicecloud.crt file provided with the module will
            be used.  In most cases, the devicecloud.crt file should be acceptable.
        :param workers: Number of workers threads to process callback calls.
        """
        self._conn = conn
        self._secure = secure
        self._ca_certs = ca_certs

        # A dict mapping Sockets to their PushSessions
        self.sessions = {}
        # IO thread is used monitor sockets and consume data.
        self._io_thread = None
        # Writer thread is used to send data on sockets.
        self._writer_thread = None
        # Write queue is used to queue up data to write to sockets.
        self._write_queue = Queue()
        # A pool that monitors callback events and invokes them.
        self._callback_pool = CallbackWorkerPool(self._write_queue, size=workers)

        self.closed = False
        self.log = logging.getLogger(__name__)

    @property
    def hostname(self):
        return self._conn.hostname

    @property
    def username(self):
        return self._conn.username

    @property
    def password(self):
        return self._conn.password

    def _restart_session(self, session):
        """Restarts and re-establishes session

        :param session: The session to restart
        """
        # remove old session key, if socket is None, that means the
        # session was closed by user and there is no need to restart.
        if session.socket is not None:
            self.log.info("Attempting restart session for Monitor Id %s."
                          % session.monitor_id)
            del self.sessions[session.socket.fileno()]
            session.stop()
            session.start()
            self.sessions[session.socket.fileno()] = session

    def _writer(self):
        """
        Indefinitely checks the writer queue for data to write
        to socket.
        """
        while not self.closed:
            try:
                sock, data = self._write_queue.get(timeout=0.1)
                self._write_queue.task_done()
                sock.send(data)
            except Empty:
                pass  # nothing to write after timeout
            except socket.error as err:
                if err.errno == errno.EBADF:
                    self._clean_dead_sessions()

    def _clean_dead_sessions(self):
        """
        Traverses sessions to determine if any sockets
        were removed (indicates a stopped session).
        In these cases, remove the session.
        """
        for sck in list(self.sessions.keys()):
            session = self.sessions[sck]
            if session.socket is None:
                del self.sessions[sck]

    def _select(self):
        """
        While the client is not marked as closed, performs a socket select
        on all PushSession sockets.  If any data is received, parses and
        forwards it on to the callback function.  If the callback is
        successful, a PublishMessageReceived message is sent.
        """
        try:
            while not self.closed:
                try:
                    inputready =  select.select(self.sessions.keys(), [], [], 0.1)[0]
                    for sock in inputready:
                        session = self.sessions[sock]
                        sck = session.socket

                        if sck is None:
                            # Socket has since been deleted, continue
                            continue

                        # If no defined message length, nothing has been
                        # consumed yet, parse the header.
                        if session.message_length == 0:
                            # Read header information before receiving rest of
                            # message.
                            response_type = _read_msg_header(session)
                            if response_type == NO_DATA:
                                # No data could be read, assume socket closed.
                                if session.socket is not None:
                                    self.log.error("Socket closed for Monitor %s." % session.monitor_id)
                                    self._restart_session(session)
                                continue
                            elif response_type == INCOMPLETE:
                                # More Data to be read.  Continue.
                                continue
                            elif response_type != PUBLISH_MESSAGE:
                                self.log.warn("Response Type (%x) does not match PublishMessage (%x)"
                                              % (response_type, PUBLISH_MESSAGE))
                                continue

                        try:
                            if not _read_msg(session):
                                # Data not completely read, continue.
                                continue
                        except PushException as err:
                            # If Socket is None, it was closed,
                            # otherwise it was closed when it shouldn't
                            # have been restart it.
                            session.data = six.b("")
                            session.message_length = 0

                            if session.socket is None:
                                del self.sessions[sck]
                            else:
                                self.log.exception(err)
                                self._restart_session(session)
                            continue

                        # We received full payload,
                        # clear session data and parse it.
                        data =  session.data
                        session.data = six.b("")
                        session.message_length = 0
                        block_id = struct.unpack('!H', data[0:2])[0]
                        compression = struct.unpack('!B', data[4:5])[0]
                        payload = data[10:]

                        if compression == 0x01:
                            # Data is compressed, uncompress it.
                            payload = zlib.decompress(payload)

                        # Enqueue payload into a callback queue to be
                        # invoked
                        self._callback_pool.queue_callback(session, block_id, payload)
                except select.error as err:
                    # Evaluate sessions if we get a bad file descriptor, if
                    # socket is gone, delete the session.
                    if err.args[0] == errno.EBADF:
                        self._clean_dead_sessions()
                except Exception as err:
                    self.log.exception(err)
        finally:
            for session in self.sessions.values():
                if session is not None:
                    session.stop()

    def _init_threads(self):
        """Initializes the IO and Writer threads"""
        if self._io_thread is None:
            self._io_thread = Thread(target=self._select)
            self._io_thread.start()

        if self._writer_thread is None:
            self._writer_thread = Thread(target=self._writer)
            self._writer_thread.start()

    def create_session(self, callback, monitor_id):
        """
        Creates and Returns a PushSession instance based on the input monitor
        and callback.  When data is received, callback will be invoked.
        If neither monitor or monitor_id are specified, throws an Exception.

        :param callback: Callback function to call when PublishMessage
            messages are received. Expects 1 argument which will contain the
            payload of the pushed message.  Additionally, expects
            function to return True if callback was able to process
            the message, False or None otherwise.
        :param monitor_id: The id of the Monitor, will be queried
            to understand parameters of the monitor.
        """
        self.log.info("Creating Session for Monitor %s." % monitor_id)
        session = SecurePushSession(callback, monitor_id, self, self._ca_certs) \
            if self._secure else PushSession(callback, monitor_id, self)

        session.start()
        self.sessions[session.socket.fileno()] = session

        self._init_threads()
        return session

    def stop(self):
        """Stops all session activity.

        Blocks until io and writer thread dies
        """
        if self._io_thread is not None:
            self.log.info("Waiting for I/O thread to stop...")
            self.closed = True
            self._io_thread.join()

        if self._writer_thread is not None:
            self.log.info("Waiting for Writer Thread to stop...")
            self.closed = True
            self._writer_thread.join()

        self.log.info("All worker threads stopped.")