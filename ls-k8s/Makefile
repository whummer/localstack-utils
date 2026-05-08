CLUSTER    := ls-k8s-cluster
NAMESPACE  := localstack
RELEASE    := localstack
CONTEXT    := k3d-$(CLUSTER)
HOST_PORT  := 4566
NODE_PORT  := 31566
VENV       := tests/.venv

export K8S_CONTEXT   := $(CONTEXT)
export K8S_NAMESPACE := $(NAMESPACE)
export LS_ENDPOINT   := http://localhost:$(HOST_PORT)

.PHONY: cluster-up cluster-down deploy-ls test clean help

cluster-up: ## Create k3d cluster and deploy LocalStack with Kubernetes Lambda executor
	@bash scripts/cluster-up.sh $(CLUSTER) $(NODE_PORT) $(HOST_PORT)

cluster-down: ## Tear down the k3d cluster
	@bash scripts/cluster-down.sh $(CLUSTER)

deploy-ls: ## Re-deploy / upgrade LocalStack (cluster must already be running)
	helm repo update localstack
	helm upgrade --install $(RELEASE) localstack/localstack \
	  --kube-context $(CONTEXT) \
	  --namespace $(NAMESPACE) \
	  --values k8s/values.yaml \
	  --wait --timeout 180s

$(VENV):
	python3 -m venv $(VENV)
	$(VENV)/bin/pip install -q -r tests/requirements.txt

test: $(VENV) ## Run Lambda-on-Kubernetes integration tests
	$(VENV)/bin/pytest tests/ -v -s

logs: ## Tail LocalStack pod logs
	kubectl --context $(CONTEXT) -n $(NAMESPACE) logs -f deploy/$(RELEASE)

pods: ## List all pods in the LocalStack namespace
	kubectl --context $(CONTEXT) -n $(NAMESPACE) get pods -o wide

clean: ## Remove the Python virtualenv
	rm -rf $(VENV)

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*##' $(MAKEFILE_LIST) | \
	  awk 'BEGIN{FS=":.*##"}{printf "  %-16s %s\n", $$1, $$2}'
