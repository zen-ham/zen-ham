"""Daily reconciler for gitlab -> github mirroring.

Runs on the same scheduled pipeline as make_widgets.py.

Actions per gitlab repo owned by / in namespace `zenham`:
  - If github twin missing:   create + initial-push all refs + add gitlab push mirror
  - If github twin exists:    PATCH visibility to match gitlab, ensure push mirror configured
  - If push mirror missing:   POST it (also updates token embedded in URL when rotating)

Idempotent. No-op when everything already matches. Safe to re-run.

Not synced: issues, MRs/PRs, wiki, labels, CI configs, comments, deletions.
Deletions on gitlab do NOT delete on github (safety).

Full architecture doc: see MIRRORING.md at root of zenham/zenham.
"""
import os, sys, json, subprocess, tempfile, shutil, time
import urllib.request, urllib.error

GH_TOKEN = os.environ['GH_TOKEN']       # github PAT, scopes: repo + delete_repo
GL_TOKEN = os.environ['GITLAB_TOKEN']   # gitlab PAT, scopes: api + write_repository
GH_USER  = 'zen-ham'
GL_USER  = 'zenham'
NAME_MAP = {'zenham': 'zen-ham'}        # gitlab_path -> github_name (profile repo naming quirk)
EXCLUDE  = set()                        # gitlab paths to skip entirely (never mirror)

def api(url, headers, method='GET', data=None):
    body = json.dumps(data).encode() if data is not None else None
    if body: headers = {**headers, 'Content-Type': 'application/json'}
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read()
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

def has_github_mirror(pid):
    code, mirrors = gl(f'/projects/{pid}/remote_mirrors')
    if code >= 300 or not isinstance(mirrors, list): return False
    return any('github.com' in (m.get('url') or '') for m in mirrors)

def add_github_mirror(pid, gh_name):
    mirror_url = f'https://{GH_USER}:{GH_TOKEN}@github.com/{GH_USER}/{gh_name}.git'
    code, resp = gl(f'/projects/{pid}/remote_mirrors', 'POST', {
        'url': mirror_url, 'enabled': True,
        'keep_divergent_refs': False, 'only_protected_branches': False,
    })
    return code < 300, resp

def main():
    print('=== gitlab -> github reconcile ===', flush=True)

    gl_projects = gl_all('/projects?membership=true')
    gl_projects = [p for p in gl_projects
                   if (p.get('path_with_namespace') or '').startswith(f'{GL_USER}/')
                   and p.get('path') not in EXCLUDE]
    print(f'  gitlab: {len(gl_projects)} repos in scope', flush=True)

    _, gh_projects = gh('/user/repos?per_page=100&affiliation=owner')
    gh_by_name = {r['name'].lower(): r for r in (gh_projects or [])}
    print(f'  github: {len(gh_by_name)} owned repos', flush=True)

    created = fixed_vis = mirrors_added = skipped = failed = 0
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

        # 3. ensure gitlab push mirror exists (adds one if missing; safe idempotent for existing)
        if not has_github_mirror(pid):
            ok, resp = add_github_mirror(pid, gh_name)
            if ok:
                print(f'  [mir+] {gl_name}: push-mirror added', flush=True); mirrors_added += 1
            else:
                print(f'    ERR add mirror: {resp}', flush=True); failed += 1
        else:
            skipped += 1

    print()
    print('=== SUMMARY ===')
    print(f'  new github repos created: {created}')
    print(f'  visibility flips applied: {fixed_vis}')
    print(f'  push-mirrors added:       {mirrors_added}')
    print(f'  already in sync:          {skipped}')
    print(f'  failures:                 {failed}')
    return 0 if failed == 0 else 1

if __name__ == '__main__':
    sys.exit(main())
