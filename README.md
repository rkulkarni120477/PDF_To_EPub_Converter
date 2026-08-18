# PDF to ePub Platform

Microservices-based PDF to ePub conversion platform.

## Solution layout

- `services/` - independently deployable FastAPI microservices
- `web/` - user-facing web application placeholder
- `shared/` - reusable Python libraries, contracts, middleware, and test utilities
- `infra/` - Docker, Kubernetes, Terraform, and Helm deployment assets

## Local development

Create a virtual environment for the service you are working on, install its requirements, and run the service with Uvicorn. Each service owns its dependencies and can be containerized independently.

### Azure Document Intelligence

The conversion UI includes an optional **Use Azure Document Intelligence** toggle. Configure the gateway with a local `.env` file or process environment variables before enabling it. The `.env` file is ignored by Git:

```dotenv
AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT=https://your-resource.cognitiveservices.azure.com/
AZURE_DOCUMENT_INTELLIGENCE_KEY=<secret-key>
```

The key is intentionally not stored in the repository. Rotate any key that has been exposed outside your secret store. When enabled, the gateway uses the Azure `prebuilt-layout` model for OCR, lines, and document structure, then uses the result for reflowable content and Read Aloud text.

## Workspace

Open `pdf-epub-platform.code-workspace` in VS Code to load the solution folders together.
