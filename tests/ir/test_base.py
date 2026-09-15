import json
from typing import Literal

import pytest
from pydantic import ValidationError

from easymigrate.ir.base import Finding, IntentObject, IRDocument, SourceRef, intent_class

SRC = {"cloud": "aws", "type": "widget", "id": "w-123", "region": "ap-south-1"}


class Widget(IntentObject):
    kind: Literal["test_widget"] = "test_widget"
    size: int
    parent: str | None = None

    def references(self) -> list[str]:
        return [self.parent] if self.parent else []


def widget(name: str = "a", **fields) -> Widget:
    return Widget(id=f"test_widget:{name}", name=name, source=SourceRef(**SRC), size=1, **fields)


def doc(*objects) -> IRDocument:
    return IRDocument(
        source={"cloud": "aws", "account_id": "123456789012", "region": "ap-south-1"},
        objects=list(objects),
    )


def test_subclass_registers_its_kind():
    assert intent_class("test_widget") is Widget


def test_json_round_trip_keeps_subclass_and_its_fields():
    original = doc(widget("a"), widget("b", parent="test_widget:a"))

    restored = IRDocument.model_validate_json(original.model_dump_json())

    assert isinstance(restored.objects[1], Widget)
    assert restored.objects[1].parent == "test_widget:a"
    assert restored.model_dump() == original.model_dump()


def test_unknown_kind_is_rejected():
    data = json.loads(doc().model_dump_json())
    data["objects"] = [{"kind": "nope", "id": "nope:x", "name": "x", "source": SRC}]

    with pytest.raises(ValidationError, match="unknown intent kind 'nope'"):
        IRDocument.model_validate(data)


def test_invalid_object_inside_document_is_rejected():
    data = json.loads(doc().model_dump_json())
    data["objects"] = [{"kind": "test_widget", "id": "test_widget:x", "name": "x", "source": SRC}]

    with pytest.raises(ValidationError, match="invalid test_widget object 'test_widget:x'"):
        IRDocument.model_validate(data)


def test_id_must_start_with_kind():
    with pytest.raises(ValidationError, match="must start with 'test_widget:'"):
        Widget(id="other:a", name="a", source=SourceRef(**SRC), size=1)


def test_duplicate_ids_are_rejected():
    with pytest.raises(ValidationError, match="duplicate object id: test_widget:a"):
        doc(widget("a"), widget("a"))


def test_dangling_reference_is_rejected():
    with pytest.raises(
        ValidationError, match="test_widget:b references missing object test_widget:zzz"
    ):
        doc(widget("b", parent="test_widget:zzz"))


def test_extra_fields_are_forbidden():
    with pytest.raises(ValidationError):
        Widget(id="test_widget:a", name="a", source=SourceRef(**SRC), size=1, bogus=True)


def test_subclass_must_declare_kind_default():
    with pytest.raises(TypeError, match="must declare a default for 'kind'"):

        class Broken(IntentObject):
            kind: str


def test_two_classes_cannot_share_a_kind():
    with pytest.raises(TypeError, match="intent kind 'test_widget' is already registered"):

        class Impostor(IntentObject):
            kind: Literal["test_widget"] = "test_widget"


def test_get_and_of_kind():
    d = doc(widget("a"))

    assert d.get("test_widget:a").name == "a"
    assert d.of_kind(Widget) == [d.objects[0]]
    with pytest.raises(KeyError):
        d.get("test_widget:missing")


def test_finding_levels_exclude_difference_levels():
    Finding(level="scan_gap", code="SCAN-ACCESS-DENIED", message="m")

    with pytest.raises(ValidationError):
        Finding(level="approximated", code="X", message="m")


def test_resolved_ir_fields_default_to_empty():
    w = widget()

    assert w.target_hints == {}
    assert w.resolution == []
    assert w.excluded is None
