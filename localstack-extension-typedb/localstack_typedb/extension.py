from localstack_typedb.utils.docker import ProxiedDockerContainerExtension


class TypeDbExtension(ProxiedDockerContainerExtension):
    name = "localstack-typedb-extension"

    HOST = "typedb.<domain>"

    def __init__(self):
        super().__init__(
            image_name="typedb/typedb", container_ports=[8000, 1729], host=self.HOST
        )
