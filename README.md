# Tussilago

A developer-focused PaaS where applications run in isolated Firecracker microVMs.

This repo contains the following:

- `dashboard/`
  - The main [tussilago.dev](https://tussilago.dev) site. This is where users interact with the Tussilago platform. The dashboard talks to the control plane, and the control plane talks to Igniter.
  - Uses [Django](https://www.djangoproject.com/).
  - License is EUPL-1.2.
- `control-plane/`
  - Handles communication between the dashboard and igniters, and keeps track of applications. Uses WebSockets to communicate with the igniter service.
  - Uses [Litestar](https://litestar.dev/).
  - License is EUPL-1.2.
- `igniter/`
  - Runs on hosts that serve microVMs. Responsible for starting, stopping, and monitoring microVMs.
  - Uses [Litestar](https://litestar.dev/).
  - License is EUPL-1.2.
- `cli/`
  - Command-line interface for interacting with the Tussilago platform.
  - License is MIT.
- `agent/`
  - Runs inside the Firecracker microVMs and handles tasks requested by Tussilago.
  - License is MIT.

`infrastructure`, `docker`, `docs` are licensed under CC0.

## Contributing

Please refer to [CONTRIBUTING.md](CONTRIBUTING.md) if you want to contribute to the Tussilago project.

## Security

Please report any security vulnerabilities to the following email address:

[security@tussilago.dev](mailto:security@tussilago.dev)

Or via Discord: `TheLovinator#9276` (ID: 126462229892694018)

## LLM Policy

AI-generated code is not accepted.
