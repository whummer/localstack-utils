FC_VERSION   := v1.10.1
CI_TRACK     := v1.10
UNAME_M      := $(shell uname -m)
# Firecracker's release/CI artifact names use "aarch64"/"x86_64", not the
# "arm64"/"amd64" spellings uname reports on macOS or Debian-flavored Linux.
ARCH         := $(if $(filter arm64,$(UNAME_M)),aarch64,$(if $(filter amd64,$(UNAME_M)),x86_64,$(UNAME_M)))
WORK_DIR     := work
TAP_DEV      := fc-ls-tap0
TAP_IP       := 172.16.0.1
VM_IP        := 172.16.0.2
VM_MASK      := 255.255.255.0
VCPUS        := 2
MEM_MB       := 4096
BUCKET       := firecracker-demo
LAMBDA_FN    := firecracker-demo-fn

export WORK_DIR TAP_DEV TAP_IP VM_IP VM_MASK VCPUS MEM_MB BUCKET LAMBDA_FN FC_VERSION CI_TRACK ARCH LOCALSTACK_AUTH_TOKEN

.PHONY: help download rootfs up test logs down clean

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*##' $(MAKEFILE_LIST) | \
	  awk 'BEGIN{FS=":.*##"}{printf "  %-10s %s\n", $$1, $$2}'

download: ## Fetch the firecracker binary, guest kernel and base rootfs
	@bash scripts/download-assets.sh

rootfs: download ## Build a LocalStack-flavored guest rootfs image
	@bash scripts/build-rootfs.sh

up: rootfs ## Create the tap device and boot the LocalStack microVM
	@bash scripts/run-vm.sh

test: ## Run S3 + Lambda smoke tests against the running LocalStack instance
	@bash scripts/smoke-test.sh

logs: ## Tail the microVM's console/boot log
	@tail -n 100 -f $(WORK_DIR)/firecracker.log

down: ## Stop the microVM and remove the tap device
	@bash scripts/teardown.sh

clean: down ## Remove all downloaded/built artifacts
	rm -rf $(WORK_DIR)
