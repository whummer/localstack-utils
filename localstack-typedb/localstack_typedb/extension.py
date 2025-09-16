import os
import shlex

from localstack.utils.docker_utils import DOCKER_CLIENT
from localstack_typedb.utils.docker import ProxiedDockerContainerExtension
from rolo import Request

# environment variable for user-defined command args to pass to TypeDB
ENV_CMD_FLAGS = "TYPEDB_FLAGS"


class TypeDbExtension(ProxiedDockerContainerExtension):
    name = "localstack-typedb"

    HOST = "typedb.<domain>"
    # name of the Docker image to spin up
    DOCKER_IMAGE = "typedb/typedb"
    # default command args to pass to TypeDB
    DEFAULT_CMD_FLAGS = ["--diagnostics.reporting.metrics=false"]
    # default port for TypeDB HTTP2/gRPC endpoint
    TYPEDB_PORT = 1729

    def __init__(self):
        command_flags = (os.environ.get(ENV_CMD_FLAGS) or "").strip()
        command_flags = self.DEFAULT_CMD_FLAGS + shlex.split(command_flags)
        command = self._get_image_command() + command_flags
        super().__init__(
            image_name=self.DOCKER_IMAGE,
            container_ports=[8000, 1729],
            host=self.HOST,
            request_to_port_router=self.request_to_port_router,
            command=command,
            http2_ports=[self.TYPEDB_PORT],
        )

    def _get_image_command(self) -> list[str]:
        result = DOCKER_CLIENT.inspect_image(self.DOCKER_IMAGE)
        image_command = result["Config"]["Cmd"]
        return image_command

    def request_to_port_router(self, request: Request):
        # TODO add REST API / gRPC routing based on request
        return 1729
