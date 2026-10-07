import os
import re
from pathlib import Path

from sap_pipe.log import get_logger

log = get_logger()

_SCRIPT_BUNDLE = re.compile(
    r"(<(?:[\w.-]+:)?key>\s*scriptBundleId\s*</(?:[\w.-]+:)?key>\s*<(?:[\w.-]+:)?value>)([^<]*)(</(?:[\w.-]+:)?value>)"
)


def update_bpmn(artifact_dir: str, script_map: list[str]) -> None:
    if not script_map:
        return
    mapping: dict[str, str] = {}
    log.debug("Updating files in %s with collection %s", artifact_dir, script_map)
    for pair in script_map:
        source, separator, target = pair.partition("=")
        source = source.strip()
        target = target.strip()
        if separator and source and target:
            mapping[source] = target
    if not mapping:
        return
    bpmn_dir = os.path.join(artifact_dir, "src", "main", "resources", "scenarioflows", "integrationflow")
    if not os.path.isdir(bpmn_dir):
        return
    for entry in os.scandir(bpmn_dir):
        if entry.is_file():
            _update_xml(entry.path, mapping)


def _update_xml(file_path: str, scripts: dict[str, str]) -> None:
    log.info("Processing BPMN2 file %s", file_path)
    raw = Path(file_path).read_bytes()
    original = raw.decode("utf-8")

    def replace(match: re.Match[str]) -> str:
        source = match.group(2).strip()
        target = scripts.get(source, "")
        if not target:
            return match.group(0)
        log.debug("Changing scriptBundleId from %s to %s", source, target)
        return match.group(1) + target + match.group(3)

    updated = _SCRIPT_BUNDLE.sub(replace, original)
    if updated != original:
        Path(file_path).write_bytes(updated.encode("utf-8"))
