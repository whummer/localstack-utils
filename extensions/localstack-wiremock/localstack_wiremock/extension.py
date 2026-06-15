from localstack_wiremock.utils.docker import ProxiedDockerContainerExtension


class WireMockExtension(ProxiedDockerContainerExtension):
    name = "localstack-wiremock"

    HOST = "wiremock.<domain>"
    # name of the Docker image to spin up
    DOCKER_IMAGE = "wiremock/wiremock"
    # name of the container
    CONTAINER_NAME = "ls-wiremock"

    def __init__(self):
        super().__init__(
            image_name=self.DOCKER_IMAGE,
            container_ports=[8080],
            container_name=self.CONTAINER_NAME,
            host=self.HOST,
        )
