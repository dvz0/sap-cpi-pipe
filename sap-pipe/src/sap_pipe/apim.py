import json
import os
import shutil
from pathlib import Path

from sap_pipe import files
from sap_pipe.httpclient import ApiError, HttpExecuter
from sap_pipe.log import get_logger
from sap_pipe.sync import filter_skipped

log = get_logger()


class APIProxy:
    def __init__(self, exe: HttpExecuter):
        self.exe = exe

    def download(self, name: str, target_root: str) -> None:
        log.info("Downloading APIProxy %s", name)
        body = json.dumps({"selection": {"apiproxies": {"entities": [{"name": name}]}}}).encode()
        response = self.exe.read_only("/apiportal/api/1.0/ContentArchive.svc", "Get APIProxy", content=body, accept=None)
        target_dir = os.path.join(target_root, name)
        target_file = os.path.join(target_dir, f"{name}.zip")
        os.makedirs(os.path.dirname(target_file), exist_ok=True)
        Path(target_file).write_bytes(response.content)
        log.info("Unzipping contents to %s", target_dir)
        files.unzip_source(target_file, target_dir)
        os.remove(target_file)

    def upload(self, source_dir: str, work_dir: str) -> None:
        zip_path = os.path.join(os.path.abspath(work_dir), os.path.basename(os.path.abspath(source_dir)) + ".zip")
        log.debug("Compressing contents of directory %s to file %s", source_dir, zip_path)
        files.zip_dir(source_dir, zip_path, include_src=False)
        log.info("Uploading API content from %s", zip_path)
        body, content_type = _multipart(zip_path)
        self.exe.modifying("POST", "/apiportal/api/1.0/ContentArchive.svc", body, 200, "Upload API ContentArchive", content_type)

    def exists(self, proxy_id: str) -> bool:
        log.info("Getting details of APIProxy %s", proxy_id)
        try:
            self.exe.read_only(f"/apiportal/api/1.0/Management.svc/APIProxies('{proxy_id}')", "Get APIProxy")
        except ApiError as exc:
            if exc.status == 404:
                return False
            raise
        return True

    def list(self) -> list[dict]:
        log.info("Getting list of APIProxies")
        try:
            response = self.exe.read_only("/apiportal/api/1.0/Management.svc/APIProxies", "List APIProxies")
            payload = json.loads(response.content)
        except (ApiError, json.JSONDecodeError) as exc:
            log.warning("Please check that hostname and credentials for APIM are correct")
            raise exc
        return [
            {"Name": item.get("name", ""), "Version": item.get("version", ""), "Status": item.get("state", "")}
            for item in payload.get("d", {}).get("results", [])
        ]


class APIProduct:
    def __init__(self, exe: HttpExecuter):
        self.exe = exe

    def download(self, name: str, target_root: str) -> None:
        log.info("Downloading APIProduct %s", name)
        path = (
            f"/apiportal/api/1.0/Management.svc/APIProducts('{name}')"
            "?$expand=additionalProperties,apiProxies,apiResources,apiResources/apiProxyEndPoint"
        )
        response = self.exe.read_only(path, "Get APIProduct")
        payload = json.loads(response.content)["d"]
        proxies = []
        for proxy in payload.get("apiProxies", {}).get("results", []):
            proxies.append({"__metadata": {"uri": f"APIProxies(name='{proxy.get('name', '')}')"}})
        create_data = _product_create_document(payload, proxies)
        target = os.path.join(target_root, f"{name}.json")
        os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
        Path(target).write_text(json.dumps(create_data, indent=2), encoding="utf-8")

    def upload(self, source_file: str) -> None:
        log.info("Uploading API Product from file %s", source_file)
        create_data = json.loads(Path(source_file).read_text(encoding="utf-8"))
        resources = APIResource(self.exe)
        cache: dict[str, list[dict]] = {}
        for resource in create_data.get("apiResources", []):
            endpoint = resource.get("apiProxyEndPoint", {})
            api_name = endpoint.get("FK_API_NAME", "")
            if resources.exists(resource.get("id", "")):
                continue
            log.warning("APIResource with Id %s does not exist. Trying to retrieve the latest Id", resource.get("id"))
            details = cache.get(api_name)
            if details is None:
                details = resources.by_name(api_name)
                if details:
                    cache[api_name] = details
            replacement = next(
                (item for item in details if item["Name"] == resource.get("name") and item["APIName"] == api_name),
                None,
            )
            if replacement is None:
                raise RuntimeError("no matching APIResource found")
            log.info("Replacing Id %s with %s", resource.get("id"), replacement["Id"])
            resource["id"] = replacement["Id"]
        log.info("Creating API Product %s", create_data.get("name"))
        self.exe.modifying(
            "POST",
            "/apiportal/api/1.0/Management.svc/APIProducts",
            json.dumps(create_data).encode(),
            201,
            "Create APIProduct",
        )

    def exists(self, product_id: str) -> bool:
        log.info("Checking existence of APIProduct %s", product_id)
        try:
            self.exe.read_only(f"/apiportal/api/1.0/Management.svc/APIProducts('{product_id}')", "Get APIProduct")
        except ApiError as exc:
            if exc.status == 404:
                return False
            raise
        return True

    def list(self) -> list[dict]:
        log.info("Getting list of APIProducts")
        response = self.exe.read_only("/apiportal/api/1.0/Management.svc/APIProducts", "List APIProducts")
        payload = json.loads(response.content)
        return [
            {"Name": item.get("name", ""), "Version": item.get("version", ""), "Status": item.get("status_code", "")}
            for item in payload.get("d", {}).get("results", [])
        ]


class APIResource:
    def __init__(self, exe: HttpExecuter):
        self.exe = exe

    def exists(self, resource_id: str) -> bool:
        log.info("Checking existence of APIResource %s", resource_id)
        try:
            self.exe.read_only(f"/apiportal/api/1.0/Management.svc/APIResources('{resource_id}')", "Get APIResource")
        except ApiError as exc:
            if exc.status == 404:
                return False
            raise
        return True

    def by_name(self, api_name: str) -> list[dict]:
        log.info("Getting list of APIResources for %s", api_name)
        path = (
            "/apiportal/api/1.0/Management.svc/APIResources?$expand=apiProxyEndPoint"
            f"&$filter=apiProxyEndPoint/FK_API_NAME%20eq%20'{api_name}'"
        )
        response = self.exe.read_only(path, "List APIResources")
        payload = json.loads(response.content)
        details = []
        for result in payload.get("d", {}).get("results", []):
            details.append(
                {
                    "Name": result.get("name", ""),
                    "Id": result.get("id", ""),
                    "APIName": result.get("apiProxyEndPoint", {}).get("FK_API_NAME", ""),
                }
            )
        return details


def sync_proxies_to_git(work_dir: str, artifacts_dir: str, included: list[str], excluded: list[str], exe: HttpExecuter) -> None:
    log.info("Sync API Proxy content to Git")
    proxy = APIProxy(exe)
    target_root = os.path.join(work_dir, "download")
    os.makedirs(target_root, exist_ok=True)
    for artifact in proxy.list():
        name = artifact["Name"]
        log.info("---------------------------------------------------------------------------------")
        log.info("Begin processing for APIProxy %s", name)
        if filter_skipped(name, included, excluded):
            continue
        proxy.download(name, target_root)
        git_path = os.path.join(artifacts_dir, name)
        downloaded = os.path.join(target_root, name)
        if files.exists(os.path.join(git_path, "manifest.json")):
            log.info("Comparing content from tenant against Git")
            if files.diff_directories(downloaded, git_path):
                log.info("Changes detected and will be updated to Git")
                files.replace_dir(downloaded, git_path)
            else:
                log.info("No changes detected. Update to Git not required")
        else:
            log.info("APIProxy %s does not exist, and will be added to Git", name)
            files.replace_dir(downloaded, git_path)
    log.info("---------------------------------------------------------------------------------")
    log.info("Completed processing of APIProxies")


def sync_proxies_to_tenant(work_dir: str, artifacts_dir: str, included: list[str], excluded: list[str], exe: HttpExecuter) -> None:
    proxy = APIProxy(exe)
    upload_dir = os.path.join(work_dir, "upload")
    download_dir = os.path.join(work_dir, "download")
    os.makedirs(upload_dir, exist_ok=True)
    os.makedirs(download_dir, exist_ok=True)
    found = False
    for entry in os.scandir(artifacts_dir):
        if not entry.is_dir() or not files.exists(os.path.join(entry.path, "manifest.json")):
            continue
        found = True
        artifact_id = entry.name
        log.info("---------------------------------------------------------------------------------")
        log.info("Processing directory %s", entry.path)
        if filter_skipped(artifact_id, included, excluded):
            continue
        log.info("Begin processing for APIProxy %s", artifact_id)
        if not proxy.exists(artifact_id):
            log.info("APIProxy %s will be created", artifact_id)
            proxy.upload(entry.path, upload_dir)
            log.info("APIProxy created successfully")
            continue
        log.info("Checking if APIProxy needs to be updated")
        proxy.download(artifact_id, download_dir)
        log.info("Comparing content from tenant against Git")
        if files.diff_directories(os.path.join(download_dir, artifact_id), entry.path):
            log.info("Changes found in APIProxy. APIProxy will be updated in tenant")
            proxy.upload(entry.path, upload_dir)
            log.info("APIProxy updated successfully")
        else:
            log.info("No changes detected. APIProxy does not need to be updated")
    if not found:
        log.warning("No directory with APIProxy contents found in %s", artifacts_dir)
    log.info("---------------------------------------------------------------------------------")
    log.info("Completed processing of APIProxies")


def sync_products_to_git(work_dir: str, artifacts_dir: str, included: list[str], excluded: list[str], exe: HttpExecuter) -> None:
    log.info("Sync API Product content to Git")
    product = APIProduct(exe)
    target_root = os.path.join(work_dir, "download")
    os.makedirs(target_root, exist_ok=True)
    for artifact in product.list():
        name = artifact["Name"]
        log.info("---------------------------------------------------------------------------------")
        log.info("Begin processing for APIProduct %s", name)
        if filter_skipped(name, included, excluded):
            continue
        product.download(name, target_root)
        git_file = os.path.join(artifacts_dir, f"{name}.json")
        downloaded = os.path.join(target_root, f"{name}.json")
        if files.exists(git_file):
            log.info("Comparing content from tenant against Git")
            if files.diff_file(downloaded, git_file):
                log.info("Changes detected and will be updated to Git")
                files.copy_file(downloaded, git_file)
            else:
                log.info("No changes detected. Update to Git not required")
        else:
            log.info("APIProduct %s does not exist, and will be added to Git", name)
            files.copy_file(downloaded, git_file)
    log.info("---------------------------------------------------------------------------------")
    log.info("Completed processing of APIProducts")


def sync_products_to_tenant(artifacts_dir: str, included: list[str], excluded: list[str], exe: HttpExecuter) -> None:
    product = APIProduct(exe)
    found = False
    for entry in os.scandir(artifacts_dir):
        if entry.is_dir() or not entry.name.endswith(".json"):
            continue
        found = True
        artifact_id = entry.name[: -len(".json")]
        log.info("---------------------------------------------------------------------------------")
        log.info("Processing file %s", entry.path)
        if filter_skipped(artifact_id, included, excluded):
            continue
        log.info("Begin processing for APIProduct %s", artifact_id)
        if not product.exists(artifact_id):
            log.info("APIProduct %s will be created", artifact_id)
            product.upload(entry.path)
            log.info("APIProduct created successfully")
        else:
            log.warning("APIProduct %s already exists. Skipping as update not currently supported", artifact_id)
    if not found:
        log.warning("No directory with APIProduct contents found in %s", artifacts_dir)
    log.info("---------------------------------------------------------------------------------")
    log.info("Completed processing of APIProducts")


def _product_create_document(payload: dict, proxies: list[dict]) -> dict:
    document: dict = {
        "name": payload.get("name", ""),
        "status_code": payload.get("status_code", ""),
        "title": payload.get("title", ""),
    }
    for key in ("version", "shortText", "description", "scope", "quotaTimeUnit"):
        if payload.get(key):
            document[key] = payload[key]
    if payload.get("isPublished"):
        document["isPublished"] = True
    for key in ("quotaCount", "quotaInterval"):
        if payload.get(key):
            document[key] = payload[key]
    document["additionalProperties"] = [
        {"entityId": item.get("entityId", ""), "name": item.get("name", ""), "value": item.get("value", "")}
        for item in payload.get("additionalProperties", {}).get("results", [])
    ]
    document["apiProxies"] = proxies
    document["apiResources"] = []
    for item in payload.get("apiResources", {}).get("results", []):
        endpoint = item.get("apiProxyEndPoint", {})
        document["apiResources"].append(
            {
                "id": item.get("id", ""),
                "isDeleteChecked": bool(item.get("isDeleteChecked", False)),
                "isGetChecked": bool(item.get("isGetChecked", False)),
                "isPostChecked": bool(item.get("isPostChecked", False)),
                "isPutChecked": bool(item.get("isPutChecked", False)),
                "name": item.get("name", ""),
                "apiProxyEndPoint": {"FK_API_NAME": endpoint.get("FK_API_NAME", "")},
            }
        )
    return document


def cleanup(path: str) -> None:
    shutil.rmtree(path, ignore_errors=True)


def _multipart(file_path: str) -> tuple[bytes, str]:
    boundary = "----sappipeboundary"
    filename = os.path.basename(file_path)
    content = Path(file_path).read_bytes()
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode() + content + f"\r\n--{boundary}--\r\n".encode()
    return body, f"multipart/form-data; boundary={boundary}"
