import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from sap_pipe.bpmn import update_bpmn
from sap_pipe.cli import cli
from sap_pipe.cpi import package_body, update_body
from sap_pipe.sync import configuration_file
from sap_pipe.files import diff_directories, diff_file, unzip_source, zip_dir
from sap_pipe.textutil import (
    artifact_id_from_symbolic_name,
    header_get,
    parse_manifest,
    parse_properties,
    trim_manifest_field,
)

FIXTURE = Path(__file__).parent / "fixtures" / "Integration_Test_IFlow"


def test_properties_unescape_spaces():
    parsed = parse_properties(Path(FIXTURE, "src/main/resources/parameters.prop").read_text(encoding="utf-8"))
    assert parsed["Parameter 1"] == "Value1"
    assert parsed["Parameter 2"] == "Value 2 plus ${property.Parameter1}"
    assert parsed["Sender Endpoint"] == "/flow"


def test_configuration_file_selects_environment_override(tmp_path: Path):
    artifact = tmp_path / "flow"
    design = artifact / "src" / "main" / "resources" / "parameters.prop"
    design.parent.mkdir(parents=True)
    design.write_text("Sender\\ Endpoint=/dev\n", encoding="utf-8")
    selected = configuration_file(str(artifact), "flow", "dev")
    assert selected == str(design)
    override = artifact / "parameters" / "qas.prop"
    override.parent.mkdir()
    override.write_text("Sender\\ Endpoint=/qas\n", encoding="utf-8")
    assert configuration_file(str(artifact), "flow", "qas") == str(override)


def test_configuration_file_requires_override_when_parameters_exist(tmp_path: Path):
    artifact = tmp_path / "flow"
    design = artifact / "src" / "main" / "resources" / "parameters.prop"
    design.parent.mkdir(parents=True)
    design.write_text("# no keys\n", encoding="utf-8")
    assert configuration_file(str(artifact), "flow", "prod") == str(design)
    design.write_text("Sender\\ Endpoint=/dev\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match=r"parameters/prod\.prop is missing"):
        configuration_file(str(artifact), "flow", "prod")
    with pytest.raises(RuntimeError, match="Invalid environment"):
        configuration_file(str(artifact), "flow", "../prod")


def test_manifest_continuation_and_symbolic_name():
    headers = parse_manifest(Path(FIXTURE, "META-INF/MANIFEST.MF").read_text(encoding="utf-8"))
    assert header_get(headers, "SAP-BundleType") == "IntegrationFlow"
    assert "com.sap.esb.security" in header_get(headers, "Import-Package")
    assert artifact_id_from_symbolic_name(header_get(headers, "Bundle-SymbolicName")) == "Integration_Test_IFlow"


def test_manifest_wrapped_name_drops_continuation_space():
    wrapped = "ALVO 1308-S Microsoft SharePoint Download Drive Item Conten t"
    assert "Conten t" not in trim_manifest_field(wrapped)
    assert trim_manifest_field(wrapped).endswith("Content")


def test_update_body_omits_identity_on_put():
    created = json.loads(update_body("POST", "Flow", "Flow name", "Pkg", "e30=", "desc"))
    assert created["Id"] == "Flow"
    assert created["PackageId"] == "Pkg"
    updated = json.loads(update_body("PUT", "Flow", "Flow name", "Pkg", "e30=", ""))
    assert list(updated) == ["ArtifactContent"]


def test_package_body_keeps_only_writable_fields():
    payload = {
        "d": {
            "__metadata": {"uri": "https://dev.example/api/v1/IntegrationPackages('Pkg')"},
            "Id": "Pkg",
            "Name": "Pkg",
            "ResourceId": "abc",
            "Description": "hello",
            "ShortText": "Pkg",
            "Version": "1.0.0",
            "Vendor": "",
            "PartnerContent": False,
            "UpdateAvailable": False,
            "Mode": "EDIT_ALLOWED",
            "SupportedPlatform": "Cloud Integration",
            "ModifiedBy": "dev@example.com",
            "CreationDate": "1795204140587",
            "ModifiedDate": "1795204164308",
            "CreatedBy": "dev@example.com",
            "PackageContent": None,
            "IntegrationDesigntimeArtifacts": {"__deferred": {"uri": "https://dev.example/artifacts"}},
        }
    }
    body = json.loads(package_body(payload))
    assert body["d"] == {
        "Id": "Pkg",
        "Name": "Pkg",
        "Description": "hello",
        "ShortText": "Pkg",
        "Version": "1.0.0",
        "SupportedPlatform": "Cloud Integration",
    }


def test_zip_roundtrip(tmp_path: Path):
    source = tmp_path / "artifact"
    (source / "META-INF").mkdir(parents=True)
    (source / "META-INF" / "MANIFEST.MF").write_text("Bundle-Name: Demo\n", encoding="utf-8")
    archive = tmp_path / "artifact.zip"
    zip_dir(str(source), str(archive), include_src=False)
    target = tmp_path / "out"
    unzip_source(str(archive), str(target))
    assert (target / "META-INF" / "MANIFEST.MF").read_text(encoding="utf-8") == "Bundle-Name: Demo\n"


def test_bpmn_rewrites_script_bundle_id(tmp_path: Path):
    flow = tmp_path / "src" / "main" / "resources" / "scenarioflows" / "integrationflow"
    flow.mkdir(parents=True)
    xml = flow / "demo.iflw"
    xml.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<bpmn2:definitions xmlns:bpmn2="http://www.omg.org/spec/BPMN/20100524/MODEL" xmlns:ifl="http:///com.sap.ifl.model/Ifl.xsd">
  <bpmn2:process>
    <ifl:property>
      <key>scriptBundleId</key>
      <value>CollectionDev</value>
    </ifl:property>
  </bpmn2:process>
</bpmn2:definitions>
""",
        encoding="utf-8",
    )
    update_bpmn(str(tmp_path), ["CollectionDev=CollectionQas"])
    text = xml.read_text(encoding="utf-8")
    assert "CollectionQas" in text
    assert "CollectionDev" not in text
    assert "xmlns:bpmn2=" in text
    assert "xmlns:ifl=" in text
    assert "ns0:" not in text


def test_diff_keeps_spaces_and_limits_manifest_ignore(tmp_path: Path):
    left = tmp_path / "left"
    right = tmp_path / "right"
    for root in (left, right):
        (root / "src").mkdir(parents=True)
        (root / "META-INF").mkdir()
    (left / "src" / "script.groovy").write_text("def x = 1\n", encoding="utf-8")
    (right / "src" / "script.groovy").write_text("def x=1\n", encoding="utf-8")
    (left / "META-INF" / "MANIFEST.MF").write_text("Bundle-Name: A\nOrigin-Bundle-Name: A\n", encoding="utf-8")
    (right / "META-INF" / "MANIFEST.MF").write_text("Bundle-Name: A\nOrigin-Bundle-Name: B\n", encoding="utf-8")
    assert diff_directories(str(left), str(right)) is True
    (right / "src" / "script.groovy").write_text("def x = 1\n", encoding="utf-8")
    assert diff_directories(str(left), str(right)) is False
    xml_left = tmp_path / "a.xml"
    xml_right = tmp_path / "b.xml"
    xml_left.write_text("<tag>#keep</tag>\n", encoding="utf-8")
    xml_right.write_text("<tag>other</tag>\n", encoding="utf-8")
    assert diff_file(str(xml_left), str(xml_right)) is True


def test_yaml_config_supplies_optional_flags(monkeypatch, tmp_path: Path):
    for name in (
        "SAP_PIPE_TMN_HOST",
        "SAP_PIPE_OAUTH_HOST",
        "SAP_PIPE_OAUTH_CLIENTID",
        "SAP_PIPE_OAUTH_CLIENTSECRET",
        "SAP_PIPE_PACKAGE_ID",
    ):
        monkeypatch.delenv(name, raising=False)
    config = tmp_path / "sap-pipe.yaml"
    config.write_text("package-id: Pkg\n", encoding="utf-8")
    result = CliRunner().invoke(
        cli,
        ["sync", "--config", str(config), "--dir-git-repo", str(tmp_path), "--git-skip-commit"],
    )
    assert result.exit_code != 0
    assert 'required flag "package-id" not set' not in result.output
    assert "tmn-host" in result.output


def test_help_uses_sap_pipe_name_only():
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "sap-pipe" in result.output
    for command in ("update", "deploy", "sync", "snapshot"):
        assert command in result.output


def test_subcommand_help():
    runner = CliRunner()
    for args in (
        ["update", "artifact", "--help"],
        ["update", "package", "--help"],
        ["deploy", "--help"],
        ["sync", "--help"],
        ["sync", "apiproxy", "--help"],
        ["sync", "apiproduct", "--help"],
        ["snapshot", "--help"],
        ["snapshot", "restore", "--help"],
    ):
        result = runner.invoke(cli, args)
        assert result.exit_code == 0, result.output


def test_sync_subcommands_keep_parent_options():
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "sync",
            "apiproxy",
            "--tmn-host",
            "tenant.example",
            "--oauth-host",
            "auth.example",
            "--oauth-clientid",
            "id",
            "--oauth-clientsecret",
            "secret",
        ],
    )
    assert result.exit_code != 0
    assert "dir-git-repo" in result.output


def test_missing_auth_fails_before_calling_sap(monkeypatch):
    monkeypatch.delenv("SAP_PIPE_TMN_HOST", raising=False)
    monkeypatch.delenv("SAP_PIPE_OAUTH_HOST", raising=False)
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["deploy", "--artifact-ids", "Demo", "--tmn-host", "tenant.example"],
    )
    assert result.exit_code != 0
    assert "oauth-host" in result.output or "tmn-userid" in result.output
