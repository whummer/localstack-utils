import queue
import re
import logging
from functools import cache
from typing import Callable

from twisted.internet import reactor
from twisted.web._http2 import H2Stream

import requests
import httpx._utils
from localstack import config
from localstack.services.edge import ROUTER
from localstack.utils.patch import patch
from localstack.utils.strings import to_str, to_bytes
from rolo import Response
from rolo.proxy import Proxy
from localstack.utils.docker_utils import DOCKER_CLIENT
from localstack.extensions.api import Extension, http
from localstack.http import Request
from localstack.utils.container_utils.container_client import PortMappings
from localstack.utils.net import get_addressable_container_host
from localstack.utils.sync import retry
from rolo.serving.twisted import (
    TwistedRequestAdapter,
    to_flat_header_list,
)
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
    # TODO: currently not yet used ...
    tcp_proxy_ports: list | None
    tcp_proxies: list[int]

    def __init__(
        self,
        image_name: str,
        container_ports: list[int],
        host: str | None = None,
        path: str | None = None,
        container_name: str | None = None,
        request_to_port_router: Callable[[Request], int] | None = None,
        # tcp_proxy_ports: list[int] | None = None,
    ):
        self.image_name = image_name
        self.container_ports = container_ports
        self.host = host
        self.path = path
        self.container_name = container_name
        # self.tcp_proxy_ports = tcp_proxy_ports
        # self.tcp_proxies = []
        self.request_to_port_router = request_to_port_router

    def update_gateway_routes(self, router: http.Router[http.RouteHandler]):
        # resource = RuleAdapter(ProxyResource(self))
        # if self.host:
        #     resource = WithHost(self.host, [resource])
        # if self.path:
        #     raise NotImplementedError(
        #         "Path-based routing not yet implemented for this extension"
        #     )
        # router.add(resource)
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


# class Http2EnabledRequestsClient(SimpleRequestsClient):
#     def __init__(self, *args, **kwargs):
#         import httpx
#
#         super().__init__(*args, **kwargs)
#         # proxies = httpx._utils.get_environment_proxies()
#         # print("!proxies", proxies)
#         client = httpx.Client(http2=True)
#         self.session.mount("http://", client)


# @patch(WebsocketResourceDecorator.render)
def render(fn, self, request):
    from rolo import Response
    from localstack.utils.strings import to_str
    from werkzeug.datastructures.headers import Headers

    def _processH2Request(request: Request):
        def handler(req, *args):
            print("!!_processH2Request 1234", type(req), req.__dict__)
            response = Response(status=101)
            gateway = self.original._application.gateway
            req.headers = Headers(
                {
                    to_str(k): to_str(v)
                    for k, v in to_flat_header_list(req.requestHeaders)
                }
            )
            req.path = to_str(req.path)
            req.host = req.headers.get("Host") or "localhost"
            req.content_encoding = None
            req.environ = {"RAW_URI": to_str(req.uri)}
            req.shallow = False
            req.values = {}
            req.user_agent = ""
            req.data = req.content.read()
            print("!req details", req.__dict__)
            result = gateway.process(req, response)
            print("!gateway process result", result, response.status_code)

        # WSGIResource also dispatches requests through the threadpool
        self.original._threadpool.callInThread(handler, request)

    print("!!render req patched", request, request.__dict__, request.clientproto)
    if request.clientproto == b"HTTP/2":
        _processH2Request(request)
        return NOT_DONE_YET

    return fn(self, request)


# class H2Request(TwistedRequest):
#     def __init__(self, stream: H2Stream):
#         super().__init__(stream)
#         self.stream = stream
#         print("!H2Request", stream)
#
#     def connectionLost(self, reason):
#         print("!H2Request.connectionLost", reason)
#
#     def handleContentChunk(self, data):
#         print("!H2Request.handleContentChunk", data)


# @patch(H2Stream.__init__)
def h2stream_init(fn, self, *args, **kwargs):
    fn(self, *args, **kwargs)
    self._request = H2Request(self)


@patch(H2Connection._requestReceived)
def _requestReceived(fn, self, event, *args, **kwargs):
    # print("!!_requestReceived 13", event, self)
    result = fn(self, event, *args, **kwargs)

    stream = self.streams[event.stream_id]

    headers = {to_str(k): to_str(v) for k, v in event.headers}
    method = headers[":method"]
    path = headers[":path"]
    host = headers[":authority"]
    headers = {k: v for k, v in headers.items() if not k.startswith(":")}
    # print("!event", event, method, path, headers)
    headers["Host"] = host

    request = Request(
        method=method,
        path=path,
        headers=headers,
        body=None,
    )
    request.h2_stream = stream
    matcher = ROUTER.url_map.bind(server_name=request.host)
    # print("!request.host", request.host, request)
    handler, args = matcher.match(request.path, method=request.method)
    # print("!MATCHER", matcher, handler, args)
    response = ROUTER.dispatcher(request, handler, args)
    # print("!ROUTER.dispatcher", response)

    return result


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
        print("!request in __call__", path, type(request))
        # print("!request.__dict__", request.__dict__)
        stream = getattr(request, "h2_stream", None)
        if not isinstance(stream, H2Stream):
            return

        self.extension.start_container()

        # assert isinstance(stream, H2Stream)
        stream.h2_client = httpx.Client(http2=True, http1=False)
        # h2_request = stream.h2_client.stream

        msg_queue = queue.Queue()

        def _data_generator():
            while True:
                try:
                    message = msg_queue.get(timeout=5)
                except queue.Empty:
                    yield b""
                    return
                if message is None:
                    break
                print("!dequeued message", message)
                yield message
                # stream._send100Continue()
                if message == b"":
                    break

        def _receive_chunk(data, flowControlledLength):
            print("!receive_chunk", self, data, flowControlledLength)
            msg_queue.put(data)
            return

            # result = stream.h2_client.request(
            #     method=request.method,
            #     path=path,
            #     data=data,
            #     headers=request.headers,
            # )
            request.data = data
            result = self._forward_http2_request(request, forward_path=to_str(path))

            print("!receive_chunk response", result, result.headers)
            headers = [
                (to_bytes(k), to_bytes(v)) for k, v in dict(result.headers).items()
            ]

            # headers = {k: v for k, v in dict(response.headers).items() if not k.startswith(":")}
            headers.insert(0, (b":status", to_bytes(str(result.status_code))))

            # note: using `send_headers(..)` instead of ,
            print("!!proxied", result, headers)
            h2_conn = stream._conn
            # h2_conn.conn.send_headers(stream.streamID, headers)
            # h2_conn._tryToWriteControlData()

            # stream.writeHeaders(
            #     None,
            #     code=to_bytes(str(result.status_code)),
            #     reason=None,
            #     headers=headers,
            # )

            stream.write(result.data)

        def _request_complete():
            print("!request_complete", self)
            msg_queue.put(None)
            # return _request_complete_orig()

        # def _handle_data(response):
        #     def receiveDataChunk(self, data, flowControlledLength):

        stream.receiveDataChunk = _receive_chunk
        _request_complete_orig = stream.requestComplete
        stream.requestComplete = _request_complete

        target_url = f"{self._get_target_url(request)}/{path}"
        print("!target_url", target_url, request.method)
        def _run_request():
            print("!run_request", to_str(request.method), target_url, dict(request.headers))
            headers = dict(request.headers)
            headers.pop("content-length", None)
            result = stream.h2_client.request(
                method=to_str(request.method),
                url=target_url,
                content=_data_generator(),
                headers=headers,
            )
            print("!result123 tmp", result, result.__dict__)

            res_headers = [
                (to_bytes(k), to_bytes(v)) for k, v in dict(result.headers).items()
            ]
            stream.writeHeaders(
                None,
                code=to_bytes(str(result.status_code)),
                reason=None,
                headers=res_headers,
            )

            print("!write response content")
            stream.write(result.content)
            print("!ALL DONE")
            # _request_complete_orig()
            # stream._conn._tryToWriteControlData()
            stream.requestDone(request)

        reactor.getThreadPool().callInThread(_run_request)

        # result = self._forward_http2_request(request, forward_path=to_str(request.path))
        # assert len(result.data) == int(result.headers["content-length"])
        # headers = [
        #     (to_bytes(k), to_bytes(v)) for k, v in dict(result.headers).items()
        # ]
        # print("!!proxied", result, headers)
        # stream.writeHeaders(
        #     None,
        #     code=to_bytes(str(result.status_code)),
        #     reason=None,
        #     headers=headers,
        # )
        # stream.write(result.data)

    def __call2__(self, request: TwistedRequestAdapter, path, *args, **kwargs):
        assert isinstance(request, TwistedRequestAdapter)
        self.extension.start_container()

        # print("!CALL", request, path, type(request), request.__dict__)
        result = self._forward_http2_request(request, forward_path=to_str(request.path))
        # print("!!request.channel", request.channel, result, result.data)
        if isinstance(request.channel, H2Stream):
            assert len(result.data) == int(result.headers["content-length"])
            headers = [
                (to_bytes(k), to_bytes(v)) for k, v in dict(result.headers).items()
            ]
            request.channel.writeHeaders(
                None,
                code=to_bytes(str(result.status_code)),
                reason=None,
                headers=headers,
            )
            request.channel.write(result.data)
            # request.channel._send100Continue()
            request.channel.requestDone(None)

    # @route("/<path:path>")
    # def index(self, request: Request, path: str, *args, **kwargs):
    #     return self._proxy_request(request, forward_path=f"/{path}")

    def _proxy_request(self, request: Request, forward_path: str, *args, **kwargs):
        self.extension.start_container()

        print("!REQ", request, request.__dict__)
        if request.environ.get("SERVER_PROTOCOL") == "HTTP/2":
            return self._proxy_http2_request(request, forward_path=forward_path)

        # create proxy
        base_url = self._get_target_url(request)
        proxy = Proxy(forward_base_url=base_url)

        # update content length (may have changed due to content compression)
        if request.method not in ("GET", "OPTIONS"):
            request.headers["Content-Length"] = str(len(request.data))

        # # make sure we're forwarding the correct Host header
        # request.headers["Host"] = f"localhost:{port}"

        # forward the request to the target
        result = proxy.forward(request, forward_path=forward_path)

        return result

    def _proxy_http2_request(
        self, request: Request, forward_path: str, *args, **kwargs
    ):
        result = self._forward_http2_request(request, forward_path=forward_path)
        return result

    def _forward_http2_request(
        self, request: Request, forward_path: str, *args, **kwargs
    ):
        forward_path = (
            f"/{forward_path}" if not forward_path.startswith("/") else forward_path
        )
        with httpx.Client(http2=True, http1=False) as client:
            target_url = f"{self._get_target_url(request)}{forward_path}"
            # print("!request", request.method, target_url, request.headers, request.data)
            print("!target_url", target_url)
            response = client.request(
                method=to_str(request.method),
                url=target_url,
                headers=request.headers,
                content=request.data,
            )
            print("!target_url", target_url, response.content, response.status_code)
            result = Response(
                response=response.content,
                status=response.status_code,
                headers=dict(response.headers),
            )
            return result

    def _get_target_url(self, request: Request) -> str:
        port = self.extension.container_ports[0]
        if self.extension.request_to_port_router:
            port = self.extension.request_to_port_router(request)

        container_host = get_addressable_container_host()
        base_url = f"http://{container_host}:{port}"
        return base_url
