import re
import logging
from functools import cache
from typing import Callable

import requests
import httpx._utils
from localstack import config
from rolo import Response
from rolo.proxy import Proxy
from localstack.utils.docker_utils import DOCKER_CLIENT
from localstack.extensions.api import Extension, http
from localstack.http import Request
from localstack.utils.container_utils.container_client import PortMappings
from localstack.utils.net import get_addressable_container_host
from localstack.utils.sync import retry

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
        tcp_proxy_ports: list[int] | None = None,
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

    def __call__(self, request, path, *args, **kwargs):
        print("!CALL", request, path, type(request), request.__dict__)

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
        import traceback

        print("!request in ls-ext", request)
        print("".join(traceback.format_stack()))

        with httpx.Client(http2=True, http1=False) as client:
            target_url = f"{self._get_target_url(request)}{forward_path}"
            print("!target_url", target_url)
            response = client.request(
                method=request.method,
                url=target_url,
                headers=request.headers,
                content=request.data,
            )
            result = Response(
                response=response.content,
                status=response.status_code,
                headers=dict(response.headers),
            )
            content = request.stream.read()
            print(
                "!response.headers",
                dict(response.headers),
                response.content,
                response.status_code,
                content,
            )
            # request.write(b'foo response')
            # return result

    def _get_target_url(self, request: Request) -> str:
        port = self.extension.container_ports[0]
        if self.extension.request_to_port_router:
            port = self.extension.request_to_port_router(request)

        container_host = get_addressable_container_host()
        base_url = f"http://{container_host}:{port}"
        return base_url
