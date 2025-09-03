import queue
import re
import logging
import socket
from functools import cache
from typing import Callable

from localstack.runtime import events
from twisted.internet import reactor
from twisted.web._http2 import H2Stream

import requests
import httpx._utils
from localstack import config
from localstack.services.edge import ROUTER
from localstack.utils.patch import patch
from localstack.utils.strings import to_str, to_bytes
from localstack.utils.docker_utils import DOCKER_CLIENT
from localstack.extensions.api import Extension, http
from localstack.http import Request
from localstack.utils.container_utils.container_client import PortMappings
from localstack.utils.net import get_addressable_container_host
from localstack.utils.sync import retry
from twisted.web._http2 import H2Connection
from twisted.web.server import NOT_DONE_YET

LOG = logging.getLogger(__name__)
LOG.setLevel(logging.DEBUG if config.DEBUG else logging.INFO)
logging.basicConfig()


class ProxiedDockerContainerExtension(Extension):
    name: str
    """Name of this extension"""
    image_name: str
    """Docker image name"""
    container_name: str | None
    """Name of the Docker container spun up by the extension"""
    container_ports: list[int]
    """List of network ports of the Docker container spun up by the extension"""
    host: str | None
    """
    Optional host on which to expose the container endpoints.
    Can be either a static hostname, or a pattern like `<regex("(.+\.)?"):subdomain>myext.<domain>`
    """
    path: str | None
    """Optional path on which to expose the container endpoints."""

    request_to_port_router: Callable[[Request], int] | None
    """Callable that returns the target port for a given request, for routing purposes"""

    def __init__(
        self,
        image_name: str,
        container_ports: list[int],
        host: str | None = None,
        path: str | None = None,
        container_name: str | None = None,
        request_to_port_router: Callable[[Request], int] | None = None,
    ):
        self.image_name = image_name
        self.container_ports = container_ports
        self.host = host
        self.path = path
        self.container_name = container_name
        self.request_to_port_router = request_to_port_router

    def update_gateway_routes(self, router: http.Router[http.RouteHandler]):
        if self.path:
            raise NotImplementedError(
                "Path-based routing not yet implemented for this extension"
            )
        router.add(
            path="/<path:path>",
            host=self.host,
            endpoint=ProxyResource(self),
            defaults={},
            strict_slashes=False,
        )
        _apply_patches(self)

    def on_platform_shutdown(self):
        self._remove_container()

    def _get_container_name(self) -> str:
        if self.container_name:
            return self.container_name
        name = f"ls-ext-{self.name}"
        name = re.sub(r"\W", "-", name)
        return name

    @cache
    def start_container(self) -> None:
        container_name = self._get_container_name()
        LOG.debug("Starting extension container %s", container_name)

        ports = PortMappings()
        for port in self.container_ports:
            ports.add(port)
        DOCKER_CLIENT.run_container(
            self.image_name,
            detach=True,
            remove=True,
            name=container_name,
            ports=ports,
        )

        main_port = self.container_ports[0]
        container_host = get_addressable_container_host()

        def _ping_endpoint():
            # TODO: allow defining a custom healthcheck endpoint ...
            response = requests.get(f"http://{container_host}:{main_port}/")
            assert response.ok

        try:
            retry(_ping_endpoint, retries=40, sleep=1)
        except Exception as e:
            LOG.info("Failed to connect to container %s: %s", container_name, e)
            self._remove_container()
            raise

        # TODO: enable support for TCP port proxying!
        # for port in self.container_ports:
        #     proxy = TCPProxy(
        #         target_address="localhost",
        #         target_port=port,
        #         port=...,
        #         host="...",
        #     )

        LOG.debug("Successfully started extension container %s", container_name)

    def _remove_container(self):
        container_name = self._get_container_name()
        LOG.debug("Stopping extension container %s", container_name)
        DOCKER_CLIENT.remove_container(
            container_name, force=True, check_existence=False
        )


class TcpForwarder:
    def __init__(self, port: int, host: str = "localhost"):
        self.port = port
        self.host = host
        self._buffer_size = 1024
        self._socket = None
        self.connect()

    def connect(self):
        if not self._socket:
            self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._socket.connect((self.host, self.port))

    def receive_loop(self, callback):
        self.connect()
        while True:
            data = self._socket.recv(self._buffer_size)
            callback(data)
            if not data:
                break

    def send(self, data):
        self._socket.sendall(data)


def _apply_patches(extension):
    @patch(H2Connection.connectionMade)
    def _connectionMade(fn, self, *args, **kwargs):
        extension.start_container()

        self._ls_forwarder = TcpForwarder(1729)
        print("!!self.transport", self.transport, self.transport.__dict__)

        def _print(data):
            print("!RETURN RESPONSE received upstream", data)
            self.transport.write(data)

        reactor.getThreadPool().callInThread(self._ls_forwarder.receive_loop, _print)

    @patch(H2Connection.dataReceived)
    def _dataReceived(fn, self, data, *args, **kwargs):
        forwarder = getattr(self, "_ls_forwarder", None)
        if not forwarder:
            return fn(self, data, *args, **kwargs)
        print("!!RECEIVED FROM client", data)
        forwarder.send(data)


# @patch(H2Connection._requestReceived)
def _requestReceived(fn, self, event, *args, **kwargs):
    # call upstream function
    fn(self, event, *args, **kwargs)

    headers = {to_str(k): to_str(v) for k, v in event.headers}
    method = headers[":method"]
    path = headers[":path"]
    host = headers[":authority"]
    headers = {k: v for k, v in headers.items() if not k.startswith(":")}
    headers["Host"] = host

    request = Request(
        method=method,
        path=path,
        headers=headers,
        body=None,
    )
    request.h2_stream = self.streams[event.stream_id]
    matcher = ROUTER.url_map.bind(server_name=request.host)
    handler, args = matcher.match(request.path, method=request.method)
    ROUTER.dispatcher(request, handler, args)

    return NOT_DONE_YET


def get_environment_proxies(*args, **kwargs):
    # small patch to fix handling of proxy keys like 'all://*[::1]'
    result = get_environment_proxies_orig(*args, **kwargs)
    return {k: v for k, v in result.items() if "[::1]" not in k}


get_environment_proxies_orig = httpx._utils.get_environment_proxies
httpx._utils.get_environment_proxies = get_environment_proxies
httpx._client.get_environment_proxies = get_environment_proxies


class ProxyResource:
    """
    Simple proxy resource that forwards incoming requests from the
    LocalStack Gateway to the target Docker container.
    """

    extension: ProxiedDockerContainerExtension

    def __init__(self, extension: ProxiedDockerContainerExtension):
        self.extension = extension

    def __call__(self, request: Request, path, *args, **kwargs):
        stream = getattr(request, "h2_stream", None)
        if not isinstance(stream, H2Stream):
            return

        self.extension.start_container()

        stream.h2_client = httpx.Client(http2=True, http1=False)
        msg_queue = queue.Queue()

        def _data_generator():
            while True:
                try:
                    message = msg_queue.get(timeout=3)
                except queue.Empty:
                    if events.infra_stopping.is_set():
                        return
                    continue
                if message is None:
                    break
                yield message
                if message == b"":
                    break
                # stream._send100Continue()
                # return

        def _receive_chunk(data, flowControlledLength):
            print("!_receive_chunk", data, len(data), flowControlledLength)
            msg_queue.put(data)
            stream._conn.openStreamWindow(stream.streamID, flowControlledLength)
            print("!_receive_chunk DONE", flowControlledLength)

        def _request_complete():
            msg_queue.put(None)
            # return _request_complete_orig()

        stream.receiveDataChunk = _receive_chunk
        _request_complete_orig = stream.requestComplete
        stream.requestComplete = _request_complete

        def _run_request():
            headers = dict(request.headers)
            headers.pop("content-length", None)
            target_url = f"{self._get_target_url(request)}/{path}"
            streaming_response = stream.h2_client.stream(
                method=to_str(request.method),
                url=target_url,
                content=_data_generator(),
                headers=headers,
            )

            with streaming_response as response:
                res_headers = [
                    (to_bytes(k), to_bytes(v))
                    for k, v in dict(response.headers).items()
                ]
                stream.writeHeaders(
                    None,
                    code=to_bytes(str(response.status_code)),
                    reason=None,
                    headers=res_headers,
                )
                for data in response.iter_bytes():
                    print("!!!!data123", data)
                    stream.write(data)

            stream.requestDone(request)

        reactor.getThreadPool().callInThread(_run_request)

    def _get_target_url(self, request: Request) -> str:
        port = self.extension.container_ports[0]
        if self.extension.request_to_port_router:
            port = self.extension.request_to_port_router(request)
        container_host = get_addressable_container_host()
        base_url = f"http://{container_host}:{port}"
        return base_url
