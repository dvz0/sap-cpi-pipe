import json
import os
import shutil
from pathlib import Path
from urllib.parse import quote, urlsplit

from sap_pipe import files
from sap_pipe.bpmn import update_bpmn
from sap_pipe.httpclient import ApiError, HttpExecuter
from sap_pipe.log import get_logger
from sap_pipe.textutil import parse_properties

log = get_logger()

ARTIFACT_TYPES = (
    "Integration",
    "MessageMapping",
    "ScriptCollection",
    "ValueMapping",
    "DataType",
    "MessageType",
    "FaultMessageType",
)
NO_DEPLOY = {"DataType", "MessageType", "FaultMessageType"}
DESCRIPTION_ON_CREATE = {"MessageType", "FaultMessageType"}
DESCRIPTION_ON_UPDATE = {"DataType", "MessageType", "FaultMessageType"}


class DesigntimeArtifact:
    def __init__(self, artifact_type: str, exe: HttpExecuter):
        self.artifact_type = artifact_type
        self.exe = exe

    def create(self, artifact_id: str, name: str, package_id: str, artifact_dir: str) -> None:
        description = ""
        if self.artifact_type in DESCRIPTION_ON_CREATE:
            description = description_from_additional_attributes(artifact_dir)
        log.info("Creating %s designtime artifact %s", self.artifact_type, artifact_id)
        path = f"/api/v1/{self.artifact_type}DesigntimeArtifacts"
        self._upsert(artifact_id, name, package_id, description, artifact_dir, "POST", path, 201, "Create")

    def update(self, artifact_id: str, name: str, package_id: str, artifact_dir: str) -> None:
        if self.artifact_type == "ValueMapping":
            log.info("Update of Value Mapping %s by executing delete followed by create", artifact_id)
            self.delete(artifact_id)
            self.create(artifact_id, name, package_id, artifact_dir)
            return
        description = ""
        if self.artifact_type in DESCRIPTION_ON_UPDATE:
            description = description_from_additional_attributes(artifact_dir)
        log.info("Updating %s designtime artifact %s", self.artifact_type, artifact_id)
        path = f"/api/v1/{self.artifact_type}DesigntimeArtifacts(Id='{artifact_id}',Version='active')"
        self._upsert(artifact_id, name, package_id, description, artifact_dir, "PUT", path, 200, "Update")

    def deploy(self, artifact_id: str) -> None:
        if self.artifact_type in NO_DEPLOY:
            log.warning(
                "Deployment of %s designtime artifact not supported. Skipping deployment of %s",
                self.artifact_type,
                artifact_id,
            )
            return
        log.info("Deploying %s designtime artifact %s", self.artifact_type, artifact_id)
        path = f"/api/v1/Deploy{self.artifact_type}DesigntimeArtifact?Id='{artifact_id}'&Version='active'"
        self.exe.modifying("POST", path, None, 202, f"Deploy {self.artifact_type} designtime artifact")

    def delete(self, artifact_id: str) -> None:
        log.info("Deleting %s designtime artifact %s", self.artifact_type, artifact_id)
        path = f"/api/v1/{self.artifact_type}DesigntimeArtifacts(Id='{artifact_id}',Version='active')"
        self.exe.modifying("DELETE", path, None, 200, f"Delete {self.artifact_type} designtime artifact")

    def get(self, artifact_id: str, version: str) -> tuple[str, str, bool]:
        log.info("Getting details of %s designtime artifact %s", self.artifact_type, artifact_id)
        path = f"/api/v1/{self.artifact_type}DesigntimeArtifacts(Id='{artifact_id}',Version='{version}')"
        call_type = f"Get {self.artifact_type} designtime artifact"
        try:
            response = self.exe.read_only(path, call_type)
        except ApiError as exc:
            if exc.status == 404:
                return "", "", False
            raise
        payload = json.loads(response.content)
        root = payload.get("d", {})
        return root.get("Version", ""), root.get("Description", ""), True

    def download(self, target_file: str, artifact_id: str) -> None:
        log.info("Getting content of artifact %s from tenant for comparison", artifact_id)
        content = self._content(artifact_id, "active")
        os.makedirs(os.path.dirname(target_file), exist_ok=True)
        Path(target_file).write_bytes(content)
        log.info("Content of artifact %s downloaded to %s", artifact_id, target_file)

    def copy_content(self, src_dir: str, tgt_dir: str) -> None:
        if self.artifact_type == "ValueMapping":
            files.replace_dir(os.path.join(src_dir, "META-INF"), os.path.join(tgt_dir, "META-INF"))
            files.copy_file(os.path.join(src_dir, "value_mapping.xml"), os.path.join(tgt_dir, "value_mapping.xml"))
            _copy_metainfo(src_dir, tgt_dir)
            return
        if self.artifact_type == "ScriptCollection":
            files.replace_dir(os.path.join(src_dir, "META-INF"), os.path.join(tgt_dir, "META-INF"))
            src_resources = os.path.join(src_dir, "src", "main", "resources")
            tgt_resources = os.path.join(tgt_dir, "src", "main", "resources")
            if files.exists(src_resources):
                files.replace_dir(src_resources, tgt_resources)
            elif files.exists(tgt_resources):
                shutil.rmtree(tgt_resources)
            _copy_metainfo(src_dir, tgt_dir)
            return
        _copy_standard(src_dir, tgt_dir)

    def compare_content(self, src_dir: str, tgt_dir: str, script_map: list[str], target: str) -> bool:
        if self.artifact_type == "Integration":
            update_bpmn(src_dir, script_map)
            differ = diff_standard(src_dir, tgt_dir)
            if target == "git":
                differ = differ or files.diff_optional_file(src_dir, tgt_dir, "src/main/resources/parameters.prop")
            return differ
        if self.artifact_type == "ValueMapping":
            log.info("Checking for changes in META-INF directory")
            meta = files.diff_directories(os.path.join(src_dir, "META-INF"), os.path.join(tgt_dir, "META-INF"))
            log.info("Checking for changes in value_mapping.xml")
            xml = files.diff_file(os.path.join(src_dir, "value_mapping.xml"), os.path.join(tgt_dir, "value_mapping.xml"))
            return meta or xml
        if self.artifact_type == "ScriptCollection":
            log.info("Checking for changes in META-INF directory")
            meta = files.diff_directories(os.path.join(src_dir, "META-INF"), os.path.join(tgt_dir, "META-INF"))
            src_resources = os.path.join(src_dir, "src", "main", "resources")
            tgt_resources = os.path.join(tgt_dir, "src", "main", "resources")
            if files.exists(src_resources) and files.exists(tgt_resources):
                return meta or diff_standard(src_dir, tgt_dir)
            if not files.exists(src_resources) and not files.exists(tgt_resources):
                log.warning("Skipping diff as /src/main/resources does not exist in both source and target")
                log.info("Checking for changes in metainfo.prop")
                return meta or files.diff_optional_file(src_dir, tgt_dir, "metainfo.prop")
            log.info("Directory /src/main/resources does not exist in either source or target")
            return True
        return diff_standard(src_dir, tgt_dir)

    def _content(self, artifact_id: str, version: str) -> bytes:
        log.info("Getting content of %s designtime artifact %s", self.artifact_type, artifact_id)
        path = f"/api/v1/{self.artifact_type}DesigntimeArtifacts(Id='{artifact_id}',Version='{version}')/$value"
        response = self.exe.read_only(path, f"Download {self.artifact_type} designtime artifact")
        return response.content

    def _upsert(
        self,
        artifact_id: str,
        name: str,
        package_id: str,
        description: str,
        artifact_dir: str,
        method: str,
        path: str,
        success: int,
        call_type: str,
    ) -> None:
        encoded = files.zip_dir_to_base64(artifact_dir)
        self.exe.modifying(
            method,
            path,
            update_body(method, artifact_id, name, package_id, encoded, description),
            success,
            f"{call_type} {self.artifact_type} designtime artifact",
        )


def update_body(method: str, artifact_id: str, name: str, package_id: str, content: str, description: str) -> bytes:
    payload: dict[str, str] = {"ArtifactContent": content}
    if method != "PUT":
        if name:
            payload["Name"] = name
        if artifact_id:
            payload["Id"] = artifact_id
        if package_id:
            payload["PackageId"] = package_id
    if description:
        payload["Description"] = description
    return json.dumps(payload).encode()


def description_from_additional_attributes(artifact_dir: str) -> str:
    attr_file = os.path.join(artifact_dir, "src", "main", "resources", "additionalAttributes.json")
    if not files.exists(attr_file):
        log.info("additionalAttributes.json file not found. Description will be unchanged")
        return ""
    payload = json.loads(Path(attr_file).read_text(encoding="utf-8"))
    description = payload.get("Description", "")
    log.info("additionalAttributes.json file found. Description = %s", description)
    return description


def diff_standard(first_dir: str, second_dir: str) -> bool:
    log.info("Checking for changes in META-INF directory")
    meta = files.diff_directories(os.path.join(first_dir, "META-INF"), os.path.join(second_dir, "META-INF"))
    log.info("Checking for changes in src/main/resources directory")
    resources = files.diff_directories(
        os.path.join(first_dir, "src", "main", "resources"),
        os.path.join(second_dir, "src", "main", "resources"),
    )
    log.info("Checking for changes in metainfo.prop")
    metainfo = files.diff_optional_file(first_dir, second_dir, "metainfo.prop")
    return meta or resources or metainfo


def _copy_standard(src_dir: str, tgt_dir: str) -> None:
    files.replace_dir(os.path.join(src_dir, "META-INF"), os.path.join(tgt_dir, "META-INF"))
    files.replace_dir(
        os.path.join(src_dir, "src", "main", "resources"),
        os.path.join(tgt_dir, "src", "main", "resources"),
    )
    _copy_metainfo(src_dir, tgt_dir)


def _copy_metainfo(src_dir: str, tgt_dir: str) -> None:
    source = os.path.join(src_dir, "metainfo.prop")
    if files.exists(source):
        files.copy_file(source, os.path.join(tgt_dir, "metainfo.prop"))


class IntegrationPackage:
    def __init__(self, exe: HttpExecuter):
        self.exe = exe

    def list_ids(self) -> list[str]:
        log.info("Getting list of IntegrationPackages")
        rows = odata_results(self.exe, "/api/v1/IntegrationPackages", "Get IntegrationPackages list")
        return [item.get("Id", "") for item in rows]

    def get(self, package_id: str) -> tuple[dict | None, bool, bool]:
        log.info("Getting details of integration package %s", package_id)
        call_type = "Get IntegrationPackages by ID"
        try:
            response = self.exe.read_only(f"/api/v1/IntegrationPackages('{package_id}')", call_type)
        except ApiError as exc:
            if exc.status == 404:
                return None, False, False
            raise
        payload = json.loads(response.content)
        read_only = payload.get("d", {}).get("Mode") == "READ_ONLY"
        return payload, read_only, True

    def artifacts(self, package_id: str, artifact_type: str) -> list[dict]:
        log.info("Getting %s designtime artifacts of package %s", artifact_type, package_id)
        path = f"/api/v1/IntegrationPackages('{package_id}')/{artifact_type}DesigntimeArtifacts"
        rows = odata_results(self.exe, path, f"Get {artifact_type} designtime artifacts of IntegrationPackages")
        details = []
        for result in rows:
            details.append(
                {
                    "Id": result.get("Id", ""),
                    "Name": result.get("Name", ""),
                    "Version": result.get("Version", ""),
                    "IsDraft": result.get("Version") == "Active",
                    "ArtifactType": artifact_type,
                }
            )
        return details

    def all_artifacts(self, package_id: str) -> list[dict]:
        details: list[dict] = []
        for artifact_type in ARTIFACT_TYPES:
            details.extend(self.artifacts(package_id, artifact_type))
        return details

    def create(self, package_data: dict) -> None:
        package_id = package_data["d"]["Id"]
        log.info("Creating integration package %s", package_id)
        self.exe.modifying(
            "POST",
            "/api/v1/IntegrationPackages",
            package_body(package_data),
            201,
            "Create integration package",
        )

    def update(self, package_data: dict) -> None:
        package_id = package_data["d"]["Id"]
        log.info("Updating integration package %s", package_id)
        self.exe.modifying(
            "PUT",
            f"/api/v1/IntegrationPackages('{package_id}')",
            package_body(package_data),
            202,
            "Update integration package",
        )


# Fields the IntegrationPackages POST/PUT accepts. The GET payload also carries
# read-only values (__metadata, ResourceId, authors, dates, deferred links).
# Sending those back is rejected by the OData service.
PACKAGE_WRITABLE = (
    "Id",
    "Name",
    "Description",
    "ShortText",
    "Version",
    "Vendor",
    "SupportedPlatform",
    "Products",
    "Keywords",
    "Countries",
    "Industries",
    "LineOfBusiness",
)
# Mode is read-only on the API and is kept only in the Git copy.
PACKAGE_STORED = PACKAGE_WRITABLE + ("Mode",)


def package_for_storage(package_data: dict) -> dict:
    root = package_data.get("d", {})
    kept = {}
    for key in PACKAGE_STORED:
        value = root.get(key)
        if value in ("", None):
            continue
        kept[key] = value
    return {"d": kept}


def package_body(package_data: dict) -> bytes:
    stored = package_for_storage(package_data)
    stored["d"].pop("Mode", None)
    return json.dumps(stored).encode()


def load_package_file(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def find_artifact(artifact_id: str, artifacts: list[dict]) -> dict | None:
    for artifact in artifacts:
        if artifact["Id"] == artifact_id:
            return artifact
    return None


class Runtime:
    def __init__(self, exe: HttpExecuter):
        self.exe = exe

    def undeploy(self, artifact_id: str) -> None:
        log.info("Undeploying runtime artifact %s", artifact_id)
        self.exe.modifying("DELETE", f"/api/v1/IntegrationRuntimeArtifacts('{artifact_id}')", None, 202, "Undeploy runtime artifact")

    def get(self, artifact_id: str) -> tuple[str, str]:
        log.info("Getting details of runtime artifact %s", artifact_id)
        call_type = "Get runtime artifact"
        try:
            response = self.exe.read_only(f"/api/v1/IntegrationRuntimeArtifacts('{artifact_id}')", call_type)
        except ApiError as exc:
            if exc.status == 404 or b"Requested entity could not be found" in exc.body:
                return "NOT_DEPLOYED", ""
            raise
        payload = json.loads(response.content)
        root = payload.get("d", {})
        status = root.get("Status", "")
        if status == "STARTED":
            return root.get("Version", ""), "STARTED"
        return "", status

    def error_info(self, artifact_id: str) -> str:
        log.info("Getting error info of runtime artifact %s", artifact_id)
        path = f"/api/v1/IntegrationRuntimeArtifacts('{artifact_id}')/ErrorInformation/$value"
        response = self.exe.read_only(path, "Get runtime artifact error information")
        payload = json.loads(response.content)
        parameters = payload.get("parameter") or []
        return parameters[0] if parameters else ""


class Configuration:
    def __init__(self, exe: HttpExecuter):
        self.exe = exe

    def get(self, artifact_id: str, version: str) -> list[dict]:
        log.info("Getting configuration parameters of Integration designtime artifact %s", artifact_id)
        path = f"/api/v1/IntegrationDesigntimeArtifacts(Id='{artifact_id}',Version='{version}')/Configurations"
        response = self.exe.read_only(path, "Get configuration parameters")
        payload = json.loads(response.content)
        return payload.get("d", {}).get("results", [])

    def update(self, artifact_id: str, version: str, key: str, value: str) -> None:
        log.info("Updating configuration parameter %s of Integration designtime artifact %s", key, artifact_id)
        encoded = quote(key, safe="")
        path = (
            f"/api/v1/IntegrationDesigntimeArtifacts(Id='{artifact_id}',Version='{version}')"
            f"/$links/Configurations('{encoded}')"
        )
        body = json.dumps({"ParameterValue": value}).encode()
        self.exe.modifying("PUT", path, body, 202, f"Update configuration parameter {key}")


def odata_path(url: str) -> str:
    if url.startswith("http://") or url.startswith("https://"):
        parts = urlsplit(url)
        if parts.query:
            return f"{parts.path}?{parts.query}"
        return parts.path
    return url


def odata_results(exe: HttpExecuter, path: str, call_type: str) -> list:
    """Read an OData collection, following d.__next when the tenant pages it."""
    items: list = []
    seen: set[str] = set()
    while path and path not in seen:
        seen.add(path)
        response = exe.read_only(path, call_type)
        payload = json.loads(response.content)
        root = payload.get("d", {})
        if isinstance(root, list):
            items.extend(root)
            break
        batch = root.get("results")
        if isinstance(batch, list):
            items.extend(batch)
        nxt = root.get("__next") or root.get("@odata.nextLink") or ""
        path = odata_path(nxt) if nxt else ""
    return items


def update_configuration(artifact_id: str, parameters_file: str, exe: HttpExecuter) -> bool:
    tenant_parameters = Configuration(exe).get(artifact_id, "active")
    log.info("Getting parameters from %s file", parameters_file)
    file_parameters = parse_properties(Path(parameters_file).read_text(encoding="utf-8"))
    log.info("Comparing parameters and updating where necessary")
    updated = False
    for result in tenant_parameters:
        if result.get("DataType") == "custom:schedule":
            continue
        key = result.get("ParameterKey", "")
        if key not in file_parameters:
            continue
        file_value = file_parameters[key]
        if file_value != result.get("ParameterValue"):
            log.info(
                "Parameter %s to be updated from %s to %s",
                key,
                result.get("ParameterValue"),
                file_value,
            )
            Configuration(exe).update(artifact_id, "active", key, file_value)
            updated = True
    if updated:
        log.info("Configured parameters of %s changed. Runtime restart is left to deploy", artifact_id)
    else:
        log.info("No updates required for configured parameters")
    return updated
