"""PocketBase Backups API transport for complete pb_data snapshots."""
import json
import os
from pathlib import Path
import shutil
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile

from . import pbwrite as W
from .offline import MutationUncertain

MAX_UPLOAD = 90 * 1024 * 1024
R2_ENDPOINT = 'https://4d0bb8ab7e61809c181b815854ecd8d8.r2.cloudflarestorage.com'
R2_BUCKET = 'pb-backup-stage'
INSTANCE_IDS = {'selfhosted': '2sb1xkspblek1f5', 'bkptest': '5kvbuh7ntjbz6la'}
_KEY = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,180}\.zip\Z')


class APIError(RuntimeError):
  def __init__(self, message, status=None):
    super().__init__(message)
    self.status = status


class BackupAPITransport:
  """Use the instance automation superuser; never powers the instance off."""
  upload_cap = MAX_UPLOAD

  def __init__(self, instance='selfhosted', base=None, opener=None):
    if not isinstance(instance, str) or not re.fullmatch(r'[a-z0-9-]{1,63}', instance):
      raise ValueError('invalid PocketHost instance name')
    expected = f'https://{instance}.pockethost.io'
    base = (base or expected).rstrip('/')
    if base != expected:
      raise ValueError('backup API host must match the selected PocketHost instance')
    self.instance, self.base = instance, base
    self.identity = {'transport': 'pb-backups-api', 'instance': instance, 'base': base}
    try:
      import sys
      helper = str(Path.home() / 'dotfiles/python')
      sys.path.insert(0, helper)
      try:
        from pb_credentials import resolve
        selected = resolve(instance)
      finally:
        sys.path.remove(helper)
      if selected.url.rstrip('/') != base:
        raise ValueError('resolved credential target mismatch')
      self.api = W.PB(base, email=selected.email, password=selected.password, quiet=True)
    except Exception:
      raise RuntimeError('instance automation credentials unavailable') from None
    self.uncertain = False
    self._marker = None
    self.r2_active = False
    self.opener = opener or urllib.request.build_opener(_NoRedirect())

  def configure(self, path):
    self._marker = Path(str(path) + '.backup-mutation.json')
    if self._marker.exists():
      record = json.loads(self._marker.read_text())
      if (record.get('version') != 1 or record.get('transport') != self.identity
          or type(record.get('pending')) is not bool):
        raise RuntimeError('invalid backup API mutation marker')
      self.uncertain = record['pending']
    if self.uncertain:
      raise MutationUncertain('unfinished PocketBase mutation requires operator reconciliation')
    settings = self.api._req('GET', '/api/settings')
    s3 = (settings.get('backups') or {}).get('s3') if isinstance(settings, dict) else None
    if isinstance(s3, dict) and s3.get('enabled'):
      access = os.environ.get('CLOUDFLARE_R2_ACCESS', '')
      if (s3.get('endpoint') != R2_ENDPOINT or s3.get('bucket') != R2_BUCKET
          or not access or s3.get('accessKey') != access):
        raise RuntimeError('unexpected backup S3 settings require operator review')
      self.r2_active = True

  def _mark(self, operation, action):
    if self.uncertain:
      raise MutationUncertain('PocketBase mutation outcome uncertain; operator reconciliation required')
    record = {'version': 1, 'transport': self.identity, 'pending': True, 'operation': operation}
    if self._marker is None:
      raise RuntimeError('API mutation requires a recorded journal context')
    W._save(self._marker, record)
    self.uncertain = True
    try:
      result = action()
    except W.PBError as exc:
      if 400 <= exc.status < 500:
        W._save(self._marker, dict(record, pending=False, response_status=exc.status))
        self.uncertain = False
      raise
    except BaseException:
      raise MutationUncertain('PocketBase mutation outcome uncertain; operator reconciliation required') from None
    W._save(self._marker, dict(record, pending=False))
    self.uncertain = False
    return result

  @staticmethod
  def _key(key):
    if not isinstance(key, str) or not _KEY.fullmatch(key):
      raise ValueError('invalid PocketBase backup key')
    return key

  def is_on(self):
    out = subprocess.run(['phost', 'status', self.instance], text=True,
                         capture_output=True, timeout=180, check=True).stdout
    try:
      record, _ = json.JSONDecoder().raw_decode(out.lstrip())
    except ValueError:
      raise RuntimeError('instance power state was not confirmed') from None
    expected_id = INSTANCE_IDS.get(self.instance)
    if (not isinstance(record, dict) or record.get('subdomain') != self.instance
        or (expected_id and record.get('id') != expected_id)
        or type(record.get('power')) is not bool):
      raise RuntimeError('instance power state was not confirmed')
    return record['power']

  def _keys(self):
    rows = self.api._req('GET', '/api/backups')
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
      raise APIError('invalid remote backup listing')
    return {row.get('key') for row in rows if isinstance(row.get('key'), str)}

  def create_backup(self, key):
    key = self._key(key)
    if key in self._keys():
      raise RuntimeError('refusing to overwrite a retained remote backup')
    self._mark('create-backup:' + key,
               lambda: self.api._req('POST', '/api/backups', {'name': key}))
    if key not in self._keys():
      raise APIError('created backup was not confirmed remotely')
    return key

  def _file_token(self):
    result = self._mark('file-token', lambda: self.api._req('POST', '/api/files/token'))
    token = result.get('token') if isinstance(result, dict) else None
    if not isinstance(token, str) or not token or any(ord(c) < 33 or ord(c) > 126 for c in token):
      raise APIError('file token was not confirmed')
    return token

  def download(self, key, path):
    key = self._key(key)
    token = self._file_token()
    url = (self.base + '/api/backups/' + urllib.parse.quote(key, safe='') + '?'
           + urllib.parse.urlencode({'token': token}))
    request = urllib.request.Request(url, headers={'User-Agent': W.UA['User-Agent']}, method='GET')
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.exists() or path.is_symlink():
      raise RuntimeError('refusing to overwrite a local backup')
    temp = path.with_name(path.name + '.part-' + uuid.uuid4().hex)
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
      with os.fdopen(fd, 'wb') as out, self.opener.open(request, timeout=300) as response:
        if response.status != 200:
          raise APIError(f'backup download returned HTTP {response.status}', response.status)
        while chunk := response.read(1 << 20):
          out.write(chunk)
        out.flush()
        os.fsync(out.fileno())
      if not zipfile.is_zipfile(temp):
        raise APIError('backup download is not a valid ZIP archive')
      os.replace(temp, path)
      return path
    except urllib.error.HTTPError as exc:
      status = exc.code
      exc.close()
      raise APIError(f'backup download returned HTTP {status}', status) from None
    except (urllib.error.URLError, TimeoutError, OSError):
      raise APIError('backup download did not complete') from None
    finally:
      if temp.exists():
        temp.unlink()

  def _aws_env(self):
    access, secret = os.environ.get('CLOUDFLARE_R2_ACCESS'), os.environ.get('CLOUDFLARE_R2_SECRET')
    if not access or not secret:
      raise APIError('R2 transfer credentials are unavailable')
    env = os.environ.copy()
    env.update(AWS_ACCESS_KEY_ID=access, AWS_SECRET_ACCESS_KEY=secret,
               AWS_DEFAULT_REGION='auto', AWS_EC2_METADATA_DISABLED='true')
    return env

  def _s3_uri(self, key):
    return f's3://{R2_BUCKET}/{self._key(key)}'

  def _aws_copy(self, source, destination, *, mutation=False, operation=''):
    if not shutil.which('aws'):
      raise APIError('AWS CLI is required for backups larger than 90 MiB')
    command = ['aws', '--no-cli-pager', 's3', 'cp', str(source), str(destination),
               '--endpoint-url', R2_ENDPOINT, '--only-show-errors']
    def run():
      try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=600,
                                env=self._aws_env(), check=False)
      except (OSError, subprocess.SubprocessError):
        raise APIError('R2 transfer did not complete') from None
      if result.returncode:
        raise APIError('R2 transfer failed; inspect retained backups before retrying')
    if mutation:
      self._mark(operation, run)
    else:
      run()

  @staticmethod
  def _hash(path):
    import hashlib
    with Path(path).open('rb') as stream:
      return hashlib.file_digest(stream, 'sha256').hexdigest()

  def configure_r2(self):
    settings = self.api._req('GET', '/api/settings')
    current = (settings.get('backups') or {}).get('s3') if isinstance(settings, dict) else None
    if not isinstance(current, dict):
      raise APIError('backup S3 settings could not be inspected')
    if current.get('enabled') or current.get('accessKey') or current.get('endpoint') or current.get('bucket'):
      raise APIError('refusing to overwrite existing backup S3 settings')
    env = self._aws_env()
    config = {'enabled': True, 'endpoint': R2_ENDPOINT, 'region': 'auto',
              'bucket': R2_BUCKET, 'accessKey': env['AWS_ACCESS_KEY_ID'],
              'secret': env['AWS_SECRET_ACCESS_KEY'], 'forcePathStyle': True}
    self._mark('enable-temporary-r2',
               lambda: self.api._req('PATCH', '/api/settings', {'backups': {'s3': config}}))
    self.r2_active = True
    verified = self.api._req('GET', '/api/settings')['backups']['s3']
    if (verified.get('enabled') is not True or verified.get('endpoint') != R2_ENDPOINT
        or verified.get('bucket') != R2_BUCKET or verified.get('accessKey') != env['AWS_ACCESS_KEY_ID']):
      raise APIError('temporary R2 settings were not confirmed')

  def enable_r2(self, original_key, original_zip, directory):
    self.configure_r2()
    original_zip = Path(original_zip)
    self._aws_copy(original_zip, self._s3_uri(original_key), mutation=True,
                   operation='retain-original-in-r2:' + original_key)
    check = Path(directory) / 'r2-original-check.zip'
    if check.exists():
      raise APIError('R2 verification destination already exists')
    self._aws_copy(self._s3_uri(original_key), check)
    if self._hash(check) != self._hash(original_zip):
      raise APIError('R2 original backup hash mismatch')

  def clear_r2(self):
    if not self.r2_active:
      return
    empty = {'enabled': False, 'endpoint': '', 'region': '', 'bucket': '',
             'accessKey': '', 'secret': '', 'forcePathStyle': False}
    self._mark('clear-temporary-r2',
               lambda: self.api._req('PATCH', '/api/settings', {'backups': {'s3': empty}}))
    settings = self.api._req('GET', '/api/settings')['backups']['s3']
    if ('secret' not in settings or settings.get('secret') or settings.get('enabled')
        or settings.get('accessKey') or settings.get('endpoint') or settings.get('bucket')):
      raise APIError('temporary R2 credentials could not be verified cleared')
    self.r2_active = False

  def upload_backup(self, path, filename):
    filename = self._key(filename)
    path = Path(path)
    if path.is_symlink() or not path.is_file():
      raise ValueError('backup upload requires a regular file')
    size = path.stat().st_size
    if not size:
      raise ValueError('backup upload must not be empty')
    if size > MAX_UPLOAD and not self.r2_active:
      raise APIError('backup exceeds the HTTPS limit; configure temporary R2 first')
    if not zipfile.is_zipfile(path):
      raise ValueError('backup upload must be a valid ZIP archive')
    if size > MAX_UPLOAD:
      if not self.r2_active:
        raise APIError('backup exceeds the HTTPS limit; configure temporary R2 first')
      self._aws_copy(path, self._s3_uri(filename), mutation=True,
                     operation='upload-large-backup-to-r2:' + filename)
      check = path.with_name(path.name + '.r2-check')
      if check.exists():
        raise APIError('R2 verification destination already exists')
      self._aws_copy(self._s3_uri(filename), check)
      if self._hash(check) != self._hash(path):
        raise APIError('R2 staged backup hash mismatch')
      return filename
    boundary = '----pb-backup-' + uuid.uuid4().hex
    prefix = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
              f'filename="{filename}"\r\nContent-Type: application/zip\r\n\r\n').encode()
    body = prefix + path.read_bytes() + f'\r\n--{boundary}--\r\n'.encode()
    request = urllib.request.Request(self.base + '/api/backups/upload', data=body,
      headers={'User-Agent': W.UA['User-Agent'], 'Authorization': self.api.token,
               'Content-Type': f'multipart/form-data; boundary={boundary}',
               'Accept': 'application/json'}, method='POST')
    def send():
      try:
        with self.opener.open(request, timeout=300) as response:
          raw = response.read()
          return json.loads(raw) if raw.strip() else None
      except urllib.error.HTTPError as exc:
        status = exc.code
        exc.close()
        raise W.PBError(status, {}) from None
    result = self._mark('upload-backup:' + filename, send)
    values = []
    if isinstance(result, dict):
      values.extend((result.get('key'), result.get('name')))
    for row in self.api._req('GET', '/api/backups') or []:
      if isinstance(row, dict) and filename in (row.get('key'), row.get('name')):
        values.extend((row.get('key'), row.get('name')))
    keys = {self._key(value) for value in values if isinstance(value, str)}
    if len(keys) != 1:
      raise APIError('uploaded backup key was not uniquely confirmed')
    return keys.pop()

  def restore(self, key):
    key = self._key(key)
    return self._mark('restore-backup:' + key,
      lambda: self.api._req('POST', '/api/backups/' + urllib.parse.quote(key, safe='') + '/restore'))

  def rollback_retryable(self, key):
    key = self._key(key)
    if self._marker is None or not self._marker.exists():
      return False
    record = json.loads(self._marker.read_text())
    if (record.get('version') != 1 or record.get('transport') != self.identity
        or type(record.get('pending')) is not bool or not isinstance(record.get('operation'), str)):
      raise RuntimeError('invalid backup API mutation marker')
    if record['pending']:
      raise MutationUncertain('unfinished PocketBase mutation requires operator reconciliation')
    if record['operation'] == 'restore-backup:' + key:
      return record.get('response_status') == 400
    return True

  def health(self):
    request = urllib.request.Request(self.base + '/api/health', headers=W.UA)
    try:
      with self.opener.open(request, timeout=20) as response:
        return response.status == 200 and json.load(response).get('code') == 200
    except Exception:
      return False

  def wait_healthy(self, timeout=180):
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
      if self.health():
        return True
      time.sleep(2)
    return False


class _NoRedirect(urllib.request.HTTPRedirectHandler):
  def redirect_request(self, *args, **kwargs):
    return None
