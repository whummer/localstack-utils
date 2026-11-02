export LOCALSTACK_ENDPOINT ?= http://localhost.localstack.cloud:4566
export AWS_ENDPOINT_URL = $(LOCALSTACK_ENDPOINT)
export AWS_ACCESS_KEY_ID = test
export AWS_SECRET_ACCESS_KEY = test
export AWS_DEFAULT_REGION = us-east-1

.PHONY: help deploy url test clean
help: ## list targets
	@grep -E '^[a-z]+:.*##' $(MAKEFILE_LIST) | sed 's/:.*##/ -/'
deploy: ## build, deploy the stack, seed stock
	./scripts/deploy.sh
url: ## print the web function URL
	@aws cloudformation describe-stacks --stack-name procurement --query 'Stacks[0].Outputs[0].OutputValue' --output text
test: ## run integration tests against the deployed stack
	python3 -m pytest -v tests
clean: ## delete the stack
	aws cloudformation delete-stack --stack-name procurement; rm -rf build
