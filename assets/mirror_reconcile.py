"""Daily reconciler for gitlab -> github mirroring.

Runs on the same scheduled pipeline as make_widgets.py.

Actions per gitlab repo owned by / in namespace `zenham`:
  - If github twin missing:   create + initial-push all refs + add gitlab push mirror
  - If github twin exists:    PATCH visibility to match gitlab, ensure push mirror configured
  - If push mirror missing:   POST it
  - If push mirror failing
    on auth:                  delete + re-POST so the URL picks up the current GH_TOKEN
                              (gitlab's mirror API cannot update an existing mirror's url)

Set ROTATE_MIRRORS=1 to force that delete + re-POST for every repo, not just the ones
already observed failing. Do this once after rotating the github PAT - mirrors that
haven't been pushed to since the rotation still look healthy but hold the dead token.

Aborts early (exit 1) if the github token is rejected, rather than mistaking an error
response for "github has zero repos" and trying to recreate everything.

Idempotent. No-op when everything already matches. Safe to re-run.

Not synced: issues, MRs/PRs, wiki, labels, CI configs, comments, deletions.
Deletions on gitlab do NOT delete on github (safety).

Full architecture doc: see MIRRORING.md at root of zenham/zenham.
"""
import os, sys, json, subprocess, tempfile, shutil, time, datetime
import urllib.request, urllib.error

GH_TOKEN = os.environ['GH_TOKEN']       # github PAT, scopes: repo + delete_repo
GL_TOKEN = os.environ['GITLAB_TOKEN']   # gitlab PAT, scopes: api + write_repository
GH_USER  = 'zen-ham'
GL_USER  = 'zenham'
NAME_MAP = {'zenham': 'zen-ham'}        # gitlab_path -> github_name (profile repo naming quirk)
EXCLUDE  = set()                        # gitlab paths to skip entirely (never mirror)

# Set ROTATE_MIRRORS=1 to delete+recreate EVERY push mirror so each URL picks up the
# current GH_TOKEN. Needed after rotating the github PAT, because gitlab's mirror API
# has no way to update the url of an existing mirror (PUT takes no `url` attribute).
ROTATE_MIRRORS = os.environ.get('ROTATE_MIRRORS', '') == '1'
EXPIRY_WARN_DAYS = 14                   # warn when the github PAT is nearly expired

LAST_HEADERS = {}                       # response headers of the most recent api() call

def api(url, headers, method='GET', data=None):
    body = json.dumps(data).encode() if data is not None else None
    if body: headers = {**headers, 'Content-Type': 'application/json'}
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
            LAST_HEADERS.clear(); LAST_HEADERS.update({k.lower(): v for k, v in r.headers.items()})
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read()
        LAST_HEADERS.clear(); LAST_HEADERS.update({k.lower(): v for k, v in e.headers.items()})
        try: parsed = json.loads(raw) if raw else None
        except: parsed = raw.decode(errors='replace')
        return e.code, parsed

def gh(path, method='GET', data=None):
    return api(f'https://api.github.com{path}',
               {'Authorization': f'token {GH_TOKEN}', 'Accept': 'application/vnd.github+json'},
               method, data)
def gl(path, method='GET', data=None):
    return api(f'https://gitlab.com/api/v4{path}',
               {'PRIVATE-TOKEN': GL_TOKEN}, method, data)

def gl_all(path):
    out, page = [], 1
    while True:
        sep = '&' if '?' in path else '?'
        code, d = gl(f'{path}{sep}per_page=100&page={page}')
        if code >= 300 or not d: break
        out += d
        if len(d) < 100: break
        page += 1
        if page > 20: break
    return out

def gh_all(path):
    """Paginated github GET. Returns (ok, items). ok=False means the API errored -
    callers must NOT treat that as 'zero repos', or the reconciler would try to
    re-create every repo that already exists on github."""
    out, page = [], 1
    while True:
        sep = '&' if '?' in path else '?'
        code, d = gh(f'{path}{sep}per_page=100&page={page}')
        if code >= 300 or not isinstance(d, list):
            return False, d
        out += d
        if len(d) < 100: break
        page += 1
        if page > 20: break
    return True, out

def gh_preflight():
    """Validate GH_TOKEN before touching anything. Returns True if usable."""
    code, me = gh('/user')
    if code >= 300 or not isinstance(me, dict) or not me.get('login'):
        msg = me.get('message') if isinstance(me, dict) else me
        print(f'  FATAL: github auth failed ({code}): {msg}', flush=True)
        print( '  GH_MIRROR_TOKEN is expired, revoked, or lacks scopes.', flush=True)
        print( '  Fix: mint a new PAT at github.com/settings/tokens/new (scopes: repo, delete_repo),', flush=True)
        print( '       set it as GH_MIRROR_TOKEN at gitlab.com/zenham/zenham/-/settings/ci_cd,', flush=True)
        print( '       then re-run this pipeline once with ROTATE_MIRRORS=1 to refresh mirror URLs.', flush=True)
        print( '  See MIRRORING.md -> "Rotating GH_MIRROR_TOKEN".', flush=True)
        return False

    login = me.get('login')
    if login != GH_USER:
        print(f'  WARN: token belongs to github user {login!r}, expected {GH_USER!r}', flush=True)

    exp = LAST_HEADERS.get('github-authentication-token-expiration')
    if exp:
        print(f'  github: authenticated as {login}, token expires {exp}', flush=True)
        try:
            # header looks like "2026-08-06 06:00:00 UTC" or an ISO timestamp
            stamp = exp.replace(' UTC', '+0000').replace('Z', '+0000')
            for fmt in ('%Y-%m-%d %H:%M:%S%z', '%Y-%m-%dT%H:%M:%S%z'):
                try: when = datetime.datetime.strptime(stamp, fmt); break
                except ValueError: when = None
            if when:
                days = (when - datetime.datetime.now(datetime.timezone.utc)).days
                if days <= EXPIRY_WARN_DAYS:
                    print(f'  WARN: github token expires in {days} day(s) - rotate it soon,', flush=True)
                    print( '        or the whole mirror rig silently stops. See MIRRORING.md.', flush=True)
        except Exception:
            pass
    else:
        print(f'  github: authenticated as {login} (token has no expiry)', flush=True)
    return True

def run(cmd, timeout=1200):
    r = subprocess.run(cmd, capture_output=True, timeout=timeout)
    return r.returncode, r.stdout.decode('utf-8', errors='replace'), r.stderr.decode('utf-8', errors='replace')

def initial_push(gl_path_ns, gh_name):
    """Bare-clone from gitlab, push --mirror --force to github. Used only for net-new repos."""
    tmp = tempfile.mkdtemp(prefix='reconcile_')
    try:
        clone_url = f'https://oauth2:{GL_TOKEN}@gitlab.com/{gl_path_ns}.git'
        push_url  = f'https://x-access-token:{GH_TOKEN}@github.com/{GH_USER}/{gh_name}.git'
        rc, out, err = run(['git', 'clone', '--mirror', '--quiet', clone_url, tmp])
        if rc != 0: return False, f'clone: {err.strip()[:200]}'
        rc, out, err = run(['git', '-C', tmp, 'push', '--mirror', '--force', push_url])
        if rc != 0: return False, f'push: {err.strip()[:200]}'
        return True, 'ok'
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

def get_github_mirror(pid):
    """Return the github push-mirror dict for a project, or None."""
    code, mirrors = gl(f'/projects/{pid}/remote_mirrors')
    if code >= 300 or not isinstance(mirrors, list): return None
    for m in mirrors:
        if 'github.com' in (m.get('url') or ''): return m
    return None

def mirror_is_broken(m):
    """True when a mirror's last run failed on auth - i.e. its URL holds a stale token."""
    if (m.get('update_status') or '') != 'failed': return False
    err = (m.get('last_error') or '').lower()
    return ('authentication failed' in err or 'invalid username or token' in err
            or 'password authentication' in err or '403' in err or '401' in err)

def delete_mirror(pid, mirror_id):
    code, _ = gl(f'/projects/{pid}/remote_mirrors/{mirror_id}', 'DELETE')
    return code < 300

def add_github_mirror(pid, gh_name):
    mirror_url = f'https://{GH_USER}:{GH_TOKEN}@github.com/{GH_USER}/{gh_name}.git'
    code, resp = gl(f'/projects/{pid}/remote_mirrors', 'POST', {
        'url': mirror_url, 'enabled': True,
        'keep_divergent_refs': False, 'only_protected_branches': False,
    })
    return code < 300, resp

def main():
    print('=== gitlab -> github reconcile ===', flush=True)
    if ROTATE_MIRRORS:
        print('  ROTATE_MIRRORS=1: every push mirror will be recreated with the current token', flush=True)

    if not gh_preflight():
        return 1

    gl_projects = gl_all('/projects?membership=true')
    gl_projects = [p for p in gl_projects
                   if (p.get('path_with_namespace') or '').startswith(f'{GL_USER}/')
                   and p.get('path') not in EXCLUDE]
    print(f'  gitlab: {len(gl_projects)} repos in scope', flush=True)
    if not gl_projects:
        print('  FATAL: gitlab returned no repos in scope - refusing to run (WIDGETS_TOKEN bad?)', flush=True)
        return 1

    ok, gh_projects = gh_all('/user/repos?affiliation=owner')
    if not ok:
        print(f'  FATAL: github repo listing failed: {gh_projects}', flush=True)
        return 1
    gh_by_name = {r['name'].lower(): r for r in gh_projects}
    print(f'  github: {len(gh_by_name)} owned repos', flush=True)

    created = fixed_vis = mirrors_added = mirrors_rotated = skipped = failed = 0
    for p in sorted(gl_projects, key=lambda x: x['path']):
        gl_name  = p['path']
        gh_name  = NAME_MAP.get(gl_name, gl_name)
        private  = p.get('visibility') != 'public'
        pid      = p['id']

        gh_repo = gh_by_name.get(gh_name.lower())

        # 1. create github repo if missing (net-new gitlab repo)
        if gh_repo is None:
            print(f'  [new] {gl_name} -> github/{gh_name} (private={private})', flush=True)
            code, resp = gh('/user/repos', 'POST', {
                'name': gh_name, 'private': private, 'has_issues': True,
                'description': (p.get('description') or '')[:350],
            })
            if code >= 300:
                print(f'    ERR create: {resp}', flush=True); failed += 1; continue
            time.sleep(1)
            ok, err = initial_push(p['path_with_namespace'], gh_name)
            if not ok:
                print(f'    ERR initial-push: {err}', flush=True); failed += 1; continue
            # align default_branch
            db = p.get('default_branch') or 'master'
            gh(f'/repos/{GH_USER}/{gh_name}', 'PATCH', {'default_branch': db})
            created += 1
            gh_repo = resp
            gh_by_name[gh_name.lower()] = resp

        # 2. sync visibility (private/public flip)
        else:
            if gh_repo.get('private') != private:
                code, _ = gh(f'/repos/{GH_USER}/{gh_name}', 'PATCH', {'private': private})
                if code < 300:
                    print(f'  [vis]  {gh_name}: -> private={private}', flush=True); fixed_vis += 1
                else:
                    print(f'    ERR patch visibility: {code}', flush=True); failed += 1

        # 3. ensure gitlab push mirror exists and holds a working token.
        #    gitlab has no "update mirror url" API, so refreshing a stale embedded
        #    token means delete + re-POST.
        mirror = get_github_mirror(pid)
        if mirror is not None and (ROTATE_MIRRORS or mirror_is_broken(mirror)):
            why = 'rotate' if ROTATE_MIRRORS else 'auth-failed'
            if delete_mirror(pid, mirror['id']):
                mirror = None
                rotating = True
            else:
                print(f'    ERR delete stale mirror ({why}): {gl_name}', flush=True)
                failed += 1
                rotating = False
        else:
            rotating = False

        if mirror is None:
            ok, resp = add_github_mirror(pid, gh_name)
            if ok:
                if rotating:
                    print(f'  [mir~] {gl_name}: push-mirror recreated with current token', flush=True)
                    mirrors_rotated += 1
                else:
                    print(f'  [mir+] {gl_name}: push-mirror added', flush=True)
                    mirrors_added += 1
            else:
                print(f'    ERR add mirror: {resp}', flush=True); failed += 1
        else:
            skipped += 1

    print()
    print('=== SUMMARY ===')
    print(f'  new github repos created: {created}')
    print(f'  visibility flips applied: {fixed_vis}')
    print(f'  push-mirrors added:       {mirrors_added}')
    print(f'  push-mirrors rotated:     {mirrors_rotated}')
    print(f'  already in sync:          {skipped}')
    print(f'  failures:                 {failed}')
    return 0 if failed == 0 else 1

if __name__ == '__main__':
    sys.exit(main())
