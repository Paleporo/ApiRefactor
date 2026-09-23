import pytest

from app.cli import main
from app.errors import EmptySpecError, SpecParseError
from app.loader import load_spec
from app.model.document import SpecDocument, SpecVersion
from app.model.refs import RefIndex
from app.validators.openapi_validator import OpenAPIValidator


def test_broken_yaml_reports_line_and_column(tmp_path):
    spec = tmp_path / "broken.yaml"
    spec.write_text("openapi: 3.0.3\ninfo:\n  title: x\n  version: [1.0\npaths: {}\n")
    with pytest.raises(SpecParseError) as exc:
        load_spec(spec)
    assert exc.value.line is not None and exc.value.column is not None
    assert "broken.yaml" in str(exc.value) and "riga" in str(exc.value)


def test_broken_json_reports_line_and_column(tmp_path):
    spec = tmp_path / "broken.json"
    spec.write_text('{\n  "openapi": "3.0.3",\n  "paths": {,}\n}')
    with pytest.raises(SpecParseError) as exc:
        load_spec(spec)
    assert (exc.value.line, exc.value.column) == (3, 13)


@pytest.mark.parametrize("content", ["", "openapi: 3.0.3\ninfo: {title: x, version: 1.0.0}\npaths: {}\n",
                                     "openapi: 3.0.3\ninfo: {title: x, version: 1.0.0}\n"])
def test_empty_spec_or_no_paths_exits_cleanly(tmp_path, content):
    spec = tmp_path / "empty.yaml"
    spec.write_text(content)
    with pytest.raises(EmptySpecError, match="nessun path da rifattorizzare"):
        load_spec(spec)


def test_cli_parse_error_is_clean_not_a_traceback(tmp_path, capsys):
    spec = tmp_path / "broken.yaml"
    spec.write_text("openapi: 3.0.3\npaths:\n  /a: [\n")
    code = main(["refactor", "--input", str(spec), "--output", str(tmp_path / "out")])
    out = capsys.readouterr().out
    assert code == 3
    assert "Impossibile parsare la specifica" in out and "Traceback" not in out


def test_cli_empty_spec_exit_zero_with_message(tmp_path, capsys):
    spec = tmp_path / "empty.yaml"
    spec.write_text("openapi: 3.0.3\ninfo: {title: x, version: 1.0.0}\npaths: {}\n")
    assert main(["refactor", "--input", str(spec), "--output", str(tmp_path / "out")]) == 0
    assert "nessun path da rifattorizzare" in capsys.readouterr().out
    assert not (tmp_path / "out").exists()  # nessuna iterazione avviata, nessun output


def test_yaml_status_codes_and_dates_stay_strings(tmp_path):
    spec = tmp_path / "s.yaml"
    spec.write_text("openapi: 3.0.3\ninfo: {title: x, version: 1.0.0}\npaths:\n  /a:\n    get:\n"
                    "      responses:\n        200: {description: ok}\n      x-since: 2024-01-01\n")
    doc = load_spec(spec)
    op = doc.data["paths"]["/a"]["get"]
    assert "200" in op["responses"] and op["x-since"] == "2024-01-01"


CIRCULAR = {
    "openapi": "3.0.3", "info": {"title": "x", "version": "1.0.0"},
    "paths": {"/users": {"get": {"responses": {"200": {"description": "ok", "content": {"application/json": {
        "schema": {"$ref": "#/components/schemas/User"}}}}}}}},
    "components": {"schemas": {
        "User": {"type": "object", "properties": {"manager": {"$ref": "#/components/schemas/Manager"}}},
        "Manager": {"type": "object", "properties": {"reports": {"type": "array",
                                                                  "items": {"$ref": "#/components/schemas/User"}}}},
    }},
}


def test_circular_refs_are_detected_and_resolution_stops():
    refs = RefIndex(CIRCULAR)
    assert len(refs.cycles) == 1 and set(refs.cycles[0]) == {"/components/schemas/User", "/components/schemas/Manager"}
    defs, truncated = refs.closure("/paths/~1users/get", max_depth=5)
    assert set(defs) == {"/components/schemas/User", "/components/schemas/Manager"}
    assert refs.usage_contexts()["/components/schemas/Manager"] == {"response"}


def test_validator_reports_circular_as_info_and_broken_ref_without_crashing():
    data = {**CIRCULAR, "paths": {**CIRCULAR["paths"], "/x": {"get": {"responses": {"200": {
        "description": "ok", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Nope"}}}}}}}}}
    doc = SpecDocument(source_file="t.yaml", source_version=SpecVersion.OPENAPI_3_0,
                       target_version=SpecVersion.OPENAPI_3_0, data=data)
    violations = OpenAPIValidator().validate(doc)
    ids = {v.rule_id: v for v in violations}
    assert ids["OAS-REF-BROKEN"].severity == "ERROR"
    assert ids["OAS-REF-BROKEN"].path == "/paths/~1x/get/responses/200/content/application~1json/schema"
    assert ids["OAS-REF-CIRCULAR"].severity == "INFO"
