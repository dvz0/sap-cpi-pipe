import json
import os
import re
import shutil
from pathlib import Path

from sap_pipe import files
from sap_pipe.bpmn import update_bpmn
from sap_pipe.cpi import (
    DesigntimeArtifact,
    IntegrationPackage,
    Runtime,
    find_artifact,
    load_package_file,
    package_for_storage,
    update_configuration,
)
from sap_pipe.httpclient import HttpExecuter
from sap_pipe.log import get_logger
from sap_pipe.textutil import (
    artifact_id_from_symbolic_name,
    bundle_type_to_artifact_type,
    filter_ids,
    header_get,
    parse_manifest,
    parse_properties,
    trim_manifest_field,
)

log = get_logger()


def create_package_if_missing(package_id: str, package_name: str, exe: HttpExecuter) -> None:
    ip = IntegrationPackage(exe)
    _data, _read_only, exists = ip.get(package_id)
    if exists:
        return
    ip.create(
        {
            "d": {
                "Id": package_id,
                "Name": package_name,
                "ShortText": package_id,
                "Version": "1.0.0",
            }
        }
    )
    log.info("Integration package %s created", package_id)


def verify_downloadable(package_id: str, exe: HttpExecuter) -> tuple[dict | None, bool, bool]:
    data, read_only, exists = IntegrationPackage(exe).get(package_id)
    if not exists:
        raise RuntimeError(
            f"Package '{package_id}' does not exist on the tenant. "
            "Use the Technical Name from SAP (not the display Name), and check packages.json."
        )
    if read_only:
        log.warning("Skipping package %s as it is Configure-only and cannot be downloaded", package_id)
    return data, read_only, exists


def package_to_git(package_data: dict, package_id: str, work_dir: str, artifacts_dir: str) -> None:
    staging = os.path.join(work_dir, "from_tenant")
    os.makedirs(staging, exist_ok=True)
    log.info("Storing package details from tenant for comparison")
    stored = package_for_storage(package_data)
    tenant_file = os.path.join(staging, f"{package_id}.json")
    Path(tenant_file).write_text(json.dumps(stored, indent=2), encoding="utf-8")
    git_file = os.path.join(artifacts_dir, f"{package_id}.json")
    if files.exists(git_file) and load_package_file(git_file) == stored:
        log.info("No changes to package %s detected. Update to Git not required", package_id)
    elif files.exists(git_file):
        log.info("Changes to package %s detected and will be updated to Git", package_id)
        files.copy_file(tenant_file, git_file)
    else:
        log.info("Saving new file for package %s to Git", package_id)
        files.copy_file(tenant_file, git_file)
    shutil.rmtree(staging, ignore_errors=True)


def artifacts_to_git(
    package_id: str,
    work_dir: str,
    artifacts_dir: str,
    included: list[str],
    excluded: list[str],
    draft_handling: str,
    dir_naming_type: str,
    script_map: list[str],
    exe: HttpExecuter,
) -> None:
    log.info("Getting artifacts in integration package %s", package_id)
    artifacts = IntegrationPackage(exe).all_artifacts(package_id)
    os.makedirs(os.path.join(work_dir, "download"), exist_ok=True)
    selected = _filter_artifacts(artifacts, included, excluded)
    for artifact in selected:
        log.info("---------------------------------------------------------------------------------")
        log.info("Begin processing for artifact %s", artifact["Id"])
        if artifact["IsDraft"]:
            if draft_handling == "SKIP":
                log.warning("Artifact %s is in draft version, and will be skipped", artifact["Id"])
                continue
            if draft_handling == "ERROR":
                raise RuntimeError(f"Artifact {artifact['Id']} is in draft version. Save Version in Web UI first!")
            log.info("Artifact %s is in draft version, and will be added", artifact["Id"])
        dt = DesigntimeArtifact(artifact["ArtifactType"], exe)
        zip_path = os.path.join(work_dir, "download", f"{artifact['Id']}.zip")
        dt.download(zip_path, artifact["Id"])
        directory_name = artifact["Name"] if dir_naming_type == "NAME" else artifact["Id"]
        downloaded = os.path.join(work_dir, "download", directory_name)
        files.unzip_source(zip_path, downloaded)
        log.info("Downloaded artifact unzipped to %s", downloaded)
        git_path = os.path.join(artifacts_dir, directory_name)
        if files.exists(os.path.join(git_path, "META-INF", "MANIFEST.MF")):
            log.info("Comparing content from tenant against Git")
            differs = dt.compare_content(downloaded, git_path, script_map, "git")
            if differs:
                log.info("Changes detected and will be updated to Git")
                dt.copy_content(downloaded, git_path)
            else:
                log.info("No changes detected. Update to Git not required")
        else:
            log.info("Artifact %s does not exist, and will be added to Git", artifact["Id"])
            if artifact["ArtifactType"] == "Integration":
                update_bpmn(downloaded, script_map)
            files.replace_dir(downloaded, git_path)
    shutil.rmtree(os.path.join(work_dir, "download"), ignore_errors=True)
    log.info("---------------------------------------------------------------------------------")
    log.info("Completed processing of artifacts in integration package %s", package_id)


def artifacts_to_tenant(
    package_id: str,
    work_dir: str,
    artifacts_dir: str,
    included: list[str],
    excluded: list[str],
    exe: HttpExecuter,
    environment: str = "",
    defer_redeploy: list[str] | None = None,
) -> None:
    environment = _normalize_environment(environment)
    if environment:
        log.info("Configured parameters will be read for environment %s", environment)
    found = False
    for entry in os.scandir(artifacts_dir):
        if not entry.is_dir():
            continue
        manifest_path = os.path.join(entry.path, "META-INF", "MANIFEST.MF")
        if not files.exists(manifest_path):
            continue
        found = True
        log.info("---------------------------------------------------------------------------------")
        log.info("Processing directory %s", entry.path)
        headers = parse_manifest(Path(manifest_path).read_text(encoding="utf-8"))
        artifact_id = artifact_id_from_symbolic_name(header_get(headers, "Bundle-SymbolicName"))
        if included and artifact_id not in included:
            log.warning("Skipping artifact %s as it is not in --ids-include", artifact_id)
            continue
        if excluded and artifact_id in excluded:
            log.warning("Skipping artifact %s as it is in --ids-exclude", artifact_id)
            continue
        artifact_name = trim_manifest_field(header_get(headers, "Bundle-Name"))
        artifact_type = bundle_type_to_artifact_type(header_get(headers, "SAP-BundleType"))
        parameters = os.path.join(entry.path, "src", "main", "resources", "parameters.prop")
        if artifact_type == "Integration":
            parameters = configuration_file(entry.path, artifact_id, environment)
        log.info("Begin processing for artifact %s", artifact_id)
        single_artifact_to_tenant(
            artifact_id,
            artifact_name,
            artifact_type,
            package_id,
            entry.path,
            work_dir,
            parameters,
            [],
            exe,
            defer_redeploy,
        )
    if not found:
        log.warning("No directory with artifact contents found in %s", artifacts_dir)


def single_artifact_to_tenant(
    artifact_id: str,
    artifact_name: str,
    artifact_type: str,
    package_id: str,
    artifact_dir: str,
    work_dir: str,
    parameters_file: str,
    script_map: list[str],
    exe: HttpExecuter,
    defer_redeploy: list[str] | None = None,
) -> None:
    dt = DesigntimeArtifact(artifact_type, exe)
    exists = _artifact_exists(artifact_id, artifact_type, package_id, dt, exe)
    if not exists:
        log.info("Artifact %s will be created", artifact_id)
        if artifact_type == "Integration":
            update_bpmn(artifact_dir, script_map)
        _prepare_upload_dir(work_dir, artifact_dir, dt)
        dt.create(artifact_id, artifact_name, package_id, os.path.join(work_dir, "upload"))
        log.info("Designtime artifact created successfully")
        _apply_configuration(artifact_id, artifact_type, parameters_file, exe)
        return
    log.info("Checking if designtime artifact needs to be updated")
    zip_file = os.path.join(work_dir, f"{artifact_id}.zip")
    dt.download(zip_file, artifact_id)
    changes = _compare_artifact(work_dir, zip_file, artifact_dir, script_map, dt)
    if changes:
        log.info("Changes found in designtime artifact. Designtime artifact will be updated in CPI tenant")
        _prepare_upload_dir(work_dir, artifact_dir, dt)
        dt.update(artifact_id, artifact_name, package_id, os.path.join(work_dir, "upload"))
        log.info("Designtime artifact updated successfully")
        if _same_version_running(dt, exe, artifact_id):
            _schedule_redeploy(artifact_id, defer_redeploy, exe, "changes in design")
    else:
        log.info("No changes detected. Designtime artifact does not need to be updated")
    if _apply_configuration(artifact_id, artifact_type, parameters_file, exe) and _same_version_running(dt, exe, artifact_id):
        _schedule_redeploy(artifact_id, defer_redeploy, exe, "changes in configured parameters")


def upsert_package_from_file(package_file: str, exe: HttpExecuter) -> None:
    log.info("Getting package details from %s file", package_file)
    details = load_package_file(package_file)
    ip = IntegrationPackage(exe)
    package_id = details["d"]["Id"]
    _data, _read_only, exists = ip.get(package_id)
    if not exists:
        log.info("Package %s does not exist", package_id)
        ip.create(details)
        log.info("Package %s created", package_id)
    else:
        ip.update(details)
        log.info("Package %s updated", package_id)


def sync_package_to_tenant(package_id: str, artifacts_dir: str, package_file: str, exe: HttpExecuter) -> None:
    _data, _read_only, exists = verify_downloadable_allow_missing(package_id, exe)
    if not exists:
        candidate = package_file or os.path.join(artifacts_dir, f"{package_id}.json")
        if files.exists(candidate):
            upsert_package_from_file(candidate, exe)
        else:
            raise RuntimeError(f"Package {package_id} does not exist. Please run 'update package' command first")


def verify_downloadable_allow_missing(package_id: str, exe: HttpExecuter) -> tuple[dict | None, bool, bool]:
    return IntegrationPackage(exe).get(package_id)


def _filter_artifacts(artifacts: list[dict], included: list[str], excluded: list[str]) -> list[dict]:
    if included:
        selected = []
        for artifact_id in included:
            found = find_artifact(artifact_id, artifacts)
            if found is None:
                raise RuntimeError(f"Artifact {artifact_id} in --ids-include does not exist")
            selected.append(found)
        return selected
    if excluded:
        for artifact_id in excluded:
            if find_artifact(artifact_id, artifacts) is None:
                raise RuntimeError(f"Artifact {artifact_id} in --ids-exclude does not exist")
        return [artifact for artifact in artifacts if artifact["Id"] not in excluded]
    return artifacts


def _artifact_exists(
    artifact_id: str,
    artifact_type: str,
    package_id: str,
    dt: DesigntimeArtifact,
    exe: HttpExecuter,
) -> bool:
    _version, _description, exists = dt.get(artifact_id, "active")
    if not exists:
        log.info("Active version of artifact %s does not exist", artifact_id)
        return False
    log.info("Active version of artifact %s exists", artifact_id)
    details = IntegrationPackage(exe).artifacts(package_id, artifact_type)
    artifact = find_artifact(artifact_id, details)
    if artifact is None:
        raise RuntimeError(f"Artifact {artifact_id} not found in package {package_id}")
    if artifact["IsDraft"]:
        raise RuntimeError(f"Artifact {artifact_id} is in Draft state. Save Version of artifact in Web UI first!")
    return True


def configuration_file(artifact_dir: str, artifact_id: str, environment: str) -> str:
    """Pick the properties file applied through the configuration API.

    The iFlow zip keeps src/main/resources/parameters.prop. Environment values
    live beside it, in parameters/<environment>.prop, and are not uploaded
    inside the zip. QA and production must have that file when the design-time
    file defines any parameter.
    """
    environment = _normalize_environment(environment)
    design = os.path.join(artifact_dir, "src", "main", "resources", "parameters.prop")
    if not environment:
        return design
    override = os.path.join(artifact_dir, "parameters", f"{environment}.prop")
    if files.exists(override):
        log.info("Using %s as configured parameters for artifact %s", override, artifact_id)
        return override
    if environment != "dev" and _has_parameter_keys(design):
        raise RuntimeError(
            f"Artifact {artifact_id} has externalized parameters but parameters/{environment}.prop is missing"
        )
    return design


def _normalize_environment(environment: str) -> str:
    environment = environment.strip()
    if environment and re.fullmatch(r"[A-Za-z0-9_-]+", environment) is None:
        raise RuntimeError(f"Invalid environment name '{environment}'")
    return environment


def _has_parameter_keys(path: str) -> bool:
    if not files.exists(path):
        return False
    return bool(parse_properties(Path(path).read_text(encoding="utf-8")))


def _apply_configuration(artifact_id: str, artifact_type: str, parameters_file: str, exe: HttpExecuter) -> bool:
    if artifact_type != "Integration" or not parameters_file or not files.exists(parameters_file):
        return False
    log.info("Updating configured parameter(s) of Integration designtime artifact where necessary")
    return update_configuration(artifact_id, parameters_file, exe)


def _same_version_running(dt: DesigntimeArtifact, exe: HttpExecuter, artifact_id: str) -> bool:
    version, _description, exists = dt.get(artifact_id, "active")
    if not exists or not version:
        return False
    runtime_version, _status = Runtime(exe).get(artifact_id)
    return runtime_version == version and runtime_version != "NOT_DEPLOYED"


def _schedule_redeploy(artifact_id: str, defer_redeploy: list[str] | None, exe: HttpExecuter, reason: str) -> None:
    if defer_redeploy is not None:
        if artifact_id not in defer_redeploy:
            defer_redeploy.append(artifact_id)
        log.info("Runtime of %s stays up until deploy (%s)", artifact_id, reason)
        return
    log.info("Undeploying existing runtime artifact %s due to %s", artifact_id, reason)
    Runtime(exe).undeploy(artifact_id)


def _prepare_upload_dir(work_dir: str, artifact_dir: str, dt: DesigntimeArtifact) -> None:
    upload_dir = os.path.join(work_dir, "upload")
    shutil.rmtree(upload_dir, ignore_errors=True)
    dt.copy_content(artifact_dir, upload_dir)


def _compare_artifact(
    work_dir: str,
    zip_file: str,
    artifact_dir: str,
    script_map: list[str],
    dt: DesigntimeArtifact,
) -> bool:
    target = os.path.join(work_dir, "download")
    shutil.rmtree(target, ignore_errors=True)
    log.info("Unzipping downloaded designtime artifact %s to %s/download", zip_file, work_dir)
    files.unzip_source(zip_file, target)
    return dt.compare_content(artifact_dir, target, script_map, "tenant")


def filter_skipped(entity_id: str, included: list[str], excluded: list[str]) -> bool:
    skipped = filter_ids(entity_id, included, excluded)
    if skipped and included:
        log.warning("Skipping %s as it is not in --ids-include", entity_id)
    elif skipped and excluded:
        log.warning("Skipping %s as it is in --ids-exclude", entity_id)
    return skipped
