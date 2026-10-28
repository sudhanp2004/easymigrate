"""The download: freshly generated Terraform + report in a zip."""

from __future__ import annotations

import io
import zipfile

from easymigrate.core.project import SourceConfig
from tests.core.test_pipeline_plan_apply import _project_ready_to_plan
from tests.web.helpers import H, make_client, start


def _ready(client, app):
    start(client)
    rec = next(iter(app.state.manager.store._records.values()))
    project = _project_ready_to_plan(app.state.manager.settings.data_dir / rec.id)
    return rec, project


def _download(client, **body):
    return client.post("/api/download", headers=H, json=body)


def _zip(response) -> dict[str, str]:
    archive = zipfile.ZipFile(io.BytesIO(response.content))
    return {name: archive.read(name).decode() for name in archive.namelist()}


def test_download_zip_contents(settings, monkeypatch):
    client, app = make_client(settings)
    _ready(client, app)
    from easymigrate.core import registry

    monkeypatch.setattr(
        registry, "database_password_variables", lambda doc: {"database_instance:db": "db_pw"}
    )

    response = _download(client, gcp_project="my-proj-1")

    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/zip"
    assert response.headers["content-disposition"] == (
        'attachment; filename="easymigrate-123456789012-ap-south-1.zip"'
    )
    files = _zip(response)
    assert "terraform/providers.tf" in files
    assert "terraform/terraform.tfvars" in files
    assert "terraform/backend.tf" not in files
    assert {"report.md", "report.json", "README.md"} <= set(files)
    assert 'project = "my-proj-1"' in files["terraform/terraform.tfvars"]
    assert "export TF_VAR_db_pw=" in files["README.md"]
    assert "my-proj-1" in files["README.md"]


def test_download_defaults_gcp_project(settings):
    client, app = make_client(settings)
    _ready(client, app)
    files = _zip(_download(client))
    assert 'project = "my-gcp-project"' in files["terraform/terraform.tfvars"]


def test_download_reflects_changed_decisions(settings):
    client, app = make_client(settings)
    _ready(client, app)
    first = _zip(_download(client))

    put = client.put(
        "/api/decisions/NET-ZONE-REGIONAL@network:main",
        headers=H,
        json={"strategy": "merge_per_network"},
    )
    assert put.status_code == 200, put.text
    second = _zip(_download(client))

    tf_first = {k: v for k, v in first.items() if k.startswith("terraform/")}
    tf_second = {k: v for k, v in second.items() if k.startswith("terraform/")}
    assert tf_first != tf_second


def test_download_unmapped_region_needs_gcp_region(settings):
    client, app = make_client(settings)
    _, project = _ready(client, app)
    config = project.load_config()
    project.save_config(
        config.model_copy(
            update={
                "source": SourceConfig(cloud="aws", account_id="123456789012", region="xx-test-1")
            }
        )
    )

    response = _download(client)
    assert response.status_code == 400
    assert "Pick a Google Cloud region" in response.json()["detail"]

    assert _download(client, gcp_region="europe-west1").status_code == 200


def test_download_rejects_bad_gcp_project(settings):
    client, app = make_client(settings)
    _ready(client, app)
    response = _download(client, gcp_project="Bad_Project!")
    assert response.status_code == 400
    assert "Google Cloud project ID" in response.json()["detail"]


def test_download_not_ready_is_400(settings):
    client, _ = make_client(settings)
    start(client)
    assert _download(client).status_code == 400


def test_report_markdown(settings):
    client, app = make_client(settings)
    _, project = _ready(client, app)
    response = client.get("/api/report-markdown")
    assert response.status_code == 200
    from easymigrate.core.report import build_report

    assert response.json()["markdown"] == build_report(project).to_markdown()


def test_report_markdown_before_any_report(settings):
    client, _ = make_client(settings)
    start(client)
    response = client.get("/api/report-markdown")
    assert response.status_code == 400
    assert "No report yet" in response.json()["detail"]
