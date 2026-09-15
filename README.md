# EasyMigrate

Migrates cloud infrastructure from AWS to GCP. It scans the live source cloud, builds an intent-level intermediate representation, shows every difference between the clouds with safe default handling, generates Terraform, and applies it only after you approve.

## Development

    uv sync
    uv run pytest
    uv run ruff check . && uv run ruff format --check .

Design: `docs/superpowers/specs/2026-09-15-core-pipeline-networking-design.md`
