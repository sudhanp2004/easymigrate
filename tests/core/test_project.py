import json
import stat

import pytest
from pydantic import ValidationError

from easymigrate import __version__
from easymigrate.core.project import (
    DIR_NAME,
    Project,
    ProjectConfig,
    SourceConfig,
    StageInputMissing,
    TargetConfig,
)
from easymigrate.ir import IRDocument, SourceRef
from easymigrate.ir.network import Network, Subnet


def make_ir() -> IRDocument:
    return IRDocument(
        source={"cloud": "aws", "account_id": "123456789012", "region": "ap-south-1"},
        objects=[
            Network(
                id="network:main",
                name="main",
                source=SourceRef(cloud="aws", type="vpc", id="vpc-1", region="ap-south-1"),
                cidr_blocks=["10.0.0.0/16"],
                dns_hostnames=True,
                dns_resolution=True,
            )
        ],
    )


def test_paths_live_under_dot_easymigrate(tmp_path):
    p = Project(tmp_path)

    assert p.dir == tmp_path.resolve() / DIR_NAME
    assert p.ir_path == p.dir / "ir.json"
    assert p.decisions_path == p.dir / "decisions.yaml"
    assert p.snapshot_dir("aws") == p.dir / "snapshot" / "aws"
    assert p.terraform_dir == p.dir / "terraform"
    assert p.plan_lock_path == p.dir / "plan.lock.json"


def test_init_creates_directory_and_config_once(tmp_path):
    p = Project(tmp_path)

    assert p.init() is True
    assert p.init() is False
    assert p.load_config() == ProjectConfig(tool_version=__version__)


def test_config_round_trip(tmp_path):
    p = Project(tmp_path)
    p.init()
    config = ProjectConfig(
        tool_version=__version__,
        source=SourceConfig(cloud="aws", account_id="123456789012", region="ap-south-1"),
        target=TargetConfig(cloud="gcp", project="my-proj", region="asia-south1"),
    )

    p.save_config(config)

    assert Project(tmp_path).load_config() == config


def test_missing_config_names_the_command(tmp_path):
    with pytest.raises(StageInputMissing) as info:
        Project(tmp_path).load_config()

    assert str(info.value) == "project.yaml not found. Run `easymigrate scan` first."


def test_ir_round_trip(tmp_path):
    p = Project(tmp_path)
    p.init()

    p.write_ir(p.ir_path, make_ir())

    assert p.read_ir(p.ir_path, produced_by="analyze").model_dump() == make_ir().model_dump()


def test_write_ir_revalidates_before_writing(tmp_path):
    p = Project(tmp_path)
    p.init()
    doc = make_ir()
    broken = doc.model_copy(
        update={
            "objects": [
                *doc.objects,
                Subnet(
                    id="subnet:orphan",
                    name="orphan",
                    source=SourceRef(
                        cloud="aws", type="subnet", id="subnet-1", region="ap-south-1"
                    ),
                    network="network:missing",
                    cidr="10.0.1.0/24",
                    zone="ap-south-1a",
                    internet="public",
                    map_public_ip=True,
                ),
            ]
        }
    )

    with pytest.raises(ValidationError):
        p.write_ir(p.ir_path, broken)

    assert not p.ir_path.exists()


def test_read_missing_artifact_names_the_command(tmp_path):
    p = Project(tmp_path)

    with pytest.raises(StageInputMissing) as info:
        p.read_ir(p.ir_path, produced_by="analyze")

    assert str(info.value) == "ir.json not found. Run `easymigrate analyze` first."
    assert info.value.command == "analyze"


def test_write_json_dict_is_sorted_with_trailing_newline(tmp_path):
    p = Project(tmp_path)
    target = p.snapshot_dir("aws") / "vpcs.json"

    p.write_json(target, {"b": 1, "a": [1, 2]})

    assert target.read_text() == '{\n  "a": [\n    1,\n    2\n  ],\n  "b": 1\n}\n'
    assert p.read_json(target, produced_by="scan") == {"a": [1, 2], "b": 1}


def test_writes_are_atomic_and_private(tmp_path):
    p = Project(tmp_path)
    p.init()
    p.write_json(p.differences_path, [])

    assert not list(p.dir.glob("*.tmp"))
    assert stat.S_IMODE(p.differences_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(p.config_path.stat().st_mode) == 0o600


def test_write_text_is_atomic_and_private(tmp_path):
    p = Project(tmp_path)
    p.init()
    target = p.decisions_path

    p.write_text(target, "schema_version: '1'\n")

    assert target.read_text() == "schema_version: '1'\n"
    assert not list(p.dir.glob("*.tmp"))
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_private_directories_inside_dot_easymigrate(tmp_path):
    (tmp_path / ".git").mkdir()
    git_root_mode_before = stat.S_IMODE(tmp_path.stat().st_mode)
    p = Project(tmp_path)

    p.init()
    p.write_json(p.snapshot_dir("aws") / "x.json", {})

    assert stat.S_IMODE(p.dir.stat().st_mode) == 0o700
    assert stat.S_IMODE((p.dir / "snapshot").stat().st_mode) == 0o700
    assert stat.S_IMODE(p.snapshot_dir("aws").stat().st_mode) == 0o700
    assert stat.S_IMODE(tmp_path.stat().st_mode) == git_root_mode_before


def test_init_adds_gitignore_entry_in_git_repo(tmp_path):
    (tmp_path / ".git").mkdir()
    project_root = tmp_path / "work"
    project_root.mkdir()
    p = Project(project_root)

    p.init()
    p.init()

    gitignore = tmp_path / ".gitignore"
    assert gitignore.read_text().splitlines().count("/work/.easymigrate/") == 1
    assert stat.S_IMODE(gitignore.stat().st_mode) == 0o644


def test_existing_generic_entry_is_respected(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".gitignore").write_text("__pycache__/\n.easymigrate/")

    assert Project(tmp_path).ensure_gitignored() is False
    assert (tmp_path / ".gitignore").read_text() == "__pycache__/\n.easymigrate/"


def test_appends_to_gitignore_without_trailing_newline(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".gitignore").write_text("__pycache__/")

    assert Project(tmp_path).ensure_gitignored() is True
    assert (tmp_path / ".gitignore").read_text() == "__pycache__/\n/.easymigrate/\n"


def test_no_gitignore_outside_git(tmp_path):
    Project(tmp_path).init()

    assert not (tmp_path / ".gitignore").exists()


def test_write_json_accepts_models(tmp_path):
    p = Project(tmp_path)

    p.write_json(p.report_json_path, make_ir())

    assert json.loads(p.report_json_path.read_text())["objects"][0]["id"] == "network:main"
