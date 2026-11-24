#!/usr/bin/env python3
"""Versioning shared by the deploy workflows of the optimaru repositories.

Production releases are `vX.Y.Z`: the caller picks major, minor or patch and the
latest production tag is raised accordingly. Stage deploys are
`vX.Y.Z-stageN`: the latest production version with a counter that starts at 0
and grows with each stage deploy of that version. With no production tag yet,
the version in the first versioned file is the base (0.0.0 if there is none).

  plan     work out the version, write it into the versioned files of the
           checkout (so the build carries it) and set the step outputs.
  publish  after the deploy succeeded: stage pushes its tag on the deployed
           commit; prod commits the versioned files on top of the deployed
           commit, tags that commit, pushes the tag, brings the commit to the
           default branch and creates the GitHub release with generated notes.

Versioned files: `pyproject.toml` gets the PEP 440 form (`1.2.3`,
`1.2.3+stage0`) and a `uv.lock` next to it follows; `package.json` (and a
`package-lock.json` next to it) gets the semver form (`1.2.3`, `1.2.3-stage0`).
Inputs come from environment variables (see action.yml), never from argv.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

PROD_TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)")

Version = tuple[int, int, int]


def git(*args: str, check: bool = True) -> str:
    result = subprocess.run(["git", *args], check=check, capture_output=True, text=True)
    return result.stdout.strip()


def fail(message: str) -> None:
    print(f"::error::{message}")
    sys.exit(1)


def fmt(v: Version) -> str:
    return ".".join(map(str, v))


def files() -> list[Path]:
    paths = [Path(line.strip()) for line in os.environ.get("FILES", "").splitlines() if line.strip()]
    for path in paths:
        if path.name not in ("pyproject.toml", "package.json"):
            fail(f"{path}: only pyproject.toml and package.json are versioned files")
        if not path.is_file():
            fail(f"{path}: no such file")
    return paths


def output(**values: str) -> None:
    with open(os.environ["GITHUB_OUTPUT"], "a") as out:
        for key, value in values.items():
            out.write(f"{key}={value}\n")


def summary(text: str) -> None:
    if "GITHUB_STEP_SUMMARY" in os.environ:
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as out:
            out.write(text + "\n")


# --- versioned files -------------------------------------------------------------------------


def project_table(text: str) -> tuple[int, int]:
    """Start and end offsets of the [project] table of a pyproject.toml."""
    start = re.search(r"(?m)^\[project\]\s*$", text)
    if not start:
        raise ValueError("no [project] table")
    end = re.search(r"(?m)^\[", text[start.end() :])
    return start.end(), start.end() + end.start() if end else len(text)


def read_version(path: Path) -> str | None:
    if path.name == "pyproject.toml":
        return tomllib.loads(path.read_text()).get("project", {}).get("version")
    return json.loads(path.read_text()).get("version")


def write_pyproject(path: Path, version: str) -> None:
    text = path.read_text()
    start, end = project_table(text)
    table, n = re.subn(r'(?m)^(version\s*=\s*)"[^"]*"', rf'\g<1>"{version}"', text[start:end], count=1)
    if not n:
        fail(f"{path}: [project] has no static version")
    path.write_text(text[:start] + table + text[end:])
    lock = path.with_name("uv.lock")
    if lock.is_file():
        name = re.sub(r"[-_.]+", "-", tomllib.loads(path.read_text())["project"]["name"]).lower()
        pattern = rf'(?m)^(\[\[package\]\]\nname = "{re.escape(name)}"\nversion = )"[^"]*"'
        text, n = re.subn(pattern, rf'\g<1>"{version}"', lock.read_text(), count=1)
        if n:
            lock.write_text(text)


def write_package_json(path: Path, version: str) -> None:
    text = path.read_text()
    # Only the top-level key: two-space or tab indented, before any nested object.
    text, n = re.subn(r'(?m)^(  |\t)("version"\s*:\s*)"[^"]*"', rf'\1\2"{version}"', text, count=1)
    if not n:
        fail(f'{path}: no top-level "version" field')
    path.write_text(text)
    lock = path.with_name("package-lock.json")
    if lock.is_file():
        data = json.loads(lock.read_text())
        data["version"] = version
        if "" in data.get("packages", {}):
            data["packages"][""]["version"] = version
        lock.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def stamp(paths: list[Path], semver: str, pep440: str) -> list[Path]:
    """Write the version; return every file that changed (lockfiles included)."""
    for path in paths:
        if path.name == "pyproject.toml":
            write_pyproject(path, pep440)
        else:
            write_package_json(path, semver)
    return [Path(p) for p in git("diff", "--name-only").splitlines()]


# --- plan ------------------------------------------------------------------------------------


def prod_versions(tags: list[str]) -> list[Version]:
    return sorted(tuple(map(int, m.groups())) for t in tags if (m := PROD_TAG.match(t)))


def plan() -> None:
    environment = os.environ["ENVIRONMENT"]
    if environment not in ("prod", "stage"):
        fail(f"environment must be prod or stage, not {environment!r}")
    paths = files()
    git("fetch", "--force", "--tags", "--quiet", "origin")
    tags = git("tag", "--list", "v*").splitlines()
    released = prod_versions(tags)

    base: Version = (0, 0, 0)
    if released:
        base = released[-1]
    elif paths and (current := read_version(paths[0])) and (m := SEMVER.match(current)):
        base = tuple(map(int, m.groups()))

    ref = os.environ.get("GITHUB_REF", "")
    previous = ""
    existing = "false"
    if environment == "prod" and (m := PROD_TAG.match(ref.removeprefix("refs/tags/"))) and ref.startswith("refs/tags/"):
        # A run on a production tag deploys that release; the tag commit already carries the version.
        version: Version = tuple(map(int, m.groups()))
        older = [v for v in released if v < version]
        previous = f"v{fmt(older[-1])}" if older else ""
        semver = pep440 = fmt(version)
        existing = "true"
    elif environment == "prod":
        bump = os.environ.get("BUMP", "")
        major, minor, patch = base
        if bump == "major":
            version = (major + 1, 0, 0)
        elif bump == "minor":
            version = (major, minor + 1, 0)
        elif bump == "patch":
            version = (major, minor, patch + 1)
        else:
            fail(f"bump must be major, minor or patch, not {bump!r}")
        previous = f"v{fmt(released[-1])}" if released else ""
        semver = pep440 = fmt(version)
    else:
        stage = re.compile(rf"^v{re.escape(fmt(base))}-stage(\d+)$")
        counters = [int(m.group(1)) for t in tags if (m := stage.match(t))]
        n = max(counters) + 1 if counters else 0
        semver = f"{fmt(base)}-stage{n}"
        pep440 = f"{fmt(base)}+stage{n}"

    tag = f"v{semver}"
    if existing == "false" and tag in tags:
        fail(f"tag {tag} already exists")
    changed = stamp(paths, semver, pep440) if existing == "false" else []
    output(
        version=semver,
        pep440=pep440,
        tag=tag,
        previous_tag=previous,
        existing=existing,
        files=" ".join(map(str, changed)),
    )
    summary(f"### Version `{tag}` ({environment})")
    print(f"{environment}: {tag} (previous release: {previous or 'none'})")


# --- publish ---------------------------------------------------------------------------------


def push_to_default_branch(commit: str) -> None:
    """Fast-forward the default branch to the release commit, or replay it on top if it moved."""
    branch = os.environ.get("BRANCH") or "main"
    if subprocess.run(["git", "push", "origin", f"{commit}:refs/heads/{branch}"]).returncode == 0:
        return
    git("fetch", "--quiet", "origin", branch)
    git("checkout", "--quiet", "--detach", "FETCH_HEAD")
    picked = subprocess.run(["git", "cherry-pick", commit], capture_output=True, text=True)
    if picked.returncode == 0 and subprocess.run(["git", "push", "origin", f"HEAD:refs/heads/{branch}"]).returncode == 0:
        return
    print(
        f"::warning::Could not bring the version commit to {branch} (it moved or is protected). "
        f"The release tag is pushed; raise the version files on {branch} in a PR."
    )


def publish() -> None:
    environment = os.environ["ENVIRONMENT"]
    tag = os.environ["TAG"]
    if not tag:
        fail("no tag: run the version action first")
    sha = os.environ["GITHUB_SHA"]
    git("config", "user.name", "github-actions[bot]")
    git("config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com")

    if os.environ.get("EXISTING") != "true":
        target = sha
        if environment == "prod":
            changed = os.environ.get("CHANGED", "").split()
            if changed:
                git("add", "--", *changed)
                git("commit", "--quiet", "--no-verify", "-m", f"Release {tag} [skip ci]")
                target = git("rev-parse", "HEAD")
        git("tag", "-a", tag, "-m", tag, target)
        git("push", "origin", f"refs/tags/{tag}")
        print(f"pushed {tag} on {target}")
        if environment == "prod" and target != sha:
            push_to_default_branch(target)

    if environment == "prod" and os.environ.get("RELEASE", "true") == "true":
        if subprocess.run(["gh", "release", "view", tag], capture_output=True).returncode == 0:
            print(f"release {tag} already exists")
            return
        args = ["gh", "release", "create", tag, "--verify-tag", "--title", tag, "--generate-notes"]
        if previous := os.environ.get("PREVIOUS_TAG"):
            args += ["--notes-start-tag", previous]
        subprocess.run(args, check=True)


if __name__ == "__main__":
    {"plan": plan, "publish": publish}[sys.argv[1]]()
