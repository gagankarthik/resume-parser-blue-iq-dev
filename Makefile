.PHONY: install dev test test-unit test-integration lint typecheck build build-lambda

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

# Run this before every commit — auto-fixes all lint issues
precommit:
	poetry run ruff check --fix app/ tests/
	poetry run mypy app/ --ignore-missing-imports

# Install the pre-commit hook (one-time)
hooks-install:
	pip install pre-commit
	pre-commit install
	@echo "Pre-commit hook installed. Lint will now auto-fix on every commit."

typecheck:
	poetry run mypy app/

# ─── Docker (ECS / local) ────────────────────────────────────────────────────

build:
	docker build -t resume-parser:latest .

# ─── Lambda container ────────────────────────────────────────────────────────
# CI OWNS DEPLOYS. There is deliberately no `deploy-lambda` target here.
#
# The old push-lambda/deploy-lambda targets called update-function-code on
# `resume-parser-api` and `resume-parser-worker` — neither function has ever
# existed (the real one is `resume-parser-production-api`, and there is no
# separate worker; the API self-invokes). They also defaulted to us-east-1 while
# the stack lives in us-east-2. Running them did nothing good and could touch the
# wrong account, so they are gone rather than repaired.
#
#   Deploy:   push to main   → .github/workflows/deploy.yml
#   Rollback: workflow_dispatch → .github/workflows/rollback.yml
#
# build-lambda stays: it is local-only and useful for checking the image builds
# before you open a PR.

AWS_REGION ?= us-east-2
ECR_REPO   ?= resume-parser-production-lambda
IMAGE_TAG  ?= local

build-lambda:
	docker build -f Dockerfile.lambda -t $(ECR_REPO):$(IMAGE_TAG) .

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
