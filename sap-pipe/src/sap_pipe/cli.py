import os
import time
from pathlib import Path

import click
import yaml

from sap_pipe import __version__, apim, files, gitrepo
from sap_pipe.cpi import ARTIFACT_TYPES, NO_DEPLOY, DesigntimeArtifact, IntegrationPackage, Runtime
from sap_pipe.httpclient import ApiError, HttpExecuter
from sap_pipe.log import get_logger, init_logging
from sap_pipe.sync import (
    artifacts_to_git,
    artifacts_to_tenant,
    create_package_if_missing,
    filter_skipped,
    package_to_git,
    single_artifact_to_tenant,
    sync_package_to_tenant,
    upsert_package_from_file,
    verify_downloadable,
)
from sap_pipe.textutil import header_get, parse_manifest, split_csv, trim_manifest_field, trim_slice

log = get_logger()
ENV = "SAP_PIPE_"


class Settings:
    def __init__(self, **values):
        for key, value in values.items():
            setattr(self, key, value)


def _yaml_config(path: str) -> dict:
    candidate = path or os.path.join(Path.home(), "sap-pipe.yaml")
    if path or os.path.exists(candidate):
        if not os.path.exists(candidate):
            raise click.ClickException(f"config file not found: {candidate}")
        loaded = yaml.safe_load(Path(candidate).read_text(encoding="utf-8")) or {}
        if not isinstance(loaded, dict):
            raise click.ClickException("config file must be a mapping")
        return {str(key).replace("-", "_"): value for key, value in loaded.items()}
    return {}


def _overlay(ctx: click.Context, name: str, value, config: dict):
    source = ctx.get_parameter_source(name)
    if source == click.core.ParameterSource.DEFAULT and name in config:
        return config[name]
    return value


def _env(flag: str) -> str:
    return ENV + flag.upper().replace("-", "_")


def _csv(_ctx, _param, value: str | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return trim_slice([str(item) for item in value])
    return split_csv(str(value))


def _require_auth(settings: Settings) -> None:
    if not settings.tmn_host:
        raise click.ClickException('required flag "tmn-host" not set')
    if settings.oauth_host:
        if not settings.oauth_clientid or not settings.oauth_clientsecret:
            raise click.ClickException('flags "oauth-host", "oauth-clientid" and "oauth-clientsecret" must be set together')
        return
    if not settings.tmn_userid or not settings.tmn_password:
        raise click.ClickException('required flag "tmn-userid" (Basic Auth) or "oauth-host" (OAuth) not set')


def _client(settings: Settings) -> HttpExecuter:
    _require_auth(settings)
    return HttpExecuter(
        host=settings.tmn_host,
        oauth_host=settings.oauth_host or "",
        oauth_path=settings.oauth_path or "/oauth/token",
        client_id=settings.oauth_clientid or "",
        client_secret=settings.oauth_clientsecret or "",
        user_id=settings.tmn_userid or "",
        password=settings.tmn_password or "",
    )


def _expand(value: str, settings: Settings, flag: str) -> str:
    expanded = os.path.expandvars(value or "")
    secrets = {
        "tmn-userid": settings.tmn_userid,
        "tmn-password": settings.tmn_password,
        "oauth-clientid": settings.oauth_clientid,
        "oauth-clientsecret": settings.oauth_clientsecret,
    }
    for name, secret in secrets.items():
        if secret and secret in expanded:
            raise click.ClickException(
                f"security alert for --{flag}: Input contains sensitive content from configuration parameter {name}"
            )
    return expanded


def _check_subdir(git_repo: str, artifacts: str) -> None:
    if not git_repo or not artifacts:
        return
    root = os.path.abspath(git_repo) + os.sep
    child = os.path.abspath(artifacts)
    if not child.startswith(root):
        raise click.ClickException(f"--dir-artifacts [{artifacts}] should be a subdirectory of --dir-git-repo [{root}]")


def _commit(settings: Settings, git_repo: str, message: str, user: str, email: str, skip: bool) -> None:
    if skip:
        return
    gitrepo.commit_to_repo(git_repo, message, user, email)


def _manifest_name(manifest_file: str, artifact_id: str) -> str:
    headers = parse_manifest(Path(manifest_file).read_text(encoding="utf-8"))
    bundle_name = trim_manifest_field(header_get(headers, "Bundle-Name"))
    if bundle_name:
        log.info("Using %s from Bundle-Name in MANIFEST.MF as artifact name", bundle_name)
        return bundle_name
    log.info("Using artifact ID %s as artifact name", artifact_id)
    return artifact_id


def _click_error(exc: Exception) -> None:
    if isinstance(exc, ApiError):
        detail = exc.body.decode("utf-8", errors="replace").strip()
        message = str(exc)
        if detail:
            message = f"{message}. {detail[:500]}"
        raise click.ClickException(message) from None
    raise click.ClickException(str(exc)) from None


def _sap(action) -> None:
    """Turn expected business and HTTP failures into a one-line CLI error."""
    try:
        action()
    except (RuntimeError, ApiError) as exc:
        _click_error(exc)


def _effective(settings: Settings, kwargs: dict) -> dict:
    """Use values already overlaid from sap-pipe.yaml, not only the raw flags."""
    merged = dict(kwargs)
    lists = {"ids_include", "ids_exclude", "script_collection_map", "artifact_ids", "redeploy_ids"}
    for key in merged:
        if not hasattr(settings, key):
            continue
        value = getattr(settings, key)
        if key in lists and isinstance(value, str):
            value = split_csv(value)
        merged[key] = value
    return merged


def _deploy(
    settings: Settings,
    artifact_ids: list[str],
    artifact_type: str,
    delay: int,
    limit: int,
    compare: bool,
    force_ids: list[str],
) -> None:
    exe = _client(settings)
    design = DesigntimeArtifact(artifact_type, exe)
    runtime = Runtime(exe)
    forced = set(force_ids)
    wait_for: list[str] = []
    for index, artifact_id in enumerate(artifact_ids, start=1):
        log.info("Processing artifact %d - %s", index, artifact_id)
        version, _description, exists = design.get(artifact_id, "active")
        if not exists:
            raise click.ClickException(f"Designtime artifact {artifact_id} does not exist")
        if artifact_type in NO_DEPLOY:
            design.deploy(artifact_id)
            continue
        runtime_version, _status = ("", "")
        if compare or artifact_id in forced:
            runtime_version, _status = runtime.get(artifact_id)
            log.info("Comparing designtime version with runtime version")
            log.debug("Designtime version = %s. Runtime version = %s", version, runtime_version)
        same_version = bool(version) and version == runtime_version and runtime_version != "NOT_DEPLOYED"
        if artifact_id in forced and same_version:
            log.info("Redeploying %s without a version change: undeploy, then deploy", artifact_id)
            runtime.undeploy(artifact_id)
            _wait_until_undeployed(runtime, delay, limit, artifact_id)
            design.deploy(artifact_id)
            _wait_until_started(runtime, delay, limit, artifact_id)
            log.info("Artifact %d - %s deployed successfully", index, artifact_id)
            continue
        triggered = True
        if compare and same_version:
            log.info("Artifact %s with version %s already deployed. Skipping runtime deployment", artifact_id, runtime_version)
            triggered = False
        else:
            log.info("Proceeding to deploy artifact %s with version %s", artifact_id, version)
            design.deploy(artifact_id)
            log.info("Artifact %s deployment triggered", artifact_id)
        wait_for.append(artifact_id)
        if not triggered:
            log.info("Will still confirm runtime status for %s", artifact_id)
    for index, artifact_id in enumerate(wait_for, start=1):
        _wait_until_started(runtime, delay, limit, artifact_id)
        log.info("Artifact %d - %s deployed successfully", index, artifact_id)
    log.info("Artifact(s) deployment completed successfully")


_UNDEPLOY_PENDING = {"STARTED", "STARTING", "STOPPING", "UNDEPLOYING", "STOPPED", ""}
_UNDEPLOY_MAX_DELAY = 5


def _wait_until_undeployed(runtime: Runtime, delay: int, limit: int, artifact_id: str) -> None:
    """The undeploy call returns 202 before the runtime artifact is gone."""
    poll_delay = min(delay, _UNDEPLOY_MAX_DELAY)
    log.info(
        "Waiting until runtime artifact %s is removed before deploy (every %d seconds up to %d times)",
        artifact_id,
        poll_delay,
        limit,
    )
    last_status = ""
    for attempt in range(limit):
        version, status = runtime.get(artifact_id)
        last_status = status or version
        log.info("Check %d - runtime status after undeploy = %s", attempt + 1, last_status)
        if version == "NOT_DEPLOYED":
            return
        if status not in _UNDEPLOY_PENDING:
            message = runtime.error_info(artifact_id)
            raise click.ClickException(
                f"Undeploy of {artifact_id} ended with status {status}. Error message = {message}"
            )
        if attempt == limit - 1:
            break
        time.sleep(poll_delay)
    raise click.ClickException(
        f"Artifact {artifact_id} was still {last_status} after undeploy; deploy was not started"
    )


def _wait_until_started(runtime: Runtime, delay: int, limit: int, artifact_id: str) -> None:
    log.info("Checking runtime status for artifact %s every %d seconds up to %d times", artifact_id, delay, limit)
    last_status = ""
    for attempt in range(limit):
        version, status = runtime.get(artifact_id)
        last_status = status or version
        log.info("Check %d - Current artifact runtime status = %s", attempt + 1, last_status)
        if status == "STARTED" and version != "NOT_DEPLOYED":
            return
        if version != "NOT_DEPLOYED" and status not in ("STARTING", ""):
            message = runtime.error_info(artifact_id)
            raise click.ClickException(
                f"Artifact deployment unsuccessful, ended with status {status}. Error message = {message}"
            )
        if attempt == limit - 1:
            break
        time.sleep(delay)
    raise click.ClickException(
        f"Artifact {artifact_id} did not reach STARTED after {limit} checks (last status = {last_status})"
    )


def _snapshot(settings: Settings, artifacts_base: str, work_dir: str, draft: str, sync_details: bool, included: list[str], excluded: list[str]) -> None:
    log.info("---------------------------------------------------------------------------------")
    log.info("Begin taking a snapshot of the tenant")
    exe = _client(settings)
    ids = IntegrationPackage(exe).list_ids()
    if not ids:
        raise click.ClickException("No packages found in the tenant")
    log.info("Processing %d packages", len(ids))
    for index, package_id in enumerate(ids, start=1):
        log.info("---------------------------------------------------------------------------------")
        log.info("Processing package %d/%d - ID: %s", index, len(ids), package_id)
        package_data, read_only, _exists = verify_downloadable(package_id, exe)
        if read_only:
            continue
        if filter_skipped(package_id, included, excluded):
            continue
        package_work = os.path.join(work_dir, package_id)
        package_dir = os.path.join(artifacts_base, package_id)
        if sync_details and package_data is not None:
            package_to_git(package_data, package_id, package_work, package_dir)
        artifacts_to_git(package_id, package_work, package_dir, [], [], draft, "ID", [], exe)
    log.info("---------------------------------------------------------------------------------")
    log.info("Completed taking a snapshot of the tenant")


def _target_environment(explicit: str) -> str:
    return (explicit or os.environ.get("CI_ENVIRONMENT_NAME", "")).strip()


def _restore(
    settings: Settings,
    artifacts_base: str,
    work_dir: str,
    included: list[str],
    excluded: list[str],
    environment: str,
) -> None:
    log.info("---------------------------------------------------------------------------------")
    log.info("Begin restoring snapshot to the tenant")
    exe = _client(settings)
    for entry in os.scandir(artifacts_base):
        if not entry.is_dir():
            continue
        package_id = entry.name
        package_file = os.path.join(entry.path, f"{package_id}.json")
        log.info("---------------------------------------------------------------------------------")
        log.info("Processing directory %s", entry.path)
        if not files.exists(package_file):
            log.warning("Skipping directory as integration package file %s is not found", package_file)
            continue
        if filter_skipped(package_id, included, excluded):
            continue
        upsert_package_from_file(package_file, exe)
        artifacts_to_tenant(package_id, work_dir, entry.path, [], [], exe, environment)
    log.info("---------------------------------------------------------------------------------")
    log.info("Completed restoring snapshot to the tenant")


auth_options = [
    click.option("--config", default="", envvar=_env("config"), help="Config file (default is $HOME/sap-pipe.yaml)"),
    click.option("--tmn-host", default="", envvar=_env("tmn-host"), help="Host for the Cloud Integration tenant or API Portal, without https://"),
    click.option("--tmn-userid", default="", envvar=_env("tmn-userid"), help="User ID for Basic Auth"),
    click.option("--tmn-password", default="", envvar=_env("tmn-password"), help="Password for Basic Auth"),
    click.option("--oauth-host", default="", envvar=_env("oauth-host"), help="OAuth token host, without https://"),
    click.option("--oauth-clientid", default="", envvar=_env("oauth-clientid"), help="OAuth client ID"),
    click.option("--oauth-clientsecret", default="", envvar=_env("oauth-clientsecret"), help="OAuth client secret"),
    click.option("--oauth-path", default="/oauth/token", show_default=True, envvar=_env("oauth-path"), help="OAuth token path"),
    click.option("--debug", is_flag=True, default=False, envvar=_env("debug"), help="Show debug logs"),
]


def common_options(function):
    for option in reversed(auth_options):
        function = option(function)
    return function


def _settings_from(ctx: click.Context, **kwargs) -> Settings:
    config = _yaml_config(kwargs.get("config") or "")
    values = {}
    for name, value in kwargs.items():
        values[name.replace("-", "_")] = _overlay(ctx, name, value, config)
    settings = Settings(**values)
    init_logging(bool(settings.debug))
    return settings


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="sap-pipe")
def cli() -> None:
    """sap-pipe syncs SAP Integration Suite design-time content with Git."""


@cli.group()
def update() -> None:
    """Create or update design-time artifacts and packages on the tenant."""


@update.command("artifact")
@common_options
@click.option("--artifact-id", required=True, envvar=_env("artifact-id"))
@click.option("--artifact-name", default="", envvar=_env("artifact-name"))
@click.option("--package-id", required=True, envvar=_env("package-id"))
@click.option("--package-name", default="", envvar=_env("package-name"))
@click.option("--dir-artifact", required=True, envvar=_env("dir-artifact"))
@click.option("--file-param", default="", envvar=_env("file-param"))
@click.option("--file-manifest", default="", envvar=_env("file-manifest"))
@click.option("--dir-work", default="/tmp", show_default=True, envvar=_env("dir-work"))
@click.option("--script-collection-map", default="", envvar=_env("script-collection-map"), callback=_csv)
@click.option("--artifact-type", default="Integration", show_default=True, envvar=_env("artifact-type"))
@click.pass_context
def update_artifact(ctx, **kwargs):
    """Create or update one design-time artifact."""
    settings = _settings_from(ctx, **kwargs)
    kwargs = _effective(settings, kwargs)
    artifact_type = kwargs["artifact_type"]
    if artifact_type not in ARTIFACT_TYPES:
        raise click.ClickException(f"invalid value for --artifact-type = {artifact_type}")
    log.info("Executing update artifact %s command", artifact_type)
    artifact_dir = _expand(kwargs["dir_artifact"], settings, "dir-artifact")
    work_dir = _expand(kwargs["dir_work"], settings, "dir-work")
    parameters = kwargs["file_param"] or os.path.join(artifact_dir, "src", "main", "resources", "parameters.prop")
    default_parameters = os.path.join(artifact_dir, "src", "main", "resources", "parameters.prop")
    if parameters != default_parameters:
        log.info("Using %s as parameters.prop file", parameters)
        files.copy_file(parameters, default_parameters)
    manifest = kwargs["file_manifest"] or os.path.join(artifact_dir, "META-INF", "MANIFEST.MF")
    default_manifest = os.path.join(artifact_dir, "META-INF", "MANIFEST.MF")
    if manifest != default_manifest:
        log.info("Using %s as MANIFEST.MF file", manifest)
        files.copy_file(manifest, default_manifest)
    package_name = kwargs["package_name"] or kwargs["package_id"]
    if not kwargs["package_name"]:
        log.info("Using package ID %s as package name", kwargs["package_id"])
    artifact_name = kwargs["artifact_name"] or _manifest_name(manifest, kwargs["artifact_id"])
    exe = _client(settings)

    def upload() -> None:
        create_package_if_missing(kwargs["package_id"], package_name, exe)
        single_artifact_to_tenant(
            kwargs["artifact_id"],
            artifact_name,
            artifact_type,
            kwargs["package_id"],
            artifact_dir,
            work_dir,
            parameters,
            kwargs["script_collection_map"],
            exe,
        )

    _sap(upload)


@update.command("package")
@common_options
@click.option("--package-file", required=True, envvar=_env("package-file"))
@click.pass_context
def update_package(ctx, **kwargs):
    """Create or update an integration package from its JSON file."""
    settings = _settings_from(ctx, **kwargs)
    kwargs = _effective(settings, kwargs)
    log.info("Executing update package command")
    _sap(lambda: upsert_package_from_file(kwargs["package_file"], _client(settings)))


@cli.command("deploy")
@common_options
@click.option("--artifact-ids", required=True, envvar=_env("artifact-ids"), callback=_csv)
@click.option("--delay-length", default=30, show_default=True, type=int, envvar=_env("delay-length"))
@click.option("--max-check-limit", default=10, show_default=True, type=int, envvar=_env("max-check-limit"))
@click.option("--compare-versions", default=True, show_default=True, type=bool, envvar=_env("compare-versions"))
@click.option("--artifact-type", default="Integration", show_default=True, envvar=_env("artifact-type"))
@click.option(
    "--redeploy-ids",
    default="",
    envvar=_env("redeploy-ids"),
    callback=_csv,
    help="Artifacts whose design changed without a version bump. Each is undeployed immediately before its own deploy.",
)
@click.pass_context
def deploy(ctx, **kwargs):
    """Deploy design-time artifacts to the runtime."""
    settings = _settings_from(ctx, **kwargs)
    kwargs = _effective(settings, kwargs)
    if kwargs["artifact_type"] not in ARTIFACT_TYPES:
        raise click.ClickException(f"invalid value for --artifact-type = {kwargs['artifact_type']}")
    log.info("Executing deploy %s command", kwargs["artifact_type"])
    _sap(
        lambda: _deploy(
            settings,
            kwargs["artifact_ids"],
            kwargs["artifact_type"],
            kwargs["delay_length"],
            kwargs["max_check_limit"],
            kwargs["compare_versions"],
            kwargs["redeploy_ids"],
        )
    )


git_options = [
    click.option("--dir-git-repo", default="", envvar=_env("dir-git-repo")),
    click.option("--dir-artifacts", default="", envvar=_env("dir-artifacts")),
    click.option("--dir-work", default="/tmp", show_default=True, envvar=_env("dir-work")),
    click.option("--ids-include", default="", envvar=_env("ids-include"), callback=_csv),
    click.option("--ids-exclude", default="", envvar=_env("ids-exclude"), callback=_csv),
    click.option("--target", default="git", show_default=True, envvar=_env("target")),
    click.option("--git-commit-msg", default="Sync repo from tenant", show_default=True, envvar=_env("git-commit-msg")),
    click.option("--git-commit-user", default="sap-pipe", show_default=True, envvar=_env("git-commit-user")),
    click.option("--git-commit-email", default="sap-pipe@localhost", show_default=True, envvar=_env("git-commit-email")),
    click.option("--git-skip-commit", is_flag=True, default=False, envvar=_env("git-skip-commit")),
]


def with_git_options(function):
    for option in reversed(git_options):
        function = option(function)
    return function


@cli.group(invoke_without_command=True)
@common_options
@with_git_options
@click.option("--package-id", default="", envvar=_env("package-id"))
@click.option("--dir-naming-type", default="ID", show_default=True, envvar=_env("dir-naming-type"))
@click.option("--draft-handling", default="SKIP", show_default=True, envvar=_env("draft-handling"))
@click.option("--script-collection-map", default="", envvar=_env("script-collection-map"), callback=_csv)
@click.option("--sync-package-details", is_flag=True, default=False, envvar=_env("sync-package-details"))
@click.option(
    "--environment",
    default="",
    envvar=_env("environment"),
    help="Select parameters/<environment>.prop. Defaults to CI_ENVIRONMENT_NAME.",
)
@click.option(
    "--redeploy-file",
    default="",
    envvar=_env("redeploy-file"),
    help="Write artifact ids that need a same-version restart. Runtime stays up during upload.",
)
@click.pass_context
def sync(ctx, **kwargs):
    """Sync one integration package between the tenant and Git."""
    if ctx.invoked_subcommand is not None:
        ctx.obj = {"params": kwargs, "settings": _settings_from(ctx, **kwargs)}
        return
    settings = _settings_from(ctx, **kwargs)
    kwargs = _effective(settings, kwargs)
    if not kwargs["package_id"]:
        raise click.ClickException('required flag "package-id" not set')
    if kwargs["dir_naming_type"] not in ("ID", "NAME"):
        raise click.ClickException(f"invalid value for --dir-naming-type = {kwargs['dir_naming_type']}")
    if kwargs["draft_handling"] not in ("SKIP", "ADD", "ERROR"):
        raise click.ClickException(f"invalid value for --draft-handling = {kwargs['draft_handling']}")
    if kwargs["target"] not in ("git", "tenant"):
        raise click.ClickException(f"invalid value for --target = {kwargs['target']}")
    if kwargs["ids_include"] and kwargs["ids_exclude"]:
        raise click.ClickException("--ids-include and --ids-exclude are mutually exclusive")
    git_repo = _expand(kwargs["dir_git_repo"], settings, "dir-git-repo")
    if not git_repo:
        raise click.ClickException('required flag "dir-git-repo" not set')
    artifacts = _expand(kwargs["dir_artifacts"], settings, "dir-artifacts")
    _check_subdir(git_repo, artifacts)
    artifacts = artifacts or git_repo
    work_dir = _expand(kwargs["dir_work"], settings, "dir-work")
    environment = _target_environment(kwargs["environment"])
    log.info("Executing sync command for package %s (target=%s)", kwargs["package_id"], kwargs["target"])
    exe = _client(settings)
    try:
        if kwargs["target"] == "git":
            package_data, read_only, _exists = verify_downloadable(kwargs["package_id"], exe)
            if not read_only:
                if kwargs["sync_package_details"] and package_data is not None:
                    package_to_git(package_data, kwargs["package_id"], work_dir, artifacts)
                artifacts_to_git(
                    kwargs["package_id"],
                    work_dir,
                    artifacts,
                    kwargs["ids_include"],
                    kwargs["ids_exclude"],
                    kwargs["draft_handling"],
                    kwargs["dir_naming_type"],
                    kwargs["script_collection_map"],
                    exe,
                )
                _commit(settings, git_repo, kwargs["git_commit_msg"], kwargs["git_commit_user"], kwargs["git_commit_email"], kwargs["git_skip_commit"])
            return
        sync_package_to_tenant(kwargs["package_id"], artifacts, "", exe)
        deferred: list[str] | None = [] if kwargs["redeploy_file"] else None
        artifacts_to_tenant(
            kwargs["package_id"],
            work_dir,
            artifacts,
            kwargs["ids_include"],
            kwargs["ids_exclude"],
            exe,
            environment,
            deferred,
        )
        if deferred is not None:
            Path(kwargs["redeploy_file"]).write_text("\n".join(deferred) + ("\n" if deferred else ""), encoding="utf-8")
    except (RuntimeError, ApiError) as exc:
        _click_error(exc)


def _apim_sync(ctx, kind: str, kwargs: dict) -> None:
    settings = _settings_from(ctx, **kwargs)
    kwargs = _effective(settings, kwargs)
    if kwargs["target"] not in ("git", "tenant"):
        raise click.ClickException(f"invalid value for --target = {kwargs['target']}")
    if kwargs["ids_include"] and kwargs["ids_exclude"]:
        raise click.ClickException("--ids-include and --ids-exclude are mutually exclusive")
    git_repo = _expand(kwargs["dir_git_repo"], settings, "dir-git-repo")
    if not git_repo:
        raise click.ClickException('required flag "dir-git-repo" not set')
    artifacts = _expand(kwargs["dir_artifacts"], settings, "dir-artifacts")
    _check_subdir(git_repo, artifacts)
    artifacts = artifacts or git_repo
    work_dir = os.path.join(_expand(kwargs["dir_work"], settings, "dir-work"), kind)
    log.info("Executing sync %s command", kind)

    def run() -> None:
        exe = _client(settings)
        if kind == "apiproxy":
            runner = apim.sync_proxies_to_git if kwargs["target"] == "git" else apim.sync_proxies_to_tenant
        else:
            runner = apim.sync_products_to_git if kwargs["target"] == "git" else apim.sync_products_to_tenant
        if kind == "apiproduct" and kwargs["target"] == "tenant":
            runner(artifacts, kwargs["ids_include"], kwargs["ids_exclude"], exe)
        else:
            runner(work_dir, artifacts, kwargs["ids_include"], kwargs["ids_exclude"], exe)
        if kwargs["target"] == "git":
            _commit(settings, git_repo, kwargs["git_commit_msg"], kwargs["git_commit_user"], kwargs["git_commit_email"], kwargs["git_skip_commit"])
        apim.cleanup(work_dir)

    _sap(run)


@sync.command("apiproxy")
@common_options
@with_git_options
@click.pass_context
def sync_apiproxy(ctx, **kwargs):
    """Sync API Management proxies."""
    _apim_sync(ctx, "apiproxy", kwargs)


@sync.command("apim")
@common_options
@with_git_options
@click.pass_context
def sync_apim(ctx, **kwargs):
    """Alias of sync apiproxy."""
    _apim_sync(ctx, "apiproxy", kwargs)


@sync.command("apiproduct")
@common_options
@with_git_options
@click.pass_context
def sync_apiproduct(ctx, **kwargs):
    """Sync API Management products. Updates toward the tenant are skipped."""
    _apim_sync(ctx, "apiproduct", kwargs)


@cli.group(invoke_without_command=True)
@common_options
@with_git_options
@click.option("--draft-handling", default="SKIP", show_default=True, envvar=_env("draft-handling"))
@click.option("--sync-package-details/--no-sync-package-details", default=True, show_default=True, envvar=_env("sync-package-details"))
@click.pass_context
def snapshot(ctx, **kwargs):
    """Snapshot editable integration packages from the tenant into Git."""
    if ctx.invoked_subcommand is not None:
        ctx.obj = {"params": kwargs, "settings": _settings_from(ctx, **kwargs)}
        return
    settings = _settings_from(ctx, **kwargs)
    kwargs = _effective(settings, kwargs)
    if kwargs["draft_handling"] not in ("SKIP", "ADD", "ERROR"):
        raise click.ClickException(f"invalid value for --draft-handling = {kwargs['draft_handling']}")
    if kwargs["ids_include"] and kwargs["ids_exclude"]:
        raise click.ClickException("--ids-include and --ids-exclude are mutually exclusive")
    git_repo = _expand(kwargs["dir_git_repo"], settings, "dir-git-repo")
    if not git_repo:
        raise click.ClickException('required flag "dir-git-repo" not set')
    artifacts = _expand(kwargs["dir_artifacts"], settings, "dir-artifacts")
    _check_subdir(git_repo, artifacts)
    artifacts = artifacts or git_repo
    work_dir = _expand(kwargs["dir_work"], settings, "dir-work")
    message = kwargs["git_commit_msg"]
    if message == "Sync repo from tenant":
        message = "Tenant snapshot of " + time.strftime("%a %b %d %H:%M:%S %Y")
    log.info("Executing snapshot command")

    def take_snapshot() -> None:
        _snapshot(settings, artifacts, work_dir, kwargs["draft_handling"], kwargs["sync_package_details"], kwargs["ids_include"], kwargs["ids_exclude"])
        _commit(settings, git_repo, message, kwargs["git_commit_user"], kwargs["git_commit_email"], kwargs["git_skip_commit"])

    _sap(take_snapshot)


@snapshot.command("restore")
@common_options
@with_git_options
@click.option(
    "--environment",
    default="",
    envvar=_env("environment"),
    help="Select parameters/<environment>.prop. Defaults to CI_ENVIRONMENT_NAME.",
)
@click.pass_context
def snapshot_restore(ctx, **kwargs):
    """Restore a snapshot from Git to the tenant."""
    settings = _settings_from(ctx, **kwargs)
    kwargs = _effective(settings, kwargs)
    if kwargs["ids_include"] and kwargs["ids_exclude"]:
        raise click.ClickException("--ids-include and --ids-exclude are mutually exclusive")
    git_repo = _expand(kwargs["dir_git_repo"], settings, "dir-git-repo")
    if not git_repo:
        raise click.ClickException('required flag "dir-git-repo" not set')
    artifacts = _expand(kwargs["dir_artifacts"], settings, "dir-artifacts")
    _check_subdir(git_repo, artifacts)
    artifacts = artifacts or git_repo
    work_dir = _expand(kwargs["dir_work"], settings, "dir-work")
    environment = _target_environment(kwargs["environment"])
    log.info("Executing snapshot restore command")
    _sap(lambda: _restore(settings, artifacts, work_dir, kwargs["ids_include"], kwargs["ids_exclude"], environment))


def main() -> None:
    cli(prog_name="sap-pipe")
