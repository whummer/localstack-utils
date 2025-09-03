import socket

from twisted.internet import reactor

from localstack.utils.patch import patch
from twisted.web._http2 import H2Connection


class TcpForwarder:
    """Simple helper class for bidirectional forwarding of TPC traffic."""

    buffer_size = 1024

    def __init__(self, port: int, host: str = "localhost"):
        self.port = port
        self.host = host
        self._socket = None
        self.connect()

    def connect(self):
        if not self._socket:
            self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._socket.connect((self.host, self.port))

    def receive_loop(self, callback):
        while True:
            data = self._socket.recv(self.buffer_size)
            callback(data)
            if not data:
                break

    def send(self, data):
        self._socket.sendall(data)


def apply_http2_patches_for_grpc_support(target_port: int):
    """
    Apply some patches to proxy incoming gRPC requests and forward them to a target port.
    Note: this is a very brute-force approach and needs to be fixed/enhanced over time!
    """

    @patch(H2Connection.connectionMade)
    def _connectionMade(fn, self, *args, **kwargs):
        def _process(data):
            self.transport.write(data)

        # TODO: make port configurable
        self._ls_forwarder = TcpForwarder(target_port)
        reactor.getThreadPool().callInThread(self._ls_forwarder.receive_loop, _process)

    @patch(H2Connection.dataReceived)
    def _dataReceived(fn, self, data, *args, **kwargs):
        forwarder = getattr(self, "_ls_forwarder", None)
        if not forwarder:
            return fn(self, data, *args, **kwargs)
        forwarder.send(data)
