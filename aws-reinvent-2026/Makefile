export LOCALSTACK_ENDPOINT ?= http://localhost.localstack.cloud:4566
export AWS_ENDPOINT_URL = $(LOCALSTACK_ENDPOINT)
export AWS_ACCESS_KEY_ID = test
export AWS_SECRET_ACCESS_KEY = test
export AWS_DEFAULT_REGION = us-east-1

.PHONY: help deploy reset url open test clean
help: ## list targets
	@grep -E '^[a-z]+:.*##' $(MAKEFILE_LIST) | sed 's/:.*##/ -/'
deploy: ## build, deploy the stack, seed the demo data
	./scripts/deploy.sh
reset: ## reset demo data: clear runs and chaos, hold back the DE bank feed again
	python3 scripts/seed.py
url: ## print the web function URL
	@./scripts/url.sh
open: ## open the web function URL in the browser (the endpoint id is random per deploy)
	@u=$$(./scripts/url.sh) && echo $$u && (open $$u 2>/dev/null || xdg-open $$u)
test: ## run integration tests against the deployed stack
	python3 -m pytest -v tests
clean: ## delete the stack
	aws cloudformation delete-stack --stack-name close-copilot; python3 scripts/deploy_agent.py --delete; rm -rf build
