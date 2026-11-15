#!/usr/bin/env python3
"""Deploy only the three code folders through PocketHost CLI 4.3.0."""
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
USER_AGENT = "pockethost-code-deploy/1.0"


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
    if name == ".pockethostconfig":
      continue  # Private CLI root anchor, never part of its deployment includes.
    if p.is_file():
      if not code_path(name):
        raise DeployError("Stage contains a file outside the three code folders")
      files[name] = hashlib.sha256(p.read_bytes()).hexdigest()
    elif name not in ROOTS and not code_path(name):
      raise DeployError("Stage contains an unexpected directory")
  return files


class NoRedirect(urllib.request.HTTPRedirectHandler):
  def redirect_request(self, req, fp, code, msg, headers, newurl):
    raise DeployError("HTTP redirects are forbidden")


def request_json(url, token=None, body=None):
  headers = {"User-Agent": USER_AGENT}
  if token:
    headers["Authorization"] = f"Bearer {token}"
  data = None
  if body is not None:
    data = json.dumps(body).encode()
    headers["Content-Type"] = "application/json"
  req = urllib.request.Request(url, data=data, headers=headers)
  # Never forward the bearer token through an environment-selected proxy/redirect.
  opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
  try:
    with opener.open(req, timeout=15) as response:
      raw = response.read(8 * 1024 * 1024 + 1)
      if len(raw) > 8 * 1024 * 1024:
        raise DeployError("HTTP response exceeds the size limit")
      return json.loads(raw)
  except (OSError, ValueError) as exc:
    raise DeployError("HTTP request failed or returned invalid JSON") from exc


def validate_plan(plan, files):
  if not isinstance(plan, dict) or not isinstance(plan.get("toDelete"), list):
    raise DeployError("Check response lacks a well-formed toDelete list")
  if any(not code_path(p) for p in plan["toDelete"]):
    raise DeployError("Remote deletion plan escapes the three code folders")
  missing = plan.get("missing")
  if (not isinstance(missing, list)
      or any(not isinstance(h, str) or h not in set(files.values()) for h in missing)):
    raise DeployError("Check response has an invalid missing-hash list")


def cli(args, cwd, env):
  env = dict(env, POCKETHOST_PROJECT_DIR=str(cwd), INIT_CWD=str(cwd))
  publishing = args[0] == "deploy" and "--out" not in args
  try:
    result = subprocess.run(["pockethost", *args], cwd=cwd, env=env,
                            capture_output=True, text=True, timeout=240)
  except (OSError, subprocess.TimeoutExpired) as exc:
    if publishing:
      raise UncertainDeploy("Deployment outcome unknown; inspect before changing power") from exc
    raise DeployError("PocketHost CLI unavailable or timed out") from exc
  if result.returncode and publishing:
    raise UncertainDeploy("Deployment outcome unknown; inspect before changing power")
  if result.returncode or "Instance failed to start:" in result.stdout + result.stderr:
    # CLI errors may contain remote text. Do not echo them or credentials.
    raise DeployError("PocketHost CLI failed (including possible instance startup failure)")
  return result.stdout


def power(instance, cwd, env):
  try:
    info = json.loads(cli(["show", instance, "--json"], cwd, env))
  except ValueError as exc:
    raise DeployError("Invalid show JSON") from exc
  if (not isinstance(info, dict) or info.get("subdomain") != instance
      or not isinstance(info.get("id"), str) or not info["id"]
      or type(info.get("power")) is not bool
      or not isinstance(info.get("status"), str)):
    raise DeployError("Invalid instance identity or power state")
  return info["power"]


def health(instance):
  for attempt in range(12):
    try:
      result = request_json(f"https://{instance}.pockethost.io/api/health")
      if isinstance(result, dict) and type(result.get("code")) is int and result["code"] == 200:
        return
    except DeployError:
      pass
    if attempt < 11:
      time.sleep(5)
  raise DeployError("Instance did not become healthy within the bounded checks")


def deploy(instance, source):
  token = os.environ.get("POCKETHOST_TOKEN", "")
  if not token.strip():
    raise DeployError("POCKETHOST_TOKEN is required")
  if instance == "avrec" or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", instance):
    raise DeployError("Instance must be a plain subdomain")
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
    anchor = json.dumps({"instanceName": instance})
    (snapshot / ".pockethostconfig").write_text(anchor)
    stage = Path(tmp) / "stage"
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("POCKETHOST_", "PHIO_"))
           and not k.lower().endswith("_proxy") and k != "NODE_OPTIONS"}
    env.update(POCKETHOST_TOKEN=token,
               POCKETHOST_MOTHERSHIP_URL="https://mothership.pockethost.io",
               POCKETHOST_HOME=str(Path(tmp) / "cli-home"))
    # --out is local only; the CLI, rather than a parallel glob implementation,
    # determines the exact files. Its defaults may add files: validate all of them.
    cli(["deploy", instance, "--out", str(stage), "--no-node-modules",
         "--include", *INCLUDES], snapshot, env)
    (stage / ".pockethostconfig").write_text(anchor)
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
    original = power(instance, stage, env)
    plan = request_json(f"https://{instance}.pockethost.io/_ph/deploy/v1/check",
                        token, {"files": files, "nodeModules": False})
    validate_plan(plan, files)
    if manifest(stage) != files:
      raise DeployError("Stage changed after the scope check")
    known_outcome = True
    try:
      cli(["deploy", instance, "--yes", "--no-node-modules",
           "--include", *INCLUDES], stage, env)
    except (UncertainDeploy, KeyboardInterrupt, SystemExit):
      known_outcome = False
      raise
    finally:
      if known_outcome:
        current = power(instance, stage, env)
        if current != original:
          args = ["power", "on" if original else "off", instance]
          if not original:
            args.append("--wait")
          cli(args, stage, env)
          if power(instance, stage, env) != original:
            raise DeployError("Failed to restore original power state")
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
