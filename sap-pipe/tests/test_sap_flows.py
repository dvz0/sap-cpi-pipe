import base64
import io
import json
import shutil
import zipfile
from pathlib import Path

import responses
from click.testing import CliRunner

from sap_pipe.cli import cli

TMN = "https://tenant.example:443"
AUTH = "https://auth.example:443"
AUTH_ARGS = [
    "--tmn-host",
    "tenant.example",
    "--oauth-host",
    "auth.example",
    "--oauth-clientid",
    "id",
    "--oauth-clientsecret",
    "secret",
]


def _token() -> None:
    responses.add(
        responses.POST,
        f"{AUTH}/oauth/token",
        json={"access_token": "tok", "expires_in": 3600},
        status=200,
    )


def _design(version: str = "1.0.1") -> None:
    responses.add(
        responses.GET,
        f"{TMN}/api/v1/IntegrationDesigntimeArtifacts(Id='Demo',Version='active')",
        json={"d": {"Version": version, "Description": ""}},
        status=200,
    )


def _runtime(status: str, version: str = "") -> None:
    responses.add(
        responses.GET,
        f"{TMN}/api/v1/IntegrationRuntimeArtifacts('Demo')",
        json={"d": {"Status": status, "Version": version}},
        status=200,
    )


def _runtime_missing() -> None:
    responses.add(
        responses.GET,
        f"{TMN}/api/v1/IntegrationRuntimeArtifacts('Demo')",
        body=b"Requested entity could not be found",
        status=404,
    )


def _deploy_post() -> None:
    responses.add(
        responses.POST,
        f"{TMN}/api/v1/DeployIntegrationDesigntimeArtifact?Id='Demo'&Version='active'",
        status=202,
    )


def _invoke(args: list[str], monkeypatch):
    monkeypatch.setattr("sap_pipe.cli.time.sleep", lambda _delay: None)
    for name in (
        "SAP_PIPE_TMN_HOST",
        "SAP_PIPE_OAUTH_HOST",
        "SAP_PIPE_OAUTH_CLIENTID",
        "SAP_PIPE_OAUTH_CLIENTSECRET",
        "SAP_PIPE_ENVIRONMENT",
        "CI_ENVIRONMENT_NAME",
    ):
        monkeypatch.delenv(name, raising=False)
    return CliRunner().invoke(cli, args)


@responses.activate
def test_deploy_succeeds_only_after_started(monkeypatch):
    _token()
    _design()
    _runtime("STARTING")
    _deploy_post()
    _runtime("STARTING")
    _runtime("STARTED", "1.0.1")
    result = _invoke(
        ["deploy", "--artifact-ids", "Demo", "--delay-length", "0", "--max-check-limit", "3", *AUTH_ARGS],
        monkeypatch,
    )
    assert result.exit_code == 0, result.output
    assert "deployed successfully" in result.output


@responses.activate
def test_deploy_does_not_treat_not_deployed_as_success(monkeypatch):
    _token()
    _design()
    _runtime_missing()
    _deploy_post()
    _runtime_missing()
    result = _invoke(
        ["deploy", "--artifact-ids", "Demo", "--delay-length", "0", "--max-check-limit", "1", *AUTH_ARGS],
        monkeypatch,
    )
    assert result.exit_code != 0
    assert "did not reach STARTED" in result.output


@responses.activate
def test_deploy_fails_on_error_status(monkeypatch):
    _token()
    _design()
    _runtime("ERROR")
    _deploy_post()
    _runtime("ERROR")
    responses.add(
        responses.GET,
        f"{TMN}/api/v1/IntegrationRuntimeArtifacts('Demo')/ErrorInformation/$value",
        json={"parameter": ["adapter failed"]},
        status=200,
    )
    result = _invoke(
        ["deploy", "--artifact-ids", "Demo", "--delay-length", "0", "--max-check-limit", "1", *AUTH_ARGS],
        monkeypatch,
    )
    assert result.exit_code != 0
    assert "ERROR" in result.output
    assert "adapter failed" in result.output


def _redeploy(monkeypatch, limit: str):
    return _invoke(
        [
            "deploy",
            "--artifact-ids",
            "Demo",
            "--redeploy-ids",
            "Demo",
            "--delay-length",
            "0",
            "--max-check-limit",
            limit,
            *AUTH_ARGS,
        ],
        monkeypatch,
    )


@responses.activate
def test_deploy_waits_until_undeploy_finishes(monkeypatch):
    _token()
    _design()
    _runtime("STARTED", "1.0.1")
    responses.add(responses.DELETE, f"{TMN}/api/v1/IntegrationRuntimeArtifacts('Demo')", status=202)
    _runtime("STARTED", "1.0.1")
    _runtime("UNDEPLOYING")
    _runtime("STOPPED")
    _runtime_missing()
    _deploy_post()
    _runtime("STARTED", "1.0.1")
    sleeps: list[int] = []
    monkeypatch.setattr("sap_pipe.cli.time.sleep", sleeps.append)
    for name in (
        "SAP_PIPE_TMN_HOST",
        "SAP_PIPE_OAUTH_HOST",
        "SAP_PIPE_OAUTH_CLIENTID",
        "SAP_PIPE_OAUTH_CLIENTSECRET",
        "SAP_PIPE_ENVIRONMENT",
        "CI_ENVIRONMENT_NAME",
    ):
        monkeypatch.delenv(name, raising=False)
    result = CliRunner().invoke(
        cli,
        [
            "deploy",
            "--artifact-ids",
            "Demo",
            "--redeploy-ids",
            "Demo",
            "--delay-length",
            "30",
            "--max-check-limit",
            "5",
            *AUTH_ARGS,
        ],
    )
    assert result.exit_code == 0, result.output
    removed = next(
        index
        for index, call in enumerate(responses.calls)
        if call.request.method == "GET" and call.response.status_code == 404
    )
    deployed = next(
        index
        for index, call in enumerate(responses.calls)
        if call.request.method == "POST" and "DeployIntegration" in call.request.url
    )
    assert removed < deployed
    assert sleeps == [5, 5, 5]
    assert "Skipping runtime deployment" not in result.output


@responses.activate
def test_deploy_does_not_start_while_old_runtime_is_still_started(monkeypatch):
    _token()
    _design()
    _runtime("STARTED", "1.0.1")
    responses.add(responses.DELETE, f"{TMN}/api/v1/IntegrationRuntimeArtifacts('Demo')", status=202)
    _runtime("STARTED", "1.0.1")
    _runtime("STARTED", "1.0.1")
    result = _redeploy(monkeypatch, "2")
    assert result.exit_code != 0
    assert "deploy was not started" in result.output
    assert not any(call.request.method == "POST" and "DeployIntegration" in call.request.url for call in responses.calls)


@responses.activate
def test_http_error_is_a_one_line_cli_error(monkeypatch):
    _token()
    responses.add(
        responses.GET,
        f"{TMN}/api/v1/IntegrationDesigntimeArtifacts(Id='Demo',Version='active')",
        body=b"bad request",
        status=400,
    )
    result = _invoke(["deploy", "--artifact-ids", "Demo", *AUTH_ARGS], monkeypatch)
    assert result.exit_code != 0
    assert "Traceback" not in result.output
    assert "400" in result.output
    assert "bad request" in result.output


@responses.activate
def test_package_list_follows_odata_next_link(monkeypatch):
    monkeypatch.setattr("sap_pipe.httpclient.time.sleep", lambda _delay: None)
    _token()
    responses.add(
        responses.GET,
        f"{TMN}/api/v1/IntegrationPackages",
        json={"d": {"results": [{"Id": "A"}], "__next": f"{TMN}/api/v1/IntegrationPackages?$skiptoken=2"}},
        status=200,
    )
    responses.add(
        responses.GET,
        f"{TMN}/api/v1/IntegrationPackages?$skiptoken=2",
        json={"d": {"results": [{"Id": "B"}]}},
        status=200,
    )
    from sap_pipe.cpi import IntegrationPackage
    from sap_pipe.httpclient import HttpExecuter

    exe = HttpExecuter(host="tenant.example", oauth_host="auth.example", client_id="id", client_secret="secret")
    assert IntegrationPackage(exe).list_ids() == ["A", "B"]


@responses.activate
def test_get_retries_gateway_timeout(monkeypatch):
    monkeypatch.setattr("sap_pipe.httpclient.time.sleep", lambda _delay: None)
    _token()
    responses.add(responses.GET, f"{TMN}/api/v1/IntegrationPackages", status=503)
    responses.add(
        responses.GET,
        f"{TMN}/api/v1/IntegrationPackages",
        json={"d": {"results": [{"Id": "A"}]}},
        status=200,
    )
    from sap_pipe.cpi import IntegrationPackage
    from sap_pipe.httpclient import HttpExecuter

    exe = HttpExecuter(host="tenant.example", oauth_host="auth.example", client_id="id", client_secret="secret")
    assert IntegrationPackage(exe).list_ids() == ["A"]


@responses.activate
def test_sync_missing_package_is_a_clear_error(monkeypatch, tmp_path: Path):
    _token()
    responses.add(
        responses.GET,
        f"{TMN}/api/v1/IntegrationPackages('missing')",
        status=404,
    )
    result = _invoke(
        [
            "sync",
            "--package-id",
            "missing",
            "--dir-git-repo",
            str(tmp_path),
            "--target",
            "git",
            "--git-skip-commit",
            *AUTH_ARGS,
        ],
        monkeypatch,
    )
    assert result.exit_code != 0
    assert "does not exist on the tenant" in result.output
    assert "missing" in result.output


@responses.activate
def test_sync_to_git_writes_package_file(monkeypatch, tmp_path: Path):
    _token()
    responses.add(
        responses.GET,
        f"{TMN}/api/v1/IntegrationPackages('Pkg')",
        json={
            "d": {
                "__metadata": {"uri": "https://tenant.example/api/v1/IntegrationPackages('Pkg')"},
                "Id": "Pkg",
                "Name": "Pkg",
                "Version": "1.0.0",
                "Mode": "EDIT_ALLOWED",
                "CreatedBy": "dev@example.com",
                "PartnerContent": False,
                "IntegrationDesigntimeArtifacts": {"__deferred": {"uri": "https://tenant.example/artifacts"}},
            }
        },
        status=200,
    )
    for artifact_type in (
        "Integration",
        "MessageMapping",
        "ScriptCollection",
        "ValueMapping",
        "DataType",
        "MessageType",
        "FaultMessageType",
    ):
        responses.add(
            responses.GET,
            f"{TMN}/api/v1/IntegrationPackages('Pkg')/{artifact_type}DesigntimeArtifacts",
            json={"d": {"results": []}},
            status=200,
        )
    result = _invoke(
        [
            "sync",
            "--package-id",
            "Pkg",
            "--dir-git-repo",
            str(tmp_path),
            "--target",
            "git",
            "--sync-package-details",
            "--git-skip-commit",
            *AUTH_ARGS,
        ],
        monkeypatch,
    )
    assert result.exit_code == 0, result.output
    written = json.loads((tmp_path / "Pkg.json").read_text(encoding="utf-8"))
    assert written == {"d": {"Id": "Pkg", "Name": "Pkg", "Version": "1.0.0", "Mode": "EDIT_ALLOWED"}}


@responses.activate
def test_sync_to_tenant_creates_missing_package(monkeypatch, tmp_path: Path):
    package = tmp_path / "Pkg"
    package.mkdir()
    (package / "Pkg.json").write_text(
        json.dumps(
            {
                "d": {
                    "__metadata": {"uri": "https://dev.example/api/v1/IntegrationPackages('Pkg')"},
                    "Id": "Pkg",
                    "Name": "Pkg",
                    "Version": "1.0.0",
                    "Mode": "EDIT_ALLOWED",
                    "CreatedBy": "dev@example.com",
                    "PartnerContent": False,
                    "ResourceId": "abc",
                    "IntegrationDesigntimeArtifacts": {"__deferred": {"uri": "https://dev.example/artifacts"}},
                }
            }
        ),
        encoding="utf-8",
    )
    _token()
    responses.add(responses.GET, f"{TMN}/api/v1/IntegrationPackages('Pkg')", status=404)
    responses.add(responses.POST, f"{TMN}/api/v1/IntegrationPackages", status=201)
    result = _invoke(
        [
            "sync",
            "--package-id",
            "Pkg",
            "--dir-git-repo",
            str(tmp_path),
            "--dir-artifacts",
            str(package),
            "--dir-work",
            str(tmp_path / "work"),
            "--target",
            "tenant",
            *AUTH_ARGS,
        ],
        monkeypatch,
    )
    assert result.exit_code == 0, result.output
    created = next(call for call in responses.calls if call.request.method == "POST" and call.request.url.endswith("/IntegrationPackages"))
    assert json.loads(created.request.body) == {"d": {"Id": "Pkg", "Name": "Pkg", "Version": "1.0.0"}}


def _package_exists() -> None:
    responses.add(
        responses.GET,
        f"{TMN}/api/v1/IntegrationPackages('Pkg')",
        json={"d": {"Id": "Pkg", "Name": "Pkg", "Version": "1.0.0", "Mode": "EDIT_ALLOWED"}},
        status=200,
    )


def _flow_dir(root: Path, qas_values: str | None) -> Path:
    package = root / "Pkg"
    artifact = package / "Integration_Test_IFlow"
    shutil.copytree(Path(__file__).parent / "fixtures" / "Integration_Test_IFlow", artifact)
    if qas_values is not None:
        target = artifact / "parameters" / "qas.prop"
        target.parent.mkdir()
        target.write_text(qas_values, encoding="utf-8")
    return package


@responses.activate
def test_sync_to_qas_applies_environment_file_without_changing_the_zip(monkeypatch, tmp_path: Path):
    package = _flow_dir(
        tmp_path,
        "Parameter\\ 1=ValueQas\n"
        "Parameter\\ 2=Value 2 plus ${property.Parameter1}\n"
        "Sender\\ Endpoint=\n"
        "Timer=every minute\n",
    )
    design = (package / "Integration_Test_IFlow" / "src" / "main" / "resources" / "parameters.prop").read_text(
        encoding="utf-8"
    )
    _token()
    _package_exists()
    responses.add(
        responses.GET,
        f"{TMN}/api/v1/IntegrationDesigntimeArtifacts(Id='Integration_Test_IFlow',Version='active')",
        status=404,
    )
    responses.add(responses.POST, f"{TMN}/api/v1/IntegrationDesigntimeArtifacts", status=201)
    responses.add(
        responses.GET,
        f"{TMN}/api/v1/IntegrationDesigntimeArtifacts(Id='Integration_Test_IFlow',Version='active')/Configurations",
        json={
            "d": {
                "results": [
                    {"ParameterKey": "Parameter 1", "ParameterValue": "Value1", "DataType": "xsd:string"},
                    {"ParameterKey": "Timer", "ParameterValue": "daily", "DataType": "custom:schedule"},
                    {"ParameterKey": "Sender Endpoint", "ParameterValue": "/flow", "DataType": "xsd:string"},
                    {"ParameterKey": "OnlyOnTenant", "ParameterValue": "keep", "DataType": "xsd:string"},
                    {
                        "ParameterKey": "Parameter 2",
                        "ParameterValue": "Value 2 plus ${property.Parameter1}",
                        "DataType": "xsd:string",
                    },
                ]
            }
        },
        status=200,
    )
    responses.add(
        responses.PUT,
        f"{TMN}/api/v1/IntegrationDesigntimeArtifacts(Id='Integration_Test_IFlow',Version='active')/$links/Configurations('Parameter%201')",
        status=202,
    )
    responses.add(
        responses.PUT,
        f"{TMN}/api/v1/IntegrationDesigntimeArtifacts(Id='Integration_Test_IFlow',Version='active')/$links/Configurations('Sender%20Endpoint')",
        status=202,
    )
    responses.add(
        responses.GET,
        f"{TMN}/api/v1/IntegrationRuntimeArtifacts('Integration_Test_IFlow')",
        status=404,
    )
    result = _invoke(
        [
            "sync",
            "--package-id",
            "Pkg",
            "--dir-git-repo",
            str(tmp_path),
            "--dir-artifacts",
            str(package),
            "--dir-work",
            str(tmp_path / "work"),
            "--target",
            "tenant",
            "--environment",
            "qas",
            *AUTH_ARGS,
        ],
        monkeypatch,
    )
    assert result.exit_code == 0, result.output
    created = next(call for call in responses.calls if call.request.method == "POST" and call.request.url.endswith("/IntegrationDesigntimeArtifacts"))
    payload = json.loads(created.request.body)
    with zipfile.ZipFile(io.BytesIO(base64.b64decode(payload["ArtifactContent"]))) as archive:
        assert archive.read("src/main/resources/parameters.prop").decode("utf-8") == design
        assert all(not name.startswith("parameters/") for name in archive.namelist())
    puts = [call for call in responses.calls if call.request.method == "PUT"]
    assert [json.loads(call.request.body)["ParameterValue"] for call in puts] == ["ValueQas", ""]
    assert "Timer" not in " ".join(call.request.url for call in puts)
    assert "OnlyOnTenant" not in " ".join(call.request.url for call in puts)
    assert (package / "Integration_Test_IFlow" / "src" / "main" / "resources" / "parameters.prop").read_text(
        encoding="utf-8"
    ) == design


@responses.activate
def test_sync_to_qas_stops_when_environment_file_is_missing(monkeypatch, tmp_path: Path):
    package = _flow_dir(tmp_path, None)
    _token()
    _package_exists()
    result = _invoke(
        [
            "sync",
            "--package-id",
            "Pkg",
            "--dir-git-repo",
            str(tmp_path),
            "--dir-artifacts",
            str(package),
            "--dir-work",
            str(tmp_path / "work"),
            "--target",
            "tenant",
            "--environment",
            "qas",
            *AUTH_ARGS,
        ],
        monkeypatch,
    )
    assert result.exit_code != 0
    assert "parameters/qas.prop is missing" in result.output
    assert not any(call.request.method == "POST" and "DesigntimeArtifacts" in call.request.url for call in responses.calls)


@responses.activate
def test_sync_reads_environment_from_ci_variable(monkeypatch, tmp_path: Path):
    package = _flow_dir(tmp_path, None)
    monkeypatch.setenv("CI_ENVIRONMENT_NAME", "prod")
    _token()
    _package_exists()
    monkeypatch.setattr("sap_pipe.cli.time.sleep", lambda _delay: None)
    for name in (
        "SAP_PIPE_TMN_HOST",
        "SAP_PIPE_OAUTH_HOST",
        "SAP_PIPE_OAUTH_CLIENTID",
        "SAP_PIPE_OAUTH_CLIENTSECRET",
        "SAP_PIPE_ENVIRONMENT",
    ):
        monkeypatch.delenv(name, raising=False)
    result = CliRunner().invoke(
        cli,
        [
            "sync",
            "--package-id",
            "Pkg",
            "--dir-git-repo",
            str(tmp_path),
            "--dir-artifacts",
            str(package),
            "--dir-work",
            str(tmp_path / "work"),
            "--target",
            "tenant",
            *AUTH_ARGS,
        ],
    )
    assert result.exit_code != 0
    assert "parameters/prod.prop is missing" in result.output
