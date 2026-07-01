"""Generate gitlab-readme widget SVGs for zenham. Theme: pure black bg + #00FF00 accent.

Widgets:
  widgets/stats.svg     - square gitlab stats summary card (Total Stars/Commits/MRs/Issues/Repos + grade)
  widgets/trophies.svg  - 2x2 square achievement grid (Repo Owner, Starred, Forked, Commits)
  widgets/streak.svg    - 2x2 square (Total Contributions, Current Streak, Longest Streak, Active Days) with ring
  widgets/languages.svg - donut + per-language % breakdown across all repos
  widgets/bubbles.svg   - language bubbles, area = log(total bytes) per language
  widgets/activity.svg  - line chart of events/day for last 90 days
  widgets/snake.svg     - 53x7 contribution grid
"""
import os, sys, json, urllib.request, urllib.error, time, math
from datetime import datetime, timedelta, timezone
from collections import defaultdict, Counter

USER = os.environ.get('GITLAB_USERNAME', 'zenham')
GL_TOKEN = os.environ.get('GITLAB_TOKEN')
OUT = os.environ.get('OUT_DIR', 'widgets')
os.makedirs(OUT, exist_ok=True)

T = {
    'bg':        '#000000',
    'card':      '#000000',
    'border':    '#00FF00',   # green outline on every card per user spec
    'divider':   '#00FF00',   # 2x2 internal cell dividers
    'text':      '#eaeaea',
    'text_dim':  '#888888',
    'accent':    '#00FF00',
    'accent_alt':'#3b82f6',
    'warn':      '#fbbf24',
    'err':       '#f87171',
    # contribution-grid intensity scale (low -> high)
    'grid_0':    '#0a0a0a',
    'grid_1':    '#003a00',
    'grid_2':    '#006600',
    'grid_3':    '#00b800',
    'grid_4':    '#00FF00',
}

# Tier colors per user spec
TIER_COLORS = ['#444444', '#a855f7', '#3b82f6', '#00FF00', '#fbbf24']  # Locked, Bronze, Silver, Gold, Platinum
TIER_LABELS = ['Locked', 'Bronze', 'Silver', 'Gold', 'Platinum']

# Common language colors (github-linguist defaults; extend as needed)
LANG_COLORS = {
    'JavaScript':'#f1e05a','Python':'#3572A5','TypeScript':'#3178c6','HTML':'#e34c26',
    'CSS':'#563d7c','SCSS':'#c6538c','C++':'#f34b7d','C':'#a8a8a8','Rust':'#dea584',
    'Go':'#00ADD8','Java':'#b07219','Shell':'#89e051','Ruby':'#701516','Kotlin':'#A97BFF',
    'Swift':'#F05138','PHP':'#4F5D95','Lua':'#000080','Vue':'#41b883','PowerShell':'#012456',
    'Batchfile':'#C1F12E','Makefile':'#427819','CMake':'#DA3434','Dockerfile':'#384d54',
    'GLSL':'#5686a5','HLSL':'#aace60','Cython':'#fedf5b','C#':'#178600','Jupyter Notebook':'#DA5B0B',
    'Markdown':'#083fa1','TeX':'#3D6117','YAML':'#cb171e','TOML':'#9c4221',
    'JSON':'#e6a23c','XML':'#0060ac','SVG':'#ff9900','Assembly':'#6E4C13',
    'Other':'#ad7fa8',  # muted purple — visually distinct from any real language color
}
def lang_color(name): return LANG_COLORS.get(name, '#888888')

# Fallback palette — 12 hues evenly around the color wheel at good saturation/lightness.
# Chosen so each is ~30° hue apart, avoiding the muddy cluster GitHub picks for langs.
_DISTINCT_PALETTE = [
    '#ff3b30','#ff9500','#ffcc00','#8ee34d','#34c759','#00c7be',
    '#30b0c7','#007aff','#5856d6','#af52de','#ff2d92','#a2845e',
]

def _hex_to_rgb(h):
    h = h.lstrip('#')
    return int(h[0:2],16), int(h[2:4],16), int(h[4:6],16)

def _rgb_to_hsl(r, g, b):
    r, g, b = r/255, g/255, b/255
    mx, mn = max(r,g,b), min(r,g,b)
    l = (mx + mn) / 2
    if mx == mn:
        return 0.0, 0.0, l
    d = mx - mn
    s = d / (2 - mx - mn) if l > 0.5 else d / (mx + mn)
    if   mx == r: h = ((g - b) / d) + (6 if g < b else 0)
    elif mx == g: h = ((b - r) / d) + 2
    else:         h = ((r - g) / d) + 4
    return (h * 60) % 360, s, l

def _hex_to_hsl(hx):
    return _rgb_to_hsl(*_hex_to_rgb(hx))

def _hsl_to_hex(h, s, l):
    h = h % 360 / 360
    def hue2rgb(p, q, t):
        if t < 0: t += 1
        if t > 1: t -= 1
        if t < 1/6: return p + (q - p) * 6 * t
        if t < 1/2: return q
        if t < 2/3: return p + (q - p) * (2/3 - t) * 6
        return p
    if s == 0:
        r = g = b = l
    else:
        q = l * (1 + s) if l < 0.5 else l + s - l * s
        p = 2 * l - q
        r = hue2rgb(p, q, h + 1/3)
        g = hue2rgb(p, q, h)
        b = hue2rgb(p, q, h - 1/3)
    return '#%02x%02x%02x' % (round(r*255), round(g*255), round(b*255))

def _hue_gap(h1, h2):
    d = abs(h1 - h2)
    return min(d, 360 - d)

def _colors_too_similar(c1, c2):
    """HSL-based check: colors are 'too similar' if hues are close AND lightness is close.
    Handles low-saturation edge cases (grays compared by lightness only)."""
    h1, s1, l1 = _hex_to_hsl(c1)
    h2, s2, l2 = _hex_to_hsl(c2)
    # both near-gray: compare lightness only
    if s1 < 0.15 and s2 < 0.15:
        return abs(l1 - l2) < 0.22
    # one is gray: only distinct if lightness differs enough
    if min(s1, s2) < 0.15:
        return abs(l1 - l2) < 0.20
    # both saturated: need EITHER 30°+ hue separation OR 22%+ lightness separation
    return _hue_gap(h1, h2) < 30 and abs(l1 - l2) < 0.22

def assign_distinct_colors(langs):
    """Assign each language a color that's visually distinct from all previously-assigned.
    Prefer natural color; fall back to _DISTINCT_PALETTE if conflict; synthesize by hue
    rotation if palette is exhausted."""
    assigned = {}
    used = []
    for lang in langs:
        nat = lang_color(lang)
        if all(not _colors_too_similar(nat, u) for u in used):
            chosen = nat
        else:
            chosen = None
            for fb in _DISTINCT_PALETTE:
                if all(not _colors_too_similar(fb, u) for u in used):
                    chosen = fb; break
            if chosen is None:
                # Palette exhausted — sweep hue circle for the one farthest from all used
                best_h, best_min_gap = 0, -1
                for h_try in range(0, 360, 5):
                    test = _hsl_to_hex(h_try, 0.65, 0.55)
                    ht, _, lt = _hex_to_hsl(test)
                    gap = min(_hue_gap(ht, _hex_to_hsl(u)[0]) for u in used)
                    if gap > best_min_gap:
                        best_min_gap = gap; best_h = h_try
                chosen = _hsl_to_hex(best_h, 0.65, 0.55)
        assigned[lang] = chosen
        used.append(chosen)
    return assigned

# Legacy shim — perceptual_dist still called elsewhere for brightness ops
def perceptual_dist(c1, c2):
    r1,g1,b1 = _hex_to_rgb(c1); r2,g2,b2 = _hex_to_rgb(c2)
    return ((r1-r2)**2 * 0.3 + (g1-g2)**2 * 0.59 + (b1-b2)**2 * 0.11) ** 0.5

import time as _time
def _fetch(url, headers, attempts=4):
    last = None
    for n in range(attempts):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=45) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            # 404 just means "no data", caller decides
            if e.code == 404: return None
            last = e
            _time.sleep(2 ** n)
        except Exception as e:
            last = e
            _time.sleep(2 ** n)
    raise last

def gl_get(path, paginate=False):
    url = f'https://gitlab.com/api/v4{path}'
    headers = {'PRIVATE-TOKEN': GL_TOKEN} if GL_TOKEN else {}
    if not paginate: return _fetch(url, headers)
    out, page, sep = [], 1, ('&' if '?' in path else '?')
    while True:
        batch = _fetch(f'{url}{sep}per_page=100&page={page}', headers)
        if not batch: break
        out += batch
        if len(batch) < 100: break
        page += 1
        if page > 50: break
    return out

# ============ DATA FETCH ============
print(f'fetching gitlab data for {USER}...', flush=True)
user = gl_get(f'/users?username={USER}')[0]
USER_ID = user['id']
print(f'  user_id={USER_ID}', flush=True)

projects = gl_get(f'/users/{USER_ID}/projects?statistics=true', paginate=True)
print(f'  {len(projects)} projects', flush=True)
total_stars = sum(p.get('star_count', 0) for p in projects)
total_forks = sum(p.get('forks_count', 0) for p in projects)

# Aggregate languages: bytes_for_lang = sum over repos of (repo_size * lang_pct/100)
# Used by "most used languages" widget only. Per-commit language attribution is done later
# via `git log --numstat` on cloned repos (faster + accurate + gives API a break).
print('  fetching per-repo languages...', flush=True)
lang_bytes = defaultdict(float)
for p in projects:
    pid = p['id']
    size = (p.get('statistics') or {}).get('repository_size') or 0
    if size <= 0: continue
    try:
        langs = gl_get(f'/projects/{pid}/languages')  # {Lang: pct}
    except Exception:
        continue
    if not langs: continue
    for lang, pct in langs.items():
        lang_bytes[lang] += size * (pct / 100.0)
total_lang_bytes = sum(lang_bytes.values()) or 1
print(f'  {len(lang_bytes)} distinct languages, {int(total_lang_bytes):,} bytes total', flush=True)

# Per-commit fetch across owned + contributed-to projects (so commits to collaborator
# repos like OccultMC/Zelesis_AI_Neo also count). Unlocks pre-account-creation history
# that the events API can't see.
today = datetime.now(timezone.utc).date()

# Discover all projects user has touched: owned + contributed-to
# Track path_with_namespace so we can git-clone by URL.
project_ids = {p['id'] for p in projects}
repo_name_by_pid = {p['id']: (p.get('name') or p.get('path') or f'project-{p["id"]}') for p in projects}
path_ns_by_pid  = {p['id']: p.get('path_with_namespace', '') for p in projects}
try:
    contributed_api = gl_get(f'/users/{USER_ID}/contributed_projects', paginate=True) or []
    for cp in contributed_api:
        project_ids.add(cp['id'])
        repo_name_by_pid.setdefault(cp['id'], cp.get('name') or cp.get('path') or f'project-{cp["id"]}')
        path_ns_by_pid.setdefault(cp['id'], cp.get('path_with_namespace', ''))
    print(f'  contributed_projects API added {len(contributed_api)} project entries', flush=True)
except Exception as _e:
    print(f'  contributed_projects API failed: {_e}', flush=True)

# Author email filter — user's commits could be authored by either the
# old zen-ham email or the new zenham email
AUTHOR_EMAILS = {'again.really.plz@gmail.com', 'zenmastermagnet@gmail.com', 'roeganjoe47@gmail.com'}

# ---- git-clone-based commit + language attribution ----
# For each project: bare-clone, run `git log --all --numstat` in one call, parse locally.
# Trades 1289 API calls for 26 clones — faster and easier on the API.
import subprocess, tempfile, shutil, re
EXT_TO_LANG_EARLY = {
    '.py':'Python','.pyi':'Python','.pyx':'Cython',
    '.cpp':'C++','.cc':'C++','.cxx':'C++','.hpp':'C++','.hh':'C++','.h':'C',
    '.c':'C','.ino':'C++',
    '.js':'JavaScript','.mjs':'JavaScript','.jsx':'JavaScript',
    '.ts':'TypeScript','.tsx':'TypeScript',
    '.html':'HTML','.htm':'HTML','.css':'CSS','.scss':'SCSS','.sass':'SCSS',
    '.rs':'Rust','.go':'Go','.java':'Java','.kt':'Kotlin','.swift':'Swift',
    '.sh':'Shell','.bash':'Shell','.zsh':'Shell','.ps1':'PowerShell','.bat':'Batchfile',
    '.cmake':'CMake','.md':'Markdown','.yml':'YAML','.yaml':'YAML','.toml':'TOML',
    '.json':'JSON','.xml':'XML','.svg':'SVG','.tex':'TeX','.rb':'Ruby','.lua':'Lua',
    '.glsl':'GLSL','.vert':'GLSL','.frag':'GLSL','.hlsl':'HLSL','.cs':'C#','.php':'PHP',
    '.vue':'Vue','.ipynb':'Jupyter Notebook','.asm':'Assembly','.s':'Assembly',
    '.make':'Makefile','.mk':'Makefile',
}
def _lang_of_path(path):
    if '.' not in path: return None
    ext = '.' + path.rsplit('.', 1)[-1].lower()
    return EXT_TO_LANG_EARLY.get(ext)

_COMMIT_MARKER = '\x1e__COMMIT__\x1e'  # RS-delimited so subjects/paths can never collide
_owned_pids = {p['id'] for p in projects}

all_commits = []
lang_commits = defaultdict(float)
lang_lines   = defaultdict(float)
repo_commits = Counter()
repo_lines   = Counter()
daily = Counter()
contributed_projects = set()

print(f'  cloning + parsing {len(project_ids)} repos via git...', flush=True)
_clone_start = _time.time()
for pid in project_ids:
    pns = path_ns_by_pid.get(pid) or ''
    if not pns: continue
    clone_url = (f'https://oauth2:{GL_TOKEN}@gitlab.com/{pns}.git' if GL_TOKEN
                 else f'https://gitlab.com/{pns}.git')
    tmp = tempfile.mkdtemp(prefix='zenham_clone_')
    try:
        # bare clone: no working tree, smaller footprint, same log/numstat capability
        r = subprocess.run(['git', 'clone', '--bare', '--quiet', clone_url, tmp],
                           capture_output=True, timeout=600)
        if r.returncode != 0:
            print(f'    clone failed for {pns}: {r.stderr.decode(errors="replace").strip()[:200]}', flush=True)
            continue
        r = subprocess.run(
            ['git', '-C', tmp, 'log', '--all', '--numstat', '--no-renames',
             f'--pretty=format:{_COMMIT_MARKER}%H|%aI|%ae|%s'],
            capture_output=True, timeout=300)
        if r.returncode != 0: continue
        log = r.stdout.decode('utf-8', errors='replace')
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    repo_name = repo_name_by_pid.get(pid, f'project-{pid}')
    is_owned = pid in _owned_pids
    # Parse: split on marker, first token per record = header + numstat lines
    for rec in log.split(_COMMIT_MARKER):
        rec = rec.strip('\n')
        if not rec: continue
        head, _, tail = rec.partition('\n')
        parts = head.split('|', 3)
        if len(parts) < 4: continue
        sha, iso, email, title = parts
        if not is_owned and email.lower() not in AUTHOR_EMAILS:
            continue
        try:
            dt = datetime.fromisoformat(iso)
        except Exception:
            continue
        # numstat lines: added\tdeleted\tpath ('-' for binary)
        tally = Counter()
        total_lines = 0
        for ln in tail.splitlines():
            m = ln.split('\t')
            if len(m) < 3: continue
            a_s, d_s, path = m[0], m[1], m[2]
            try:
                a = int(a_s) if a_s != '-' else 0
                d = int(d_s) if d_s != '-' else 0
            except ValueError:
                a, d = 0, 0
            n = a + d
            total_lines += n
            lang = _lang_of_path(path)
            if lang: tally[lang] += n if n > 0 else 1  # weight by lines changed (min 1 for binary/empty)
        all_commits.append({'sha': sha, 'pid': pid, 'date': dt.date(), 'dt': dt,
                            'title': title, 'lines': total_lines})
        daily[dt.date().strftime('%Y-%m-%d')] += 1
        contributed_projects.add(pid)
        repo_commits[repo_name] += 1
        repo_lines[repo_name]   += total_lines
        if tally:
            tsum = sum(tally.values()) or 1
            for lang, w in tally.items():
                f = w / tsum
                lang_commits[lang] += f
                lang_lines[lang]   += total_lines * f
        else:
            lang_commits['Other'] += 1
            lang_lines['Other']   += total_lines
print(f'  {len(all_commits)} commits parsed in {int(_time.time()-_clone_start)}s', flush=True)
total_commits = len(all_commits)
total_contributions = sum(daily.values())

# MRs and Issues authored by user
total_mrs = len(gl_get(f'/merge_requests?author_id={USER_ID}&scope=all&state=all', paginate=True) or [])
total_issues = len(gl_get(f'/issues?author_id={USER_ID}&scope=all&state=all', paginate=True) or [])

# Streak compute
def streaks(daily_counter):
    today = datetime.now(timezone.utc).date()
    cur = 0
    started = False
    d = today
    while True:
        ds = d.strftime('%Y-%m-%d')
        if daily_counter.get(ds, 0) > 0:
            cur += 1; started = True
        else:
            if started: break
            if d == today:
                d -= timedelta(days=1); continue
            break
        d -= timedelta(days=1)
    longest = 0
    dates = sorted(datetime.strptime(k, '%Y-%m-%d').date() for k in daily_counter.keys())
    if dates:
        run = 1
        for i in range(1, len(dates)):
            if (dates[i] - dates[i-1]).days == 1: run += 1; longest = max(longest, run)
            else: run = 1
        longest = max(longest, run)
    return cur, longest

cur_streak, longest_streak = streaks(daily)
active_days = sum(1 for v in daily.values() if v > 0)
print(f'  cur_streak={cur_streak} longest={longest_streak} active_days={active_days} commits={total_commits} mrs={total_mrs} issues={total_issues}', flush=True)

# ============ SVG HELPERS ============
def svg_open(w, h):
    return f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}" font-family="-apple-system, BlinkMacSystemFont, Segoe UI, Helvetica, Arial, sans-serif">'

def card(w, h, title=None, title_centered=True):
    # 2px stroke so the green border reads clearly on black bg
    out = [f'<rect x="1.5" y="1.5" rx="8" ry="8" width="{w-3}" height="{h-3}" fill="{T["card"]}" stroke="{T["border"]}" stroke-width="2"/>']
    if title:
        # y=38 gives clean clearance from the top border + rounded corner
        if title_centered:
            out.append(f'<text x="{w/2}" y="38" fill="{T["accent"]}" font-size="16" font-weight="700" text-anchor="middle">{title}</text>')
        else:
            out.append(f'<text x="22" y="38" fill="{T["accent"]}" font-size="16" font-weight="700">{title}</text>')
    return ''.join(out)

def grid_divider(w, h):
    """+ cross divider centered through the geometric middle of the widget."""
    v = f'<line x1="{w/2}" y1="15" x2="{w/2}" y2="{h-15}" stroke="{T["divider"]}" stroke-width="1" opacity="0.6"/>'
    hl = f'<line x1="15" y1="{h/2}" x2="{w-15}" y2="{h/2}" stroke="{T["divider"]}" stroke-width="1" opacity="0.6"/>'
    return v + hl

# ============ RENDERERS ============

# ---- stats.svg (rectangular; header LEFT-aligned per user spec) ----
def render_stats():
    w, h = 380, 215
    out = [svg_open(w, h), card(w, h, f'{USER} - gitlab stats', title_centered=False)]
    rows = [
        ('star',   'Total Stars',     total_stars,                T['text_dim']),
        ('commit', 'Total Commits',   total_commits,              T['text_dim']),
        ('mr',     'Total MRs',       total_mrs,                  T['text_dim']),
        ('iss',    'Total Issues',    total_issues,               T['text_dim']),
        ('repo',   'Repos',           len(projects),              T['text_dim']),
        ('con',    'Contributed to',  len(contributed_projects),  T['text_dim']),
    ]
    score = (math.log1p(total_stars) * 3 +
             math.log1p(total_commits) * 2 +
             math.log1p(total_mrs) +
             math.log1p(total_issues) +
             math.log1p(len(projects)) * 2)
    grades = [(50, 'A++'), (35, 'A+'), (25, 'A'), (15, 'B+'), (8, 'B'), (3, 'C'), (0, 'D')]
    grade = next(g for thr, g in grades if score >= thr)
    cx, cy, rr = w - 55, 123, 36  # ring vertically centered in body area (title=32, h=215, center=123)
    pct = min(score / 50.0, 1.0)
    circ = 2 * math.pi * rr
    out.append(f'<circle cx="{cx}" cy="{cy}" r="{rr}" fill="none" stroke="{T["border"]}" stroke-width="5" stroke-opacity="0.3"/>')
    out.append(f'<circle cx="{cx}" cy="{cy}" r="{rr}" fill="none" stroke="{T["accent"]}" stroke-width="5" stroke-dasharray="{pct*circ:.1f},{circ-pct*circ:.1f}" transform="rotate(-90 {cx} {cy})" stroke-linecap="round"/>')
    out.append(f'<text x="{cx}" y="{cy+8}" fill="{T["accent"]}" font-size="20" font-weight="700" text-anchor="middle">{grade}</text>')
    y = 62
    icon_x = 28
    label_x = 50
    val_x = w - 105
    for icon_key, label, val, icon_color in rows:
        # render SVG icon centered on (icon_x, y-4); 16x16 viewBox scaled to ~14px
        path = ICONS[icon_key]
        fill = icon_color if icon_key == 'star' else 'none'
        out.append(f'<g transform="translate({icon_x},{y-4}) scale(0.75)"><path d="{path}" fill="{fill}" stroke="{icon_color}" stroke-width="1.8" stroke-linejoin="round" stroke-linecap="round"/></g>')
        out.append(f'<text x="{label_x}" y="{y}" fill="{T["text"]}" font-size="12">{label}</text>')
        out.append(f'<text x="{val_x}" y="{y}" fill="{T["text"]}" font-size="12" font-weight="700" text-anchor="end">{val:,}</text>')
        y += 25
    out.append('</svg>')
    return ''.join(out)

# ---- trophies.svg (square; SVG icon glyphs inside each circle) ----
# Inline SVG paths sized for a viewBox of -16..16 (32x32). Translated to circle center at runtime.
ICONS = {
    # 5-point star
    'star': 'M 0 -12 L 3.5 -3.7 L 12.3 -3.7 L 5.4 1.4 L 8 9.7 L 0 4.6 L -8 9.7 L -5.4 1.4 L -12.3 -3.7 L -3.5 -3.7 Z',
    # Fork: Y shape with bullets at each tip
    'fork': 'M -7 -8 m -3 0 a 3 3 0 1 0 6 0 a 3 3 0 1 0 -6 0 M 7 -8 m -3 0 a 3 3 0 1 0 6 0 a 3 3 0 1 0 -6 0 M 0 8 m -3 0 a 3 3 0 1 0 6 0 a 3 3 0 1 0 -6 0 M -7 -5 L -7 0 Q -7 3 -4 3 L 4 3 Q 7 3 7 0 L 7 -5 M 0 3 L 0 5',
    # Repo / folder
    'repo': 'M -11 -7 L -2 -7 L 0 -4 L 11 -4 L 11 9 L -11 9 Z',
    # Commits: circle with horizontal line through it
    'commit': 'M -12 0 L -5 0 M 5 0 L 12 0 M 0 0 m -5 0 a 5 5 0 1 0 10 0 a 5 5 0 1 0 -10 0',
    # Merge request: two parallel branches merging into one (left+right arrows joining)
    'mr': 'M -8 -9 m -3 0 a 3 3 0 1 0 6 0 a 3 3 0 1 0 -6 0 M 8 0 m -3 0 a 3 3 0 1 0 6 0 a 3 3 0 1 0 -6 0 M -8 -6 L -8 6 m -3 0 a 3 3 0 1 0 6 0 a 3 3 0 1 0 -6 0 M -8 -3 Q -8 0 -5 0 L 5 0',
    # Issue: circle with exclamation dot
    'iss': 'M 0 0 m -10 0 a 10 10 0 1 0 20 0 a 10 10 0 1 0 -20 0 M 0 -5 L 0 1 M 0 4 L 0 5',
    # Contributed to: heart shape
    'con': 'M 0 8 C -2 5 -10 1 -10 -4 C -10 -7 -7 -10 -4 -10 C -2 -10 -1 -9 0 -7 C 1 -9 2 -10 4 -10 C 7 -10 10 -7 10 -4 C 10 1 2 5 0 8 Z',
}

def render_trophies():
    w = h = 290
    title_h = 32  # reserve top band for title so cells don't collide with it
    body_top = title_h
    body_h = h - title_h
    out = [svg_open(w, h)]
    out.append(f'<rect x="1.5" y="1.5" rx="8" ry="8" width="{w-3}" height="{h-3}" fill="{T["card"]}" stroke="{T["border"]}" stroke-width="2"/>')
    out.append(f'<text x="{w/2}" y="22" fill="{T["accent"]}" font-size="14" font-weight="700" text-anchor="middle">achievements</text>')
    # divider: vertical starts below title; horizontal at body midpoint
    out.append(f'<line x1="{w/2}" y1="{body_top+8}" x2="{w/2}" y2="{h-12}" stroke="{T["divider"]}" stroke-width="1" opacity="0.6"/>')
    out.append(f'<line x1="12" y1="{body_top+body_h/2}" x2="{w-12}" y2="{body_top+body_h/2}" stroke="{T["divider"]}" stroke-width="1" opacity="0.6"/>')
    items = [
        ('star',   'Stars',   total_stars,    [1, 5, 25, 100]),
        ('fork',   'Forks',   total_forks,    [1, 5, 25, 100]),
        ('repo',   'Repos',   len(projects),  [1, 5, 15, 30]),
        ('commit', 'Commits', total_commits,  [10, 50, 200, 1000]),
    ]
    r = 26  # smaller circle to fit content in 290 widget
    for i, (icon, label, val, levels) in enumerate(items):
        col, row = i % 2, i // 2
        cx = w/4 + col * w/2
        # cell center within BODY area (not whole widget) -> no title collision
        cy = body_top + body_h/4 + row * body_h/2
        tier = 0
        for L in levels:
            if val >= L: tier += 1
        color = TIER_COLORS[tier]
        tier_label = TIER_LABELS[tier]
        out.append(f'<circle cx="{cx}" cy="{cy-22}" r="{r}" fill="none" stroke="{color}" stroke-width="2.5"/>')
        path = ICONS[icon]
        fill = color if icon == 'star' else 'none'
        out.append(f'<g transform="translate({cx},{cy-22})"><path d="{path}" fill="{fill}" stroke="{color}" stroke-width="1.8" stroke-linejoin="round" stroke-linecap="round"/></g>')
        out.append(f'<text x="{cx}" y="{cy+18}" fill="{T["text"]}" font-size="13" font-weight="600" text-anchor="middle">{label}</text>')
        out.append(f'<text x="{cx}" y="{cy+33}" fill="{color}" font-size="11" font-weight="600" text-anchor="middle">{tier_label}</text>')
        out.append(f'<text x="{cx}" y="{cy+46}" fill="{T["text_dim"]}" font-size="10" text-anchor="middle">{val:,}</text>')
    out.append('</svg>')
    return ''.join(out)

# ---- streak.svg (2x2; ALL 4 in rings; numbers truly centered in rings) ----
def render_streak():
    w = h = 290
    title_h = 32
    body_top = title_h
    body_h = h - title_h
    out = [svg_open(w, h)]
    out.append(f'<rect x="1.5" y="1.5" rx="8" ry="8" width="{w-3}" height="{h-3}" fill="{T["card"]}" stroke="{T["border"]}" stroke-width="2"/>')
    out.append(f'<text x="{w/2}" y="22" fill="{T["accent"]}" font-size="14" font-weight="700" text-anchor="middle">commit streak</text>')
    out.append(f'<line x1="{w/2}" y1="{body_top+8}" x2="{w/2}" y2="{h-12}" stroke="{T["divider"]}" stroke-width="1" opacity="0.6"/>')
    out.append(f'<line x1="12" y1="{body_top+body_h/2}" x2="{w-12}" y2="{body_top+body_h/2}" stroke="{T["divider"]}" stroke-width="1" opacity="0.6"/>')
    cells = [
        ('Total\nContributions', f'{total_contributions:,}', T['accent_alt']),
        ('Current Streak',       f'{cur_streak}',            T['accent']),
        ('Longest Streak',       f'{longest_streak}',        T['warn']),
        ('Active Days',          f'{active_days:,}',         T['accent_alt']),
    ]
    ring_r = 28
    for i, (label, val, color) in enumerate(cells):
        col, row = i % 2, i // 2
        cx = w/4 + col * w/2
        cy = body_top + body_h/4 + row * body_h/2
        ring_cy = cy - 14
        digits = len(val)
        max_fit = (2 * ring_r * 0.78) / max(digits * 0.6, 0.6)
        font_size = int(min(24, max_fit))
        out.append(f'<circle cx="{cx}" cy="{ring_cy}" r="{ring_r}" fill="none" stroke="{color}" stroke-width="2.5"/>')
        out.append(f'<text x="{cx}" y="{ring_cy}" fill="{color}" font-size="{font_size}" font-weight="700" text-anchor="middle" dominant-baseline="central">{val}</text>')
        lines = label.split('\n')
        ly = cy + 25
        for ln in lines:
            out.append(f'<text x="{cx}" y="{ly}" fill="{T["text_dim"]}" font-size="11" text-anchor="middle">{ln}</text>')
            ly += 13
    out.append('</svg>')
    return ''.join(out)

# ---- languages.svg (donut + per-lang list) ----
def render_languages():
    w, h = 380, 215
    _BODY_CENTER = 32 + (h - 32) / 2  # vertical center of body area (below title)
    out = [svg_open(w, h), card(w, h, 'most used languages')]
    if not lang_bytes:
        out.append(f'<text x="{w/2}" y="{h/2}" fill="{T["text_dim"]}" font-size="12" text-anchor="middle">no language data yet</text>')
        out.append('</svg>')
        return ''.join(out)
    items = sorted(lang_bytes.items(), key=lambda x: -x[1])[:8]
    other = sum(v for _, v in sorted(lang_bytes.items(), key=lambda x: -x[1])[8:])
    if other > 0:
        items.append(('Other', other))
    # donut chart on right (nudged right for space vs %s + vertically centered in body area)
    cx, cy, ro, ri = w - 80, 123, 65, 42
    start = -math.pi / 2  # start at top
    for name, val in items:
        frac = val / total_lang_bytes
        end = start + frac * 2 * math.pi
        large = 1 if frac > 0.5 else 0
        x1 = cx + ro * math.cos(start); y1 = cy + ro * math.sin(start)
        x2 = cx + ro * math.cos(end);   y2 = cy + ro * math.sin(end)
        xi1 = cx + ri * math.cos(end);  yi1 = cy + ri * math.sin(end)
        xi2 = cx + ri * math.cos(start); yi2 = cy + ri * math.sin(start)
        path = f'M {x1:.1f} {y1:.1f} A {ro} {ro} 0 {large} 1 {x2:.1f} {y2:.1f} L {xi1:.1f} {yi1:.1f} A {ri} {ri} 0 {large} 0 {xi2:.1f} {yi2:.1f} Z'
        out.append(f'<path d="{path}" fill="{lang_color(name)}"/>')
        start = end
    # legend on left
    y = 65
    for name, val in items:
        pct = (val / total_lang_bytes) * 100
        out.append(f'<circle cx="30" cy="{y-4}" r="6" fill="{lang_color(name)}"/>')
        out.append(f'<text x="45" y="{y}" fill="{T["text"]}" font-size="12" font-weight="600">{name}</text>')
        out.append(f'<text x="225" y="{y}" fill="{T["text_dim"]}" font-size="11" text-anchor="end">{pct:.2f}%</text>')
        y += 18
    out.append('</svg>')
    return ''.join(out)

def _donut_arc(cx, cy, ro, ri, start, end, fill):
    """Return SVG path for an annular sector between angles start..end (radians)."""
    if end <= start: return ''
    frac = (end - start) / (2 * math.pi)
    large = 1 if frac > 0.5 else 0
    x1  = cx + ro * math.cos(start); y1  = cy + ro * math.sin(start)
    x2  = cx + ro * math.cos(end);   y2  = cy + ro * math.sin(end)
    xi1 = cx + ri * math.cos(end);   yi1 = cy + ri * math.sin(end)
    xi2 = cx + ri * math.cos(start); yi2 = cy + ri * math.sin(start)
    path = (f'M {x1:.2f} {y1:.2f} A {ro} {ro} 0 {large} 1 {x2:.2f} {y2:.2f} '
            f'L {xi1:.2f} {yi1:.2f} A {ri} {ri} 0 {large} 0 {xi2:.2f} {yi2:.2f} Z')
    return f'<path d="{path}" fill="{fill}"/>'

def _double_donut_widget(title_text, outer_counter, inner_counter, key_color_fn, top_n=8,
                         outer_label='commits', inner_label='lines'):
    """Render a widget with concentric donuts sharing color mapping by key.
    outer_counter/inner_counter: Counter of {key: value}. Slices ordered by outer value desc.
    key_color_fn: function key -> hex color (used for both rings)."""
    w, h = 380, 215
    out = [svg_open(w, h), card(w, h, title_text)]
    if not outer_counter:
        out.append(f'<text x="{w/2}" y="{h/2}" fill="{T["text_dim"]}" font-size="12" text-anchor="middle">no data yet</text>')
        out.append('</svg>')
        return ''.join(out)
    # Order by outer value desc; group tail into 'Other' (merged with any existing 'Other' key)
    ordered = sorted(outer_counter.items(), key=lambda kv: -kv[1])
    head_keys = [k for k, _ in ordered[:top_n]]
    tail_keys = [k for k, _ in ordered[top_n:]]
    keys = [k for k in head_keys if k != 'Other']
    other_outer = outer_counter.get('Other', 0) + sum(outer_counter.get(t, 0) for t in tail_keys)
    other_inner = inner_counter.get('Other', 0) + sum(inner_counter.get(t, 0) for t in tail_keys)
    if other_outer > 0 or 'Other' in head_keys:
        keys.append('Other')
    outer_vals, inner_vals = [], []
    for k in keys:
        if k == 'Other':
            outer_vals.append(other_outer); inner_vals.append(other_inner)
        else:
            outer_vals.append(outer_counter.get(k, 0))
            inner_vals.append(inner_counter.get(k, 0))
    # Re-sort so 'Other' stays at the end but head stays in value order
    pairs = list(zip(keys, outer_vals, inner_vals))
    non_other = sorted([p for p in pairs if p[0] != 'Other'], key=lambda p: -p[1])
    other = [p for p in pairs if p[0] == 'Other']
    pairs = non_other + other
    keys       = [p[0] for p in pairs]
    outer_vals = [p[1] for p in pairs]
    inner_vals = [p[2] for p in pairs]
    outer_total = sum(outer_vals) or 1
    inner_total = sum(inner_vals) or 1
    # Concentric donuts, right side of card, vertically centered in body area
    cx, cy = w - 80, 123
    ro_outer, ri_outer = 72, 56   # outer ring band width 16
    ro_inner, ri_inner = 50, 30   # inner ring band width 20 with 6px gap from outer
    start_o = -math.pi / 2
    for k, v in zip(keys, outer_vals):
        end_o = start_o + (v / outer_total) * 2 * math.pi
        out.append(_donut_arc(cx, cy, ro_outer, ri_outer, start_o, end_o, key_color_fn(k)))
        start_o = end_o
    start_i = -math.pi / 2
    for k, v in zip(keys, inner_vals):
        end_i = start_i + (v / inner_total) * 2 * math.pi
        out.append(_donut_arc(cx, cy, ro_inner, ri_inner, start_i, end_i, key_color_fn(k)))
        start_i = end_i
    # Tiny ring labels between rings + inside inner ring
    out.append(f'<text x="{cx}" y="{cy - ro_outer + 12}" fill="{T["text_dim"]}" font-size="8" text-anchor="middle" font-weight="700">{outer_label}</text>')
    out.append(f'<text x="{cx}" y="{cy + 4}" fill="{T["text_dim"]}" font-size="8" text-anchor="middle" font-weight="700">{inner_label}</text>')
    # Legend on left
    y_leg = 65
    for k, ov in zip(keys, outer_vals):
        pct = (ov / outer_total) * 100
        out.append(f'<circle cx="30" cy="{y_leg-4}" r="6" fill="{key_color_fn(k)}"/>')
        # truncate long keys so they don't overrun the %
        label = (k if len(k) <= 20 else k[:19] + '…')
        out.append(f'<text x="45" y="{y_leg}" fill="{T["text"]}" font-size="12" font-weight="600">{label}</text>')
        out.append(f'<text x="225" y="{y_leg}" fill="{T["text_dim"]}" font-size="11" text-anchor="end">{pct:.2f}%</text>')
        y_leg += 18
    out.append('</svg>')
    return ''.join(out)

def render_lang_commits():
    ordered_all = sorted(lang_commits.items(), key=lambda kv: -kv[1])
    ordered = [k for k, _ in ordered_all[:8]]
    if len(ordered_all) > 8: ordered.append('Other')
    cmap = assign_distinct_colors(ordered)
    return _double_donut_widget('most committed languages', lang_commits, lang_lines,
                                lambda k: cmap.get(k, lang_color(k)))

def render_repo_commits():
    # Repos don't have canonical colors → use distinct-color palette directly
    ordered = [k for k, _ in repo_commits.most_common(8)]
    if len(repo_commits) > 8: ordered.append('Other')
    # Assign fallback palette colors in order (they're already distinct)
    palette = _DISTINCT_PALETTE + ['#ff6b6b', '#4ecdc4', '#f7b731', '#5f27cd']
    cmap = {k: palette[i % len(palette)] for i, k in enumerate(ordered)}
    return _double_donut_widget('most committed repos', repo_commits, repo_lines,
                                lambda k: cmap.get(k, '#888888'))

# ---- bubbles.svg (per-commit blobs, size=lines changed, color=primary language) ----
EXT_TO_LANG = {
    '.py':'Python','.pyi':'Python','.pyx':'Cython',
    '.cpp':'C++','.cc':'C++','.cxx':'C++','.hpp':'C++','.hh':'C++','.h':'C',
    '.c':'C','.ino':'C++',
    '.js':'JavaScript','.mjs':'JavaScript','.jsx':'JavaScript',
    '.ts':'TypeScript','.tsx':'TypeScript',
    '.html':'HTML','.htm':'HTML','.css':'CSS','.scss':'SCSS','.sass':'SCSS',
    '.rs':'Rust','.go':'Go','.java':'Java','.kt':'Kotlin','.swift':'Swift',
    '.sh':'Shell','.bash':'Shell','.zsh':'Shell','.ps1':'PowerShell','.bat':'Batchfile',
    '.cmake':'CMake','.md':'Markdown','.yml':'YAML','.yaml':'YAML','.toml':'TOML',
    '.json':'JSON','.xml':'XML','.svg':'SVG','.tex':'TeX','.rb':'Ruby','.lua':'Lua',
    '.glsl':'GLSL','.vert':'GLSL','.frag':'GLSL','.hlsl':'HLSL','.cs':'C#','.php':'PHP',
    '.vue':'Vue','.ipynb':'Jupyter Notebook','.asm':'Assembly','.s':'Assembly',
}

def _brighten(hex_color, frac=0.45):
    """Mix a hex color toward white by fraction (for bubble stroke contrast)."""
    h = hex_color.lstrip('#')
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    r = int(r + (255 - r) * frac)
    g = int(g + (255 - g) * frac)
    b = int(b + (255 - b) * frac)
    return f'#{r:02x}{g:02x}{b:02x}'

def render_bubbles(days=7, extra_past=1, time_label='this week'):
    w, h = 1080, 520  # scaled up for higher DPI + more bubble room
    out = [svg_open(w, h)]
    out.append(f'<rect x="1.5" y="1.5" rx="8" ry="8" width="{w-3}" height="{h-3}" fill="{T["card"]}" stroke="{T["border"]}" stroke-width="2"/>')
    out.append(f'<text x="{w/2}" y="32" fill="{T["accent"]}" font-size="17" font-weight="700" text-anchor="middle">commits {time_label} | size = lines changed | colour = primary language</text>')
    today = datetime.now(timezone.utc).date()
    fetch_days = days + extra_past
    week_start = today - timedelta(days=fetch_days - 1)   # oldest fetched (includes extra past)
    visible_start = today - timedelta(days=days - 1)      # oldest VISIBLE date shown on axis
    since = week_start.strftime('%Y-%m-%d')

    commits = []
    for pid in contributed_projects:
        try:
            cs = gl_get(f'/projects/{pid}/repository/commits?since={since}&with_stats=true', paginate=True)
        except Exception:
            continue
        for c in cs:
            lines = (c.get('stats') or {}).get('total', 0)
            if lines <= 0: continue
            try:
                diff = gl_get(f'/projects/{pid}/repository/commits/{c["id"]}/diff')
            except Exception:
                diff = []
            tally = Counter()
            for d in diff or []:
                path = d.get('new_path') or d.get('old_path') or ''
                if '.' not in path: continue
                ext = '.' + path.rsplit('.', 1)[-1].lower()
                lang = EXT_TO_LANG.get(ext)
                if lang: tally[lang] += 1
            primary = tally.most_common(1)[0][0] if tally else 'Other'
            ts = c.get('created_at', '') or c.get('committed_date', '')
            try:
                ts_dt = datetime.fromisoformat(ts.replace('Z','+00:00'))
                d_dt = ts_dt.date()
            except Exception:
                ts_dt = datetime.now(timezone.utc)
                d_dt = today
            commits.append({'date': d_dt, 'ts': ts_dt, 'lines': lines, 'language': primary,
                            'msg': (c.get('title','') or '')[:60],
                            'repo': repo_name_by_pid.get(pid, f'project-{pid}')})
    n_extra = sum(1 for c in commits if c['date'] < visible_start)
    print(f'  bubbles: {len(commits)} commits in last {fetch_days} days ({n_extra} extra past for overflow effect)', flush=True)

    def truncate_middle(s, max_chars):
        if len(s) <= max_chars: return s
        if max_chars < 3: return s[:max_chars]
        keep = max_chars - 1
        left = (keep + 1) // 2
        right = keep - left
        return s[:left] + '…' + (s[-right:] if right > 0 else '')

    # Legend: order by total volume desc, assign perceptually-distinct colors
    lang_count = Counter(c['language'] for c in commits)
    lang_volume = defaultdict(int)
    for c in commits:
        lang_volume[c['language']] += c['lines']
    ordered_langs = sorted(lang_volume.keys(), key=lambda l: -lang_volume[l])
    color_map = assign_distinct_colors(ordered_langs)
    legend_y = 64
    legend_row_h = 22
    lx = 30
    margin_right = 30
    extra_rows = 0
    for lang in ordered_langs:
        lang_w   = len(lang) * 8
        stats_str = f'{lang_count[lang]}c / {lang_volume[lang]:,}L'
        stats_w  = len(stats_str) * 7
        entry_w  = 22 + lang_w + 6 + stats_w + 24
        if lx + entry_w > w - margin_right:
            lx = 30
            extra_rows += 1
        y = legend_y + extra_rows * legend_row_h
        col = color_map[lang]
        out.append(f'<circle cx="{lx+8}" cy="{y}" r="7" fill="{col}" stroke="{_brighten(col,0.5)}" stroke-width="1.5"/>')
        out.append(f'<text x="{lx+22}" y="{y+5}" fill="{T["text"]}" font-size="13" font-weight="600">{lang}</text>')
        out.append(f'<text x="{lx+22+lang_w+6}" y="{y+5}" fill="{T["text_dim"]}" font-size="11">{stats_str}</text>')
        lx += entry_w

    if not commits:
        out.append(f'<text x="{w/2}" y="{h/2}" fill="{T["text_dim"]}" font-size="14" text-anchor="middle">no commits this week</text>')
        out.append('</svg>')
        return ''.join(out)

    # plot area top adjusts for legend wraps (extra_rows from legend layout above)
    pad_l, pad_r, pad_b = 40, 40, 45
    pad_t = 105 + extra_rows * legend_row_h
    plot_w = w - pad_l - pad_r
    plot_h = h - pad_t - pad_b

    # Axis: past=LEFT, present=RIGHT (standard). Extra past day overflows off LEFT edge (rolling-window visual).
    # Vertical grid lines — thin out for long ranges (matches date-label spacing)
    grid_step = max(1, (days - 1) // 6)
    for di in range(0, days, grid_step):
        x = pad_l + (di / (days - 1)) * plot_w
        out.append(f'<line x1="{x:.1f}" y1="{pad_t}" x2="{x:.1f}" y2="{h - pad_b + 6}" stroke="#003a00" stroke-width="1"/>')

    # Radii scale so total bubble area fills ~55% of plot area regardless of commit count.
    # Fewer commits -> bigger bubbles, more commits -> smaller — no voids from low counts.
    log_vals = [math.log1p(c['lines']) for c in commits]
    target_fill = 0.60
    target_area = target_fill * plot_w * plot_h
    # sizing weight per commit = (log_val)^1 so bubble area is proportional to log(lines)
    w_sum = sum(lv * lv for lv in log_vals) or 1
    k = math.sqrt(target_area / (math.pi * w_sum))
    # clamp to sane visual range so nothing gets absurd
    radii = [max(8, min(70, lv * k)) for lv in log_vals]

    import random
    random.seed(42)
    # Soft y-bias by repo rarity: common repos start biased toward top, rare
    # ones toward bottom. Heavy random jitter on top so the physics + collision
    # still make it feel organic. Common repos will overflow their band downward
    # due to sheer volume, but small clusters of rare repos will naturally settle
    # near the bottom instead of being scattered everywhere.
    repo_counts = Counter(c['repo'] for c in commits)
    repos_sorted = sorted(repo_counts.keys(), key=lambda r: -repo_counts[r])  # most-common first
    n_repos = len(repos_sorted)
    repo_rank_frac = {r: (i / max(n_repos - 1, 1)) for i, r in enumerate(repos_sorted)}  # 0=common (top), 1=rare (bottom)
    px, py = [], []
    day_col_w = plot_w / max(days - 1, 1)

    # X (within-day rank): group commits by date, sort by timestamp, rank fills column evenly
    #   → preserves chronological order within a day, spreads across full column width,
    #     no big intra-day gaps regardless of commit count.
    per_day = defaultdict(list)
    for i, c in enumerate(commits):
        per_day[c['date']].append((c['ts'], i))
    x_rank = [0.5] * len(commits)  # default: center of column for solo-commit days
    for day, entries in per_day.items():
        entries.sort(key=lambda e: e[0])
        n = len(entries)
        for r_idx, (_, i) in enumerate(entries):
            x_rank[i] = (r_idx + 0.5) / n if n > 0 else 0.5   # 0..1 fraction within column

    # Y (rank WITHIN each day): common repos toward top of the column, rare toward bottom,
    #   but each day's commits distribute across the FULL plot height so no column has a void.
    #   Global rank was causing top/bottom clustering per day; per-day rank fills every column.
    y_rank = [0.5] * len(commits)
    for day, entries_x in per_day.items():
        # sort by repo rank (common repos first → top); index as secondary for stable tiebreak
        by_repo = sorted(entries_x, key=lambda e: (repo_rank_frac[commits[e[1]]['repo']], e[1]))
        n = len(by_repo)
        for r_idx, (_, i) in enumerate(by_repo):
            y_rank[i] = (r_idx + 0.5) / n if n > 0 else 0.5

    for i, c in enumerate(commits):
        r = radii[i]
        d_off = (c['date'] - visible_start).days
        # x: day column base + rank-based intra-column offset (spans ~90% of column) + tiny jitter to avoid perfect alignment
        intra_col_off = (x_rank[i] - 0.5) * day_col_w * 0.9
        if d_off >= 0:
            x = pad_l + (d_off / (days - 1)) * plot_w + intra_col_off + random.uniform(-4, 4)
        else:
            # Extra past day: cluster near left image edge (mostly cut off)
            x = 0 + random.uniform(-r * 0.35, r * 0.35)
        # y: full-plot rank-fill (small jitter for organic feel; physics handles collisions)
        y = pad_t + y_rank[i] * plot_h + random.uniform(-plot_h * 0.04, plot_h * 0.04)
        px.append(x); py.append(y)

    for _ in range(120):
        for i in range(len(commits)):
            for j in range(i + 1, len(commits)):
                dx = px[j] - px[i]; dy = py[j] - py[i]
                dist2 = dx*dx + dy*dy
                min_d = radii[i] + radii[j] + 3
                if dist2 < min_d * min_d:
                    dist = math.sqrt(dist2) + 1e-9
                    overlap = (min_d - dist) / 2
                    nx, ny = dx / dist, dy / dist
                    px[i] -= nx * overlap; py[i] -= ny * overlap
                    px[j] += nx * overlap; py[j] += ny * overlap
        for i in range(len(commits)):
            r = radii[i]
            # Allow LEFT-edge overflow (rolling-window cutoff visual). Right wall stays firm.
            px[i] = max(0 - r * 0.55, min(w - pad_r - r, px[i]))
            py[i] = max(pad_t + r, min(h - pad_b - r, py[i]))

    # Draw bubbles with bright stroke for contrast + line count inside
    def xml_escape(text):
        return text.replace('&','&amp;').replace('<','&lt;').replace('>','&gt;').replace('"','&quot;')
    for i, c in enumerate(commits):
        col = color_map.get(c['language'], lang_color(c['language']))
        stroke = _brighten(col, 0.55)
        title_text = xml_escape(f'{c["repo"]}: {c["msg"]} ({c["lines"]} lines, {c["language"]})')
        out.append(f'<circle cx="{px[i]:.1f}" cy="{py[i]:.1f}" r="{radii[i]:.1f}" fill="{col}" fill-opacity="0.85" stroke="{stroke}" stroke-width="2.5"><title>{title_text}</title></circle>')
        if radii[i] >= 13:
            r = radii[i]
            # Slightly smaller cap than before (14→12) so big bubbles don't feel shouty.
            fs = max(9, min(12, int(r / 3.5)))
            max_chars = int((2 * r * 0.82) / (fs * 0.55))
            parts = c['repo'].split('_')
            # Multi-line render only if 2+ parts AND bubble is medium+ (r>=18) so lines fit vertically
            if len(parts) >= 2 and r >= 18 and all(len(p) <= max_chars for p in parts):
                n = len(parts)
                line_h = fs * 1.05
                # vertical space needed = n*line_h; must fit within ~1.4*r (usable inner circle height)
                if n * line_h <= r * 1.6:
                    top_y = py[i] - (n - 1) * line_h / 2 + fs / 3
                    for j, p in enumerate(parts):
                        y_line = top_y + j * line_h
                        out.append(f'<text x="{px[i]:.1f}" y="{y_line:.1f}" fill="#000" font-size="{fs}" font-weight="700" text-anchor="middle" pointer-events="none">{xml_escape(p)}</text>')
                    continue
            label = xml_escape(truncate_middle(c['repo'], max_chars))
            out.append(f'<text x="{px[i]:.1f}" y="{py[i] + fs/3:.1f}" fill="#000" font-size="{fs}" font-weight="700" text-anchor="middle" pointer-events="none">{label}</text>')

    # Date labels: standard axis — oldest visible on LEFT, today on RIGHT.
    # For long ranges (month view) show ~7 labels, not one per day.
    label_step = max(1, (days - 1) // 6)
    for di in range(0, days, label_step):
        d = visible_start + timedelta(days=di)
        x = pad_l + (di / (days - 1)) * plot_w
        out.append(f'<text x="{x}" y="{h-18}" fill="{T["text_dim"]}" font-size="12" text-anchor="middle">{d.strftime("%b %d")}</text>')
    # always include today as the rightmost label if step doesn't hit exactly
    if (days - 1) % label_step != 0:
        out.append(f'<text x="{pad_l + plot_w}" y="{h-18}" fill="{T["text_dim"]}" font-size="12" text-anchor="middle">{today.strftime("%b %d")}</text>')
    out.append('</svg>')
    return ''.join(out)

# ---- activity.svg ----
def render_activity():
    w, h = 720, 220
    out = [svg_open(w, h), card(w, h, 'recent activity (events/day)')]
    days = 90
    today = datetime.now(timezone.utc).date()
    pts = [(today - timedelta(days=days - 1 - i), daily.get((today - timedelta(days=days - 1 - i)).strftime('%Y-%m-%d'), 0)) for i in range(days)]
    max_v = max((p[1] for p in pts), default=1) or 1
    pl, pr, pt, pb = 50, 20, 50, 35
    pw, ph = w - pl - pr, h - pt - pb
    # bottom x-axis line removed per user request (was confusing to read)
    out.append(f'<line x1="{pl}" y1="{pt}" x2="{pl}" y2="{pt+ph}" stroke="{T["border"]}" stroke-opacity="0.4"/>')
    coords = [(pl + (i / (days - 1)) * pw, pt + ph - (v / max_v) * ph) for i, (_, v) in enumerate(pts)]
    poly = ' '.join(f'{x:.1f},{y:.1f}' for x, y in coords)
    out.append(f'<polyline points="{poly}" fill="none" stroke="{T["accent"]}" stroke-width="2"/>')
    last_m = None
    for i, (d, _) in enumerate(pts):
        m = d.strftime('%b')
        if m != last_m:
            x = pl + (i / (days - 1)) * pw
            out.append(f'<text x="{x}" y="{pt+ph+18}" fill="{T["text_dim"]}" font-size="10" text-anchor="middle">{m}</text>')
            last_m = m
    out.append(f'<text x="{pl-8}" y="{pt+5}" fill="{T["text_dim"]}" font-size="10" text-anchor="end">{max_v}</text>')
    out.append(f'<text x="{pl-8}" y="{pt+ph}" fill="{T["text_dim"]}" font-size="10" text-anchor="end">0</text>')
    out.append('</svg>')
    return ''.join(out)

# ---- snake.svg (forced width 720 to match other wide widgets) ----
def render_snake():
    w = 720
    weeks, days_n = 53, 7
    pt = 50
    cell, gap = 9, 3
    grid_w = weeks * (cell + gap)
    pl = (w - grid_w - 20) / 2
    h = pt + days_n * (cell + gap) + 35
    out = [svg_open(w, h), card(w, h, '53-week contribution grid')]
    today = datetime.now(timezone.utc).date()
    end_sun = today - timedelta(days=(today.weekday() + 1) % 7)
    start = end_sun - timedelta(weeks=weeks - 1)
    for wi in range(weeks):
        for di in range(days_n):
            d = start + timedelta(weeks=wi, days=di)
            v = daily.get(d.strftime('%Y-%m-%d'), 0)
            lvl = 0
            if v >= 1: lvl = 1
            if v >= 3: lvl = 2
            if v >= 6: lvl = 3
            if v >= 10: lvl = 4
            x = pl + wi * (cell + gap)
            y = pt + di * (cell + gap)
            out.append(f'<rect x="{x}" y="{y}" width="{cell}" height="{cell}" rx="2" fill="{T[f"grid_{lvl}"]}"/>')
    for di, name in enumerate(['', 'M', '', 'W', '', 'F', '']):
        if name:
            y = pt + di * (cell + gap) + 9
            out.append(f'<text x="20" y="{y}" fill="{T["text_dim"]}" font-size="9" text-anchor="end">{name}</text>')
    legend_x = w - 160
    out.append(f'<text x="{legend_x-10}" y="{h-12}" fill="{T["text_dim"]}" font-size="9" text-anchor="end">Less</text>')
    for i in range(5):
        out.append(f'<rect x="{legend_x + i*15}" y="{h-22}" width="{cell}" height="{cell}" rx="2" fill="{T[f"grid_{i}"]}"/>')
    out.append(f'<text x="{legend_x + 5*15 + 5}" y="{h-12}" fill="{T["text_dim"]}" font-size="9">More</text>')
    out.append('</svg>')
    return ''.join(out)

# ============ write ============
for name, fn in [
    ('stats', render_stats), ('trophies', render_trophies), ('streak', render_streak),
    ('languages', render_languages),
    ('lang_commits', render_lang_commits), ('repo_commits', render_repo_commits),
    ('bubbles',       lambda: render_bubbles(days=7,  extra_past=1, time_label='this week')),
    ('bubbles_month', lambda: render_bubbles(days=30, extra_past=3, time_label='this month')),
    ('activity', render_activity), ('snake', render_snake),
]:
    p = os.path.join(OUT, f'{name}.svg')
    with open(p, 'w', encoding='utf-8') as f:
        f.write(fn())
    print(f'wrote {p} ({os.path.getsize(p)} bytes)', flush=True)
print('done.')
