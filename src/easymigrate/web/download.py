"""The download zip: generated Terraform (local state), the report, and a README."""

from __future__ import annotations

import io
import zipfile
from importlib import resources

from easymigrate.core.pipeline import GenerateSummary
from easymigrate.core.project import Project


def _readme(summary: GenerateSummary) -> str:
    template = resources.files("easymigrate.web").joinpath("README_download.md").read_text("utf-8")
    if summary.password_variables:
        password_vars = "\n".join(
            f"export TF_VAR_{name}='choose-a-strong-password'"
            for name in summary.password_variables
        )
    else:
        password_vars = "# none: this migration has no databases"
    return (
        template.replace("{gcp_project}", summary.gcp_project)
        .replace("{gcp_region}", summary.gcp_region)
        .replace("{password_vars}", password_vars)
    )


def build_zip(project: Project, summary: GenerateSummary) -> bytes:
    """`backend.tf` is left out on purpose: without it Terraform keeps its state in a local
    file, so `tofu init && tofu apply` works with nothing set up first."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        terraform_dir = project.terraform_dir
        for path in sorted(terraform_dir.glob("*.tf")):
            if path.name != "backend.tf":
                archive.write(path, f"terraform/{path.name}")
        tfvars = terraform_dir / "terraform.tfvars"
        if tfvars.exists():
            archive.write(tfvars, "terraform/terraform.tfvars")
        for path, name in (
            (project.report_md_path, "report.md"),
            (project.report_json_path, "report.json"),
        ):
            if path.exists():
                archive.write(path, name)
        archive.writestr("README.md", _readme(summary))
    return buffer.getvalue()
