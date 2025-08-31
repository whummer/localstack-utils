from localstack_typedb.utils.docker import ProxiedDockerContainerExtension


class TypeDbExtension(ProxiedDockerContainerExtension):
    name = "localstack-typedb"

    HOST = "typedb.<domain>"
    DOCKER_IMAGE = "typedb/typedb"

    def __init__(self):
        super().__init__(
            image_name=self.DOCKER_IMAGE, container_ports=[8000, 1729], host=self.HOST
        )
