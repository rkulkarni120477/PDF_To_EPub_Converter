# PDF to ePub Platform

Microservices-based PDF to ePub conversion platform.

## Solution layout

- `services/` - independently deployable FastAPI microservices
- `web/` - user-facing web application placeholder
- `shared/` - reusable Python libraries, contracts, middleware, and test utilities
- `infra/` - Docker, Kubernetes, Terraform, and Helm deployment assets

## Local development

Create a virtual environment for the service you are working on, install its requirements, and run the service with Uvicorn. Each service owns its dependencies and can be containerized independently.

## Workspace

Open `pdf-epub-platform.code-workspace` in VS Code to load the solution folders together.
