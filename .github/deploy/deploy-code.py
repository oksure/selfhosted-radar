#!/usr/bin/env python3
"""Deploy only the three code folders through the PocketHost v5 CLI.

v5 authenticates with a `pbt_` API token (`P1_TOKEN`). It deploys `pb_*` folders
minus `pb_data`/`pb_cache`, so the main database never leaves the host, and it
has no power control: power moves through `phost` (mothership user auth).
"""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOTS = ("pb_public", "pb_hooks", "pb_migrations")
INCLUDES = tuple(f"{p}/**/*" for p in ROOTS)
PRESERVED = {"bible": ("pb_hooks/bible_router.pb.js",)}
USER_AGENT = "pockethost-code-deploy/2.0"
BASE = "https://{instance}.pockethost.io"


class DeployError(Exception):
  pass


class UncertainDeploy(DeployError):
  pass


def platform_path(value):
  parts = value.split("/") if isinstance(value, str) else []
  return len(parts) == 2 and (
    (parts[0] == "pb_hooks" and parts[1].startswith("_ph_")) or
    (parts[0] == "pb_migrations" and re.fullmatch(r"[0-9]+_pockethost_.*\.js", parts[1])))


def code_path(value):
  if not isinstance(value, str) or not value or "\\" in value or "%" in value:
    return False
  parts = value.split("/")
  return (len(parts) > 1 and parts[0] in ROOTS and not platform_path(value)
          and all(p and p not in (".", "..", "pb_data", "node_modules")
                  and not any(ord(c) < 32 or ord(c) == 127 for c in p)
                  for p in parts))


def manifest(root):
  files = {}
  for p in sorted(root.rglob("*")):
    name = p.relative_to(root).as_posix()
    if p.is_symlink() or not (p.is_dir() or p.is_file()):
      raise DeployError("Stage contains a link or special file")
    if p.is_file():
      if not code_path(name):
        raise DeployError("Stage contains a file outside the three code folders")
      files[name] = hashlib.sha256(p.read_bytes()).hexdigest()
    elif name not in ROOTS and not code_path(name):
      raise DeployError("Stage contains an unexpected directory")
  return files


def cli(args, cwd, env, publishing=False):
  """Run `pockethost`; a publish whose outcome is unknown must not be retried."""
  try:
    result = subprocess.run(["pockethost", *args], cwd=cwd, env=env,
                            capture_output=True, text=True, timeout=240)
  except (OSError, subprocess.TimeoutExpired) as exc:
    if publishing:
      raise UncertainDeploy("Deployment outcome unknown; inspect before changing power") from exc
    raise DeployError("PocketHost CLI unavailable or timed out") from exc
  if result.returncode and publishing:
    raise UncertainDeploy("Deployment outcome unknown; inspect before changing power")
  if result.returncode:
    # CLI errors may contain remote text. Do not echo them or credentials.
    raise DeployError("PocketHost CLI failed")
  return result.stdout


def environment(token):
  env = {k: v for k, v in os.environ.items()
         if not k.startswith(("POCKETHOST_", "P1_", "PHIO_"))
         and not k.lower().endswith("_proxy") and k not in ("NODE_OPTIONS", "PATH")}
  env.update(P1_TOKEN=token, POCKETHOST_TOKEN=token)
  return env


def power(instance):
  """Read the live power state through phost when that control helper exists.

  The v5 CLI cannot report power, and it never changes power either. `phost`
  is a personal-machine helper, so a runner without it records `None` and the
  deploy simply does not restore a state it never disturbed.
  """
  if shutil.which("phost") is None:
    return None
  try:
    result = subprocess.run(["phost", "status", instance], capture_output=True,
                            text=True, timeout=120)
    record, _ = json.JSONDecoder().raw_decode(result.stdout.lstrip())
  except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
    raise DeployError("Instance power state was not confirmed") from exc
  if (result.returncode or not isinstance(record, dict)
      or record.get("subdomain") != instance
      or type(record.get("power")) is not bool):
    raise DeployError("Invalid instance identity or power state")
  return record["power"]


def restore_power(instance, original):
  args = ["on" if original else "off", instance, "--yes"]
  subprocess.run(["phost", *args], capture_output=True, text=True, timeout=240)
  if power(instance) != original:
    raise DeployError("Failed to restore original power state")


def health(instance):
  for attempt in range(12):
    try:
      request = urllib.request.Request(BASE.format(instance=instance) + "/api/health",
                                       headers={"User-Agent": USER_AGENT})
      opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
      with opener.open(request, timeout=15) as response:
        payload = json.loads(response.read())
      if isinstance(payload, dict) and payload.get("code") == 200:
        return
    except (OSError, ValueError, urllib.error.URLError):
      pass
    if attempt < 11:
      time.sleep(5)
  raise DeployError("Instance did not become healthy within the bounded checks")


def validate_plan(plan, files):
  """v5 answers `{missing: [...]}`; an unexpected shape never authorizes a publish."""
  if not isinstance(plan, dict) or not isinstance(plan.get("missing"), list):
    raise DeployError("Check response lacks a well-formed missing-hash list")
  known = set(files.values())
  if any(not isinstance(h, str) or h not in known for h in plan["missing"]):
    raise DeployError("Check response has an invalid missing-hash list")
  extra = set(plan) - {"missing"}
  if extra:
    raise DeployError("Check response carries unexpected keys")


def check(instance, token, files):
  body = json.dumps({"files": files}).encode()
  request = urllib.request.Request(
    BASE.format(instance=instance) + "/api/_c/deploy/v1/check", data=body,
    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
             "User-Agent": USER_AGENT})
  # Never forward the bearer token through an environment-selected proxy/redirect.
  opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
  try:
    with opener.open(request, timeout=20) as response:
      raw = response.read(8 * 1024 * 1024 + 1)
  except (OSError, urllib.error.URLError) as exc:
    raise DeployError("Deploy scope check failed") from exc
  if len(raw) > 8 * 1024 * 1024:
    raise DeployError("HTTP response exceeds the size limit")
  try:
    plan = json.loads(raw)
  except ValueError as exc:
    raise DeployError("Deploy scope check returned invalid JSON") from exc
  validate_plan(plan, files)


def link(instance, cwd, env):
  try:
    result = subprocess.run(
      ["pockethost", "link", BASE.format(instance=instance), "--token-stdin",
       "--name", "deploy"],
      cwd=cwd, env=env, capture_output=True, text=True, timeout=120,
      input=env["P1_TOKEN"])
  except (OSError, subprocess.TimeoutExpired) as exc:
    raise DeployError("PocketHost login failed") from exc
  if result.returncode:
    raise DeployError("PocketHost login failed")


def deploy(instance, source):
  token = os.environ.get("POCKETHOST_TOKEN", "").strip()
  if not token:
    raise DeployError("POCKETHOST_TOKEN is required")
  if (instance == "avrec" or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", instance)
      or not token.startswith("pbt_")):
    raise DeployError("Instance must be a plain subdomain and the token a pbt_ API token")
  source = Path(source).resolve()
  for name in ROOTS:
    folder = source / name
    if folder.is_symlink() or not folder.is_dir():
      raise DeployError(f"Required code folder missing or linked: {name}")
    if any(p.is_symlink() for p in folder.rglob("*")):
      raise DeployError("Source code folders must not contain symlinks")
  with tempfile.TemporaryDirectory(prefix="pockethost-code-") as tmp:
    snapshot = Path(tmp) / "source"
    snapshot.mkdir()
    for name in ROOTS:
      shutil.copytree(source / name, snapshot / name, symlinks=True)
    if any(p.is_symlink() for p in snapshot.rglob("*")):
      raise DeployError("Source changed to contain a symlink while snapshotting")
    for path in snapshot.rglob("*"):
      if path.is_file() and platform_path(path.relative_to(snapshot).as_posix()):
        path.unlink()  # Omit copied provider files; never alter their source or live copy.
    env = dict(environment(token), POCKETHOST_HOME=str(Path(tmp) / "cli-home"))
    # --out is local only; the CLI, not a parallel glob, determines the exact set.
    # v5 always excludes pb_data, so the main database cannot be shipped.
    cli(["deploy", "--out", str(Path(tmp) / "stage"), "--no-node-modules",
         "--include", *INCLUDES], snapshot, env)
    stage = Path(tmp) / "stage"
    for name in ROOTS:
      (stage / name).mkdir(exist_ok=True)
    for name in PRESERVED.get(instance, ()):
      if not code_path(name) or (stage / name).exists():
        raise DeployError("Private remote file conflicts with repository ownership")
      destination = Path(tmp) / "preserved"
      cli(["pull", name, "--dir", str(destination), "--instance", instance], stage, env)
      original = destination / name
      if original.is_symlink() or not original.is_file():
        raise DeployError("Private remote file was not retrieved")
      shutil.copyfile(original, stage / name)
    files = manifest(stage)
    if any(path.startswith("pb_data/") for path in files):
      raise DeployError("The deploy set must never contain pb_data")
    original = power(instance)
    link(instance, stage, env)
    check(instance, token, files)
    if manifest(stage) != files:
      raise DeployError("Stage changed after the scope check")
    known_outcome = True
    try:
      cli(["deploy", "deploy", "--no-node-modules", "--include", *INCLUDES],
          stage, env, publishing=True)
    except (UncertainDeploy, KeyboardInterrupt, SystemExit):
      known_outcome = False
      raise
    finally:
      if known_outcome and original is not None:
        current = power(instance)
        if current != original:
          restore_power(instance, original)
    if original:
      health(instance)
    print(f"Deployed code to {instance}; original power state preserved")


if __name__ == "__main__":
  try:
    if len(sys.argv) != 3:
      raise DeployError("Usage: deploy-code.py INSTANCE SOURCE_ROOT")
    deploy(sys.argv[1], sys.argv[2])
  except DeployError as exc:
    print(f"Deployment refused: {exc}", file=sys.stderr)
    sys.exit(1)
