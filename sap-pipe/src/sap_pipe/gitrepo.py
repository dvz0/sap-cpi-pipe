import subprocess

from sap_pipe.log import get_logger

log = get_logger()


def commit_to_repo(git_repo_dir: str, commit_msg: str, commit_user: str, commit_email: str) -> None:
    log.info("Opening Git repository at %s", git_repo_dir)
    _git(git_repo_dir, "rev-parse", "--is-inside-work-tree")
    log.info("Checking status of working tree")
    status = _git(git_repo_dir, "status", "--porcelain")
    if not status.strip():
        log.info("No changes to commit")
        return
    log.info("Adding all files for Git tracking")
    _git(git_repo_dir, "add", "-A")
    status = _git(git_repo_dir, "status", "--porcelain")
    log.info("Status of working tree:\n%s", status)
    log.info("Trying to commit changes")
    _git(
        git_repo_dir,
        "-c",
        f"user.name={commit_user}",
        "-c",
        f"user.email={commit_email}",
        "commit",
        "-m",
        commit_msg,
    )
    log.info("Changes committed")


def _git(repo: str, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", repo, *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise RuntimeError(detail or f"git {' '.join(args)} failed")
    return completed.stdout
