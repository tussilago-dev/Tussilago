# Tussilago

A developer-focused PaaS where applications run in isolated Firecracker microVMs.

This repo contains the following:

- `dashboard/`
  - The main [tussilago.dev](https://tussilago.dev) site. This is where users interact with the Tussilago platform.
  - Uses [Django](https://www.djangoproject.com/).
  - License is EUPL-1.2.
- `igniter/`
  - Orchestrates the creation and management of Firecracker microVMs for the Tussilago platform. Supports starting, stopping, and monitoring microVMs.
  - Uses [Litestar](https://litestar.dev/).
  - License is EUPL-1.2.
- `cli/`
  - Command-line interface for interacting with the Tussilago platform.
  - License is MIT.
- `agent/`
  - Runs inside the Firecracker microVMs to perform tasks on behalf of the Tussilago platform.
  - License is MIT.
- `docs/`
  - Documentation for the Tussilago platform. Refer to [docs.tussilago.dev](https://docs.tussilago.dev) for more information.
  - License is CC0.
- `docker/`
  - Docker-related files and configurations for the Tussilago platform.
  - License is CC0.
- `infrastructure/`
  - Infrastructure-as-Code (IaC) for the Tussilago platform.
  - License is CC0.
