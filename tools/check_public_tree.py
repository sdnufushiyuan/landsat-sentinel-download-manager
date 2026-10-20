"""Check tracked/public source for private artifacts and obvious credentials."""
import ipaddress
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import urlsplit

ROOT=Path(__file__).resolve().parents[1]
DOCUMENTATION_NETWORKS=[ipaddress.ip_network(n) for n in ('192.0.2.0/24','198.51.100.0/24','203.0.113.0/24')]


def issues(path, data):
    found=[]
    name=path.as_posix()
    if '.local.' in path.name or path.name.startswith('.env'):
        found.append('private local configuration')
    if any(part in {'private','secrets','runs','ROI','data','.venv','__pycache__'} for part in path.parts):
        found.append('private/runtime directory')
    if path.suffix.lower() in {'.tif','.tiff','.shp','.zip','.pem','.key','.sqlite','.db'}:
        found.append('data, credential or binary artifact')
    for pattern,label in [
      (r'-----BEGIN (?:OPENSSH |RSA |EC |DSA )?PRIVATE KEY-----','private key'),
      (r'\b(?:ghp_|github_pat_)[A-Za-z0-9_]{20,}','GitHub token'),
      (r'\bAIza[0-9A-Za-z_-]{30,}','Google API key'),
      (r'\bya29\.[0-9A-Za-z_-]{20,}','OAuth access token'),
      (r'"refresh_token"\s*:\s*"[^"\n]{8,}"','OAuth refresh token'),
      (r'(?i)["\'](?:password|api_secret|secret|authorization_code)["\']\s*:\s*["\'](?!REPLACE_|example|fake|test|\$)[A-Za-z0-9+/=_-]{20,}["\']','literal secret')]:
        if re.search(pattern,data):found.append(label)
    for url in re.findall(r'https?://[^\s"\']+[?&](?:token|signature|X-Goog-Signature)=[^\s"\']*',data):
        host=urlsplit(url).hostname or ''
        if not (host.endswith('.invalid') or host.endswith('.example.org') or host=='example.org'):
            found.append('signed URL')
    for raw in re.findall(r'(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])',data):
        try:address=ipaddress.ip_address(raw)
        except ValueError:continue
        if not (address.is_loopback or address.is_unspecified or any(address in network for network in DOCUMENTATION_NETWORKS)):
            found.append('non-example IPv4 address')
    for email in re.findall(r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}',data):
        if not email.endswith(('@example.org','@example.com','@example.net','@users.noreply.github.com')):
            found.append('non-example email address')
    return sorted(set(found))


def main():
    git=subprocess.run(['git','ls-files','-z'],cwd=ROOT,capture_output=True)
    if git.returncode==0:
        paths=[Path(p) for p in git.stdout.decode('utf-8').split('\0') if p]
    else:
        paths=[p.relative_to(ROOT) for p in ROOT.rglob('*') if p.is_file() and not any(
            part in {'.git','__pycache__','.venv','build','dist'} or part.endswith('.egg-info') for part in p.relative_to(ROOT).parts)]
    failures=[]
    for path in paths:
        if (ROOT/path).is_symlink():failures.append((path,['symbolic link']));continue
        try:data=(ROOT/path).read_text(encoding='utf-8')
        except UnicodeError:failures.append((path,['binary artifact']));continue
        found=issues(path,data)
        if found:failures.append((path,found))
    for path,found in failures:print(str(path)+': '+', '.join(found))
    if failures:return 1
    print(f'Public tree check passed ({len(paths)} files). This complements manual review; it is not a complete secret detector.')
    return 0


if __name__=='__main__':raise SystemExit(main())
