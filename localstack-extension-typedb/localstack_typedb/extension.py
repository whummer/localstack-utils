from localstack_typedb.utils import ProxiedDockerContainerExtension


class TypeDbExtension(ProxiedDockerContainerExtension):
    name = "localstack-typedb-extension"

    HOST = "typedb.localhost.localstack.cloud"

    def __init__(self):
        super().__init__(image_name="typedb/typedb", host=self.HOST)
