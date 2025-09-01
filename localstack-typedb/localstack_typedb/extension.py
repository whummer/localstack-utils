from localstack_typedb.utils.docker import ProxiedDockerContainerExtension
from rolo import Request


class TypeDbExtension(ProxiedDockerContainerExtension):
    name = "localstack-typedb"

    HOST = "typedb.<domain>"
    DOCKER_IMAGE = "typedb/typedb"

    def __init__(self):
        super().__init__(
            image_name=self.DOCKER_IMAGE,
            container_ports=[8000, 1729],
            host=self.HOST,
            request_to_port_router=self.request_to_port_router,
        )

    def request_to_port_router(self, request: Request):
        print("!REQ", request, request.headers, request.data)
        print("!request", request, request.__dict__)
        return 1729
