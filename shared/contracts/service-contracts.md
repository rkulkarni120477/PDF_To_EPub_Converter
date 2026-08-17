# Service Contracts

Every microservice exposes:

- `GET /health` for platform health probes
- One or more `/api/v1/...` capability endpoints for domain operations
- A JSON response with `status` and `service` fields for the initial scaffold

The current capability endpoints are intentionally thin boundaries. They are the integration points for request models, persistence, messaging, and Azure service clients as each workflow is implemented.
