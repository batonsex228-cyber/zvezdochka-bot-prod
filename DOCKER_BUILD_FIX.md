# Docker build fix (2026-09-09)

The application version remains 5.7.2. This patch changes only the Docker build pipeline.

Why:
- GitHub Actions already runs the full verified 192-test suite in the `Offline tests` job.
- The Dockerfile repeated the unit suite inside each Buildx target (amd64/arm64) under QEMU and promoted `ResourceWarning` to an exception.
- That duplicate architecture-emulated test run could fail even when the verified test job passed.

Fix:
- Docker build now runs project preflight and Python compilation only.
- Unit/integration tests remain mandatory because the Docker job has `needs: test`.
- `tests/` are not copied into the runtime image.
- `assets/` are copied into the runtime image.
