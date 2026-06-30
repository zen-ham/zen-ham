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
    'border':    '#222222',
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
    'CSS':'#563d7c','SCSS':'#c6538c','C++':'#f34b7d','C':'#555555','Rust':'#dea584',
    'Go':'#00ADD8','Java':'#b07219','Shell':'#89e051','Ruby':'#701516','Kotlin':'#A97BFF',
    'Swift':'#F05138','PHP':'#4F5D95','Lua':'#000080','Vue':'#41b883','PowerShell':'#012456',
    'Batchfile':'#C1F12E','Makefile':'#427819','CMake':'#DA3434','Dockerfile':'#384d54',
    'GLSL':'#5686a5','HLSL':'#aace60','Cython':'#fedf5b','C#':'#178600','Jupyter Notebook':'#DA5B0B',
    'Markdown':'#083fa1','TeX':'#3D6117','YAML':'#cb171e','TOML':'#9c4221',
    'JSON':'#292929','XML':'#0060ac','SVG':'#ff9900','Assembly':'#6E4C13',
}
def lang_color(name): return LANG_COLORS.get(name, '#888888')

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

# Events over ~1 year, chunked to avoid pagination caps
events = []
seen = set()
today = datetime.now(timezone.utc).date()
for ci in range(13):
    after  = (today - timedelta(days=(ci + 1) * 30)).strftime('%Y-%m-%d')
    before = (today - timedelta(days=ci * 30) + timedelta(days=1)).strftime('%Y-%m-%d')
    try:
        chunk = gl_get(f'/users/{USER_ID}/events?after={after}&before={before}', paginate=True)
    except Exception:
        continue
    new = [e for e in chunk if e.get('id') and e['id'] not in seen]
    for e in new: seen.add(e['id'])
    events += new
print(f'  {len(events)} events over ~390 days', flush=True)

daily = Counter()
total_commits = 0
contributed_projects = set()
for e in events:
    d = e.get('created_at', '')[:10]
    if d: daily[d] += 1
    if e.get('action_name') == 'pushed to':
        total_commits += e.get('push_data', {}).get('commit_count', 1)
    pid = e.get('project_id')
    if pid: contributed_projects.add(pid)
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
    out = [f'<rect x="1" y="1" rx="8" ry="8" width="{w-2}" height="{h-2}" fill="{T["card"]}" stroke="{T["border"]}"/>']
    if title:
        if title_centered:
            out.append(f'<text x="{w/2}" y="30" fill="{T["accent"]}" font-size="16" font-weight="700" text-anchor="middle">{title}</text>')
        else:
            out.append(f'<text x="20" y="30" fill="{T["accent"]}" font-size="16" font-weight="700">{title}</text>')
    return ''.join(out)

# ============ RENDERERS ============

# ---- stats.svg (rectangular; header LEFT-aligned per user spec) ----
def render_stats():
    w, h = 460, 230
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

# ---- trophies.svg (square, 2x2 grid, 4 items) ----
def render_trophies():
    w = h = 320
    out = [svg_open(w, h), card(w, h, 'achievements')]
    items = [
        ('STAR', 'Stars',     total_stars,        [1, 5, 25, 100]),
        ('FORK', 'Forks',     total_forks,        [1, 5, 25, 100]),
        ('REPO', 'Repos',     len(projects),      [1, 5, 15, 30]),
        ('COMM', 'Commits',   total_commits,      [10, 50, 200, 1000]),
    ]
    # 2x2 cell layout starting below the title
    cell_w, cell_h = w / 2, (h - 50) / 2
    for i, (icon, label, val, levels) in enumerate(items):
        col, row = i % 2, i // 2
        cx = cell_w * col + cell_w / 2
        cy = 50 + cell_h * row + cell_h / 2
        tier = 0
        for L in levels:
            if val >= L: tier += 1
        color = TIER_COLORS[tier]
        tier_label = TIER_LABELS[tier]
        out.append(f'<circle cx="{cx}" cy="{cy-12}" r="32" fill="none" stroke="{color}" stroke-width="3"/>')
        out.append(f'<text x="{cx}" y="{cy-6}" font-size="11" font-weight="700" fill="{color}" text-anchor="middle">{icon}</text>')
        out.append(f'<text x="{cx}" y="{cy+32}" fill="{T["text"]}" font-size="12" font-weight="600" text-anchor="middle">{label}</text>')
        out.append(f'<text x="{cx}" y="{cy+48}" fill="{color}" font-size="11" text-anchor="middle">{tier_label}</text>')
        out.append(f'<text x="{cx}" y="{cy+62}" fill="{T["text_dim"]}" font-size="10" text-anchor="middle">{val:,}</text>')
    out.append('</svg>')
    return ''.join(out)

# ---- streak.svg (2x2 square, 4 stats, ring around current) ----
def render_streak():
    w = h = 320
    out = [svg_open(w, h), card(w, h, 'commit streak')]
    cells = [
        ('Total\nContributions', f'{total_contributions:,}', T['accent_alt'], False),
        ('Current Streak',       f'{cur_streak}',            T['accent'],     True),  # ring
        ('Longest Streak',       f'{longest_streak}',        T['warn'],       False),
        ('Active Days',          f'{active_days:,}',         T['accent_alt'], False),
    ]
    cell_w, cell_h = w / 2, (h - 50) / 2
    for i, (label, val, color, ring) in enumerate(cells):
        col, row = i % 2, i // 2
        cx = cell_w * col + cell_w / 2
        cy = 50 + cell_h * row + cell_h / 2
        if ring:
            out.append(f'<circle cx="{cx}" cy="{cy-12}" r="34" fill="none" stroke="{color}" stroke-width="2.5"/>')
        out.append(f'<text x="{cx}" y="{cy-4}" fill="{color}" font-size="26" font-weight="700" text-anchor="middle">{val}</text>')
        # multi-line label
        lines = label.split('\n')
        ly = cy + 30
        for ln in lines:
            out.append(f'<text x="{cx}" y="{ly}" fill="{T["text_dim"]}" font-size="11" text-anchor="middle">{ln}</text>')
            ly += 14
    out.append('</svg>')
    return ''.join(out)

# ---- languages.svg (donut + per-lang list) ----
def render_languages():
    w, h = 480, 240
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

# ---- bubbles.svg (language code bubbles, log-scale area, centered single-row) ----
def render_bubbles():
    w, h = 720, 280
    out = [svg_open(w, h), card(w, h, 'code distribution by language (log scale bubbles)')]
    if not lang_bytes:
        out.append(f'<text x="{w/2}" y="{h/2}" fill="{T["text_dim"]}" font-size="12" text-anchor="middle">no language data yet</text>')
        out.append('</svg>')
        return ''.join(out)
    items = sorted(lang_bytes.items(), key=lambda x: -x[1])[:12]
    log_vals = [math.log1p(v) for _, v in items]
    max_lv = max(log_vals)
    min_lv = min(log_vals)
    max_r = 60
    min_r = 14
    def radius(lv): return min_r + (lv - min_lv) / (max_lv - min_lv + 1e-9) * (max_r - min_r)
    radii = [radius(lv) for lv in log_vals]
    # Center horizontally: sum widths + gaps, then offset start so the cluster is centered
    gap = 14
    total_w = sum(2*r for r in radii) + gap * (len(radii) - 1)
    start_x = (w - total_w) / 2
    cy = h / 2 + 10  # below header
    x = start_x
    for (name, val), r in zip(items, radii):
        cx = x + r
        c = lang_color(name)
        out.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{r:.1f}" fill="{c}" fill-opacity="0.9" stroke="{c}" stroke-width="1.5"/>')
        if r >= 22:
            fs = max(10, min(15, int(r / 3.2)))
            out.append(f'<text x="{cx:.1f}" y="{cy+fs/3:.1f}" fill="#000" font-size="{fs}" font-weight="700" text-anchor="middle">{name}</text>')
        else:
            # for tiny bubbles, label below — but stagger up/down alternating to avoid overlap
            label_y = cy + r + 14
            out.append(f'<text x="{cx:.1f}" y="{label_y}" fill="{T["text_dim"]}" font-size="9" text-anchor="middle">{name}</text>')
        x += 2*r + gap
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
    out.append(f'<line x1="{pl}" y1="{pt+ph}" x2="{pl+pw}" y2="{pt+ph}" stroke="{T["border"]}"/>')
    out.append(f'<line x1="{pl}" y1="{pt}" x2="{pl}" y2="{pt+ph}" stroke="{T["border"]}"/>')
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

# ---- snake.svg ----
def render_snake():
    cell, gap = 11, 3
    weeks, days_n = 53, 7
    pl, pt = 30, 40
    w = pl + weeks * (cell + gap) + 20
    h = pt + days_n * (cell + gap) + 30
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
