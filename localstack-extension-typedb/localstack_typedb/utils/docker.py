import re
import logging

from localstack.utils.docker_utils import DOCKER_CLIENT
from localstack.extensions.api import Extension, http
from rolo.router import RuleAdapter, WithHost
from localstack.http import Request, route


LOG = logging.getLogger(__name__)


class ProxiedDockerContainerExtension(Extension):
    name: str
    """Name of this extension"""
    image_name: str
    """Docker image name"""
    container_name: str | None
    """Name of the Docker container spun up by the extension"""
    host: str | None
    """
    Optional host on which to expose the container endpoints.
    Can be either a static hostname, or a pattern like `<regex("(.+\.)?"):subdomain>myext.<domain>`
    """
    path: str | None
    """Optional path on which to expose the container endpoints."""

    def __init__(
        self,
        image_name: str,
        host: str | None = None,
        path: str | None = None,
        container_name: str | None = None,
    ):
        self.image_name = image_name
        self.host = host
        self.path = path
        self.container_name = container_name

    def update_gateway_routes(self, router: http.Router[http.RouteHandler]):
        resource = RuleAdapter(ProxyResource())
        if self.host:
            resource = WithHost(self.host, [resource])
        if self.path:
            raise NotImplementedError(
                "Path-based routing not yet implemented for this extension"
            )
        resource.add_rule(resource)
        router.add(resource)
        # TODO ...

    def on_platform_shutdown(self):
        container_name = self._get_container_name()
        DOCKER_CLIENT.remove_container(container_name, force=True)

    def _get_container_name(self) -> str:
        if self.container_name:
            return self.container_name
        name = f"ls-ext-{self.name}"
        name = re.sub(r"\W", "-", name)
        return name


class ProxyResource:
    @route("/<path:path>")
    def index(self, request: Request, path: str, *args, **kwargs):
        # TODO
        return {}
