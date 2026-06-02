.PHONY: install dev test test-unit test-integration lint typecheck build build-lambda push-lambda deploy-lambda

# ─── Local development ────────────────────────────────────────────────────────

install:
	poetry install

dev:
	docker-compose up --build

dev-app:
	uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

# ─── Testing ─────────────────────────────────────────────────────────────────

test:
	poetry run pytest -v

test-unit:
	poetry run pytest tests/unit/ -v

test-integration:
	poetry run pytest tests/integration/ -v

test-cov:
	poetry run pytest --cov=app --cov-report=html -v

# ─── Code quality ─────────────────────────────────────────────────────────────

lint:
	poetry run ruff check app/ tests/

lint-fix:
	poetry run ruff check --fix app/ tests/

typecheck:
	poetry run mypy app/

# ─── Docker (ECS / local) ────────────────────────────────────────────────────

build:
	docker build -t resume-parser:latest .

# ─── Lambda container ────────────────────────────────────────────────────────
# Usage:
#   make build-lambda AWS_ACCOUNT=123456789012 AWS_REGION=us-east-1
#   make push-lambda  AWS_ACCOUNT=123456789012 AWS_REGION=us-east-1

AWS_ACCOUNT ?= $(shell aws sts get-caller-identity --query Account --output text)
AWS_REGION  ?= us-east-1
ECR_REPO    ?= resume-parser-lambda
IMAGE_TAG   ?= latest
ECR_URI      = $(AWS_ACCOUNT).dkr.ecr.$(AWS_REGION).amazonaws.com/$(ECR_REPO)

build-lambda:
	docker build -f Dockerfile.lambda -t $(ECR_REPO):$(IMAGE_TAG) .

push-lambda: build-lambda
	aws ecr get-login-password --region $(AWS_REGION) | \
	  docker login --username AWS --password-stdin $(ECR_URI)
	docker tag $(ECR_REPO):$(IMAGE_TAG) $(ECR_URI):$(IMAGE_TAG)
	docker push $(ECR_URI):$(IMAGE_TAG)

# Update both Lambda functions to use the new image
deploy-lambda: push-lambda
	aws lambda update-function-code \
	  --function-name resume-parser-api \
	  --image-uri $(ECR_URI):$(IMAGE_TAG) \
	  --region $(AWS_REGION)
	aws lambda update-function-code \
	  --function-name resume-parser-worker \
	  --image-uri $(ECR_URI):$(IMAGE_TAG) \
	  --region $(AWS_REGION)
	@echo "Deployed $(ECR_URI):$(IMAGE_TAG) to resume-parser-api and resume-parser-worker"

# ─── Terraform ───────────────────────────────────────────────────────────────

TF_DIR = infrastructure/terraform

tf-init:
	cd $(TF_DIR) && terraform init

tf-plan:
	cd $(TF_DIR) && terraform plan -var-file=terraform.tfvars

tf-apply:
	cd $(TF_DIR) && terraform apply -var-file=terraform.tfvars

tf-destroy:
	@echo "WARNING: this will destroy all infrastructure. Confirm manually."
	cd $(TF_DIR) && terraform destroy -var-file=terraform.tfvars

tf-output:
	cd $(TF_DIR) && terraform output

# Bootstrap: create S3 state bucket + DynamoDB lock table (run once manually)
tf-bootstrap:
	aws s3 mb s3://resume-parser-tfstate --region $(AWS_REGION) || true
	aws s3api put-bucket-versioning \
	  --bucket resume-parser-tfstate \
	  --versioning-configuration Status=Enabled
	aws dynamodb create-table \
	  --table-name resume-parser-tflock \
	  --attribute-definitions AttributeName=LockID,AttributeType=S \
	  --key-schema AttributeName=LockID,KeyType=HASH \
	  --billing-mode PAY_PER_REQUEST \
	  --region $(AWS_REGION) || true
	@echo "Bootstrap complete. Now run: make tf-init"

# ─── Utilities ───────────────────────────────────────────────────────────────

# Generate a new API key and print the hash for seeding into DynamoDB
gen-api-key:
	@python -c "from app.core.security import generate_api_key; k, h = generate_api_key(); print(f'Key:  {k}\nHash: {h}')"

# Seed a key into LocalStack DynamoDB (dev only)
seed-dev-key: gen-api-key
	@echo "Run the output hash in localstack_init.sh or via AWS CLI"
