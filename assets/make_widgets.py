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

# Fallback palette for perceptually-distinct assignment when natural colors collide
_DISTINCT_PALETTE = ['#e91e63','#9c27b0','#673ab7','#3f51b5','#2196f3','#00bcd4','#009688','#4caf50','#cddc39','#ff9800','#795548','#607d8b']

def _hex_to_rgb(h):
    h = h.lstrip('#')
    return int(h[0:2],16), int(h[2:4],16), int(h[4:6],16)

def perceptual_dist(c1, c2):
    """Quick perception-weighted RGB distance (good enough for collision detection)."""
    r1,g1,b1 = _hex_to_rgb(c1); r2,g2,b2 = _hex_to_rgb(c2)
    return ((r1-r2)**2 * 0.3 + (g1-g2)**2 * 0.59 + (b1-b2)**2 * 0.11) ** 0.5

def assign_distinct_colors(langs, min_dist=35):
    """Given an ordered list of languages, return {lang: color} where each color is
    perceptually distinct from all previously-assigned colors. Falls back to
    DISTINCT_PALETTE when the natural language color collides."""
    assigned = {}
    used = []
    for lang in langs:
        nat = lang_color(lang)
        if all(perceptual_dist(nat, u) > min_dist for u in used):
            chosen = nat
        else:
            chosen = nat  # default to natural if no fallback fits
            for fb in _DISTINCT_PALETTE:
                if all(perceptual_dist(fb, u) > min_dist for u in used):
                    chosen = fb
                    break
        assigned[lang] = chosen
        used.append(chosen)
    return assigned

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

# Discover all projects user has touched: owned + contributed-to + any picked up via events
project_ids = {p['id'] for p in projects}
try:
    contributed_api = gl_get(f'/users/{USER_ID}/contributed_projects', paginate=True) or []
    for cp in contributed_api:
        project_ids.add(cp['id'])
    print(f'  contributed_projects API added {len(contributed_api)} project entries', flush=True)
except Exception as _e:
    print(f'  contributed_projects API failed: {_e}', flush=True)

# Author email filter — user's commits could be authored by either the
# old zen-ham email or the new zenham email
AUTHOR_EMAILS = {'again.really.plz@gmail.com', 'zenmastermagnet@gmail.com', 'roeganjoe47@gmail.com'}

all_commits = []
print(f'  fetching commits across {len(project_ids)} projects (owned + contributed)...', flush=True)
for pid in project_ids:
    try:
        cs = gl_get(f'/projects/{pid}/repository/commits', paginate=True)
    except Exception:
        continue
    for c in cs:
        # For collaborator repos, filter to user-authored commits only (so other collaborators' commits don't pollute zenham's stats)
        if pid not in {p['id'] for p in projects}:
            ae = (c.get('author_email') or '').lower()
            if ae and ae not in AUTHOR_EMAILS:
                continue
        ts = c.get('created_at') or c.get('committed_date') or ''
        try:
            dt = datetime.fromisoformat(ts.replace('Z', '+00:00'))
        except Exception:
            continue
        all_commits.append({'sha': c.get('id',''), 'pid': pid, 'date': dt.date(), 'dt': dt,
                            'title': (c.get('title','') or '')})
print(f'  {len(all_commits)} commits total (after author filter on non-owned repos)', flush=True)

daily = Counter()
contributed_projects = set()
for c in all_commits:
    daily[c['date'].strftime('%Y-%m-%d')] += 1
    contributed_projects.add(c['pid'])
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
        ('STAR', 'Total Stars', total_stars),
        ('GIT',  'Total Commits', total_commits),
        ('MR',   'Total MRs', total_mrs),
        ('ISS',  'Total Issues', total_issues),
        ('REPO', 'Repos', len(projects)),
        ('CON',  'Contributed to', len(contributed_projects)),
    ]
    score = (math.log1p(total_stars) * 3 +
             math.log1p(total_commits) * 2 +
             math.log1p(total_mrs) +
             math.log1p(total_issues) +
             math.log1p(len(projects)) * 2)
    grades = [(50, 'A++'), (35, 'A+'), (25, 'A'), (15, 'B+'), (8, 'B'), (3, 'C'), (0, 'D')]
    grade = next(g for thr, g in grades if score >= thr)
    cx, cy, rr = w - 65, 130, 38
    pct = min(score / 50.0, 1.0)
    circ = 2 * math.pi * rr
    out.append(f'<circle cx="{cx}" cy="{cy}" r="{rr}" fill="none" stroke="{T["border"]}" stroke-width="6"/>')
    out.append(f'<circle cx="{cx}" cy="{cy}" r="{rr}" fill="none" stroke="{T["accent"]}" stroke-width="6" stroke-dasharray="{pct*circ:.1f},{circ-pct*circ:.1f}" transform="rotate(-90 {cx} {cy})" stroke-linecap="round"/>')
    out.append(f'<text x="{cx}" y="{cy+8}" fill="{T["accent"]}" font-size="22" font-weight="700" text-anchor="middle">{grade}</text>')
    y = 65
    label_x = 20
    val_x = w - 150
    for icon, label, val in rows:
        out.append(f'<text x="{label_x}" y="{y}" fill="{T["text_dim"]}" font-size="12" font-weight="600">{icon}</text>')
        out.append(f'<text x="{label_x+40}" y="{y}" fill="{T["text"]}" font-size="12">{label}</text>')
        out.append(f'<text x="{val_x}" y="{y}" fill="{T["text"]}" font-size="12" font-weight="700" text-anchor="end">{val:,}</text>')
        y += 25
    out.append('</svg>')
    return ''.join(out)

# ---- trophies.svg (square; SVG icon glyphs inside each circle) ----
# Inline SVG paths sized for a viewBox of -16..16 (32x32). Translated to circle center at runtime.
ICONS = {
    # 5-point star
    'star': 'M 0 -12 L 3.5 -3.7 L 12.3 -3.7 L 5.4 1.4 L 8 9.7 L 0 4.6 L -8 9.7 L -5.4 1.4 L -12.3 -3.7 L -3.5 -3.7 Z',
    # Fork: a Y shape with bullets
    'fork': 'M -7 -8 m -3 0 a 3 3 0 1 0 6 0 a 3 3 0 1 0 -6 0 M 7 -8 m -3 0 a 3 3 0 1 0 6 0 a 3 3 0 1 0 -6 0 M 0 8 m -3 0 a 3 3 0 1 0 6 0 a 3 3 0 1 0 -6 0 M -7 -5 L -7 0 Q -7 3 -4 3 L 4 3 Q 7 3 7 0 L 7 -5 M 0 3 L 0 5',
    # Repo / folder
    'repo': 'M -11 -7 L -2 -7 L 0 -4 L 11 -4 L 11 9 L -11 9 Z',
    # Commits: circle with horizontal line through it
    'commit': 'M -12 0 L -5 0 M 5 0 L 12 0 M 0 0 m -5 0 a 5 5 0 1 0 10 0 a 5 5 0 1 0 -10 0',
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
    out = [svg_open(w, h), card(w, h, 'most used languages')]
    if not lang_bytes:
        out.append(f'<text x="{w/2}" y="{h/2}" fill="{T["text_dim"]}" font-size="12" text-anchor="middle">no language data yet</text>')
        out.append('</svg>')
        return ''.join(out)
    items = sorted(lang_bytes.items(), key=lambda x: -x[1])[:8]
    other = sum(v for _, v in sorted(lang_bytes.items(), key=lambda x: -x[1])[8:])
    if other > 0:
        items.append(('Other', other))
    # donut chart on right
    cx, cy, ro, ri = w - 90, 130, 65, 42
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

def render_bubbles():
    w, h = 1080, 520  # scaled up for higher DPI + more bubble room
    out = [svg_open(w, h)]
    out.append(f'<rect x="1.5" y="1.5" rx="8" ry="8" width="{w-3}" height="{h-3}" fill="{T["card"]}" stroke="{T["border"]}" stroke-width="2"/>')
    out.append(f'<text x="{w/2}" y="32" fill="{T["accent"]}" font-size="17" font-weight="700" text-anchor="middle">commits this week | size = lines changed | colour = primary language</text>')
    today = datetime.now(timezone.utc).date()
    days = 7
    week_start = today - timedelta(days=days - 1)
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
                d_dt = datetime.fromisoformat(ts.replace('Z','+00:00')).date()
            except Exception:
                d_dt = today
            commits.append({'date': d_dt, 'lines': lines, 'language': primary,
                            'msg': (c.get('title','') or '')[:60]})
    print(f'  bubbles: {len(commits)} commits in last {days} days', flush=True)

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

    # Vertical grid lines per date label (darker green, low priority visual)
    for di in range(days):
        x = pad_l + (di / (days - 1)) * plot_w
        out.append(f'<line x1="{x:.1f}" y1="{pad_t}" x2="{x:.1f}" y2="{h - pad_b + 6}" stroke="#003a00" stroke-width="1"/>')

    log_vals = [math.log1p(c['lines']) for c in commits]
    max_lv, min_lv = max(log_vals), min(log_vals)
    min_r, max_r = 10, 52
    def radius(lv): return min_r + (lv - min_lv) / (max_lv - min_lv + 1e-9) * (max_r - min_r)
    radii = [radius(lv) for lv in log_vals]

    import random
    random.seed(42)
    px, py = [], []
    for c in commits:
        day_offset = max(0, min(days - 1, (c['date'] - week_start).days))
        x = pad_l + (day_offset / (days - 1)) * plot_w + random.uniform(-15, 15)
        y = pad_t + random.uniform(20, plot_h - 20)
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
            px[i] = max(pad_l + r, min(w - pad_r - r, px[i]))
            py[i] = max(pad_t + r, min(h - pad_b - r, py[i]))

    # Draw bubbles with bright stroke for contrast + line count inside
    def xml_escape(text):
        return text.replace('&','&amp;').replace('<','&lt;').replace('>','&gt;').replace('"','&quot;')
    for i, c in enumerate(commits):
        col = color_map.get(c['language'], lang_color(c['language']))
        stroke = _brighten(col, 0.55)
        title_text = xml_escape(f'{c["msg"]} ({c["lines"]} lines, {c["language"]})')
        out.append(f'<circle cx="{px[i]:.1f}" cy="{py[i]:.1f}" r="{radii[i]:.1f}" fill="{col}" fill-opacity="0.85" stroke="{stroke}" stroke-width="2.5"><title>{title_text}</title></circle>')
        if radii[i] >= 13:
            fs = max(9, min(18, int(radii[i] / 2.3)))
            out.append(f'<text x="{px[i]:.1f}" y="{py[i] + fs/3:.1f}" fill="#000" font-size="{fs}" font-weight="700" text-anchor="middle" pointer-events="none">{c["lines"]}</text>')

    for di in range(days):
        d = week_start + timedelta(days=di)
        x = pad_l + (di / (days - 1)) * plot_w
        out.append(f'<text x="{x}" y="{h-18}" fill="{T["text_dim"]}" font-size="12" text-anchor="middle">{d.strftime("%b %d")}</text>')
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
    ('languages', render_languages), ('bubbles', render_bubbles),
    ('activity', render_activity), ('snake', render_snake),
]:
    p = os.path.join(OUT, f'{name}.svg')
    with open(p, 'w', encoding='utf-8') as f:
        f.write(fn())
    print(f'wrote {p} ({os.path.getsize(p)} bytes)', flush=True)
print('done.')
