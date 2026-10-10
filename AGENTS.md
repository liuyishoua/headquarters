# Headquarters

Independent business control plane. Own objectives, stages, resource allocation,
business outcomes and desired deployments. Channels own events, model execution
and action delivery; workbench connectors alone own platform side effects.

- Keep the SQLite ledger and durable desired state independent of Channel.
- Never treat a model proposal as a completed business outcome. Count platform
  delivery only from authenticated Channel receipts; business outcomes need
  human confirmation and evidence tied to the same mission/stage.
- Cross-platform identity must be explicitly recorded, never inferred by nickname.
- Credentials live in ignored `.local/`, never return them to the browser.
- Source credentials and browser sessions cannot access Channel control APIs.
- All acceptance uses temporary stores and synthetic accounts. Do not activate
  real platform connectors or send real messages while testing.
- Run `.venv/bin/python -m unittest discover -s tests -v`, Channel regression,
  JavaScript syntax checks and isolated desktop/mobile browser acceptance.
