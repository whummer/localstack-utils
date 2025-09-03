import re
import logging
from functools import cache
from typing import Callable
import requests

from localstack import config
from localstack_typedb.utils.h2_proxy import apply_http2_patches_for_grpc_support
from localstack.utils.docker_utils import DOCKER_CLIENT
from localstack.extensions.api import Extension, http
from localstack.http import Request
from localstack.utils.container_utils.container_client import PortMappings
from localstack.utils.net import get_addressable_container_host
from localstack.utils.sync import retry

LOG = logging.getLogger(__name__)
LOG.setLevel(logging.DEBUG if config.DEBUG else logging.INFO)
logging.basicConfig()

TYPEDB_PORT = 1729


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
        apply_http2_patches_for_grpc_support(TYPEDB_PORT)
        self.start_container()

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
