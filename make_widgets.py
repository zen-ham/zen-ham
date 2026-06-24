"""Generate gitlab-readme widget SVGs for zenham, mirroring the github-only
widgets the profile used to have (anuraghazra/github-readme-stats,
ryo-ma/github-profile-trophy, DenverCoder1/streak-stats, Platane/snk,
github-readme-activity-graph).

Pulls live data from gitlab.com via the public API, renders SVGs into widgets/.
Designed to run inside a scheduled gitlab-ci job that commits the output back
to a widgets-output branch (or just main) which the profile README embeds.

Widgets produced:
  widgets/stats.svg         — counts (commits, repos, stars summed, followers)
  widgets/streak.svg        — current + longest commit streak
  widgets/activity.svg      — line chart of commits/day for last 365 days
  widgets/trophies.svg      — threshold-unlocked achievement badges
  widgets/snake.svg         — 53x7 contribution grid (no animation yet)
"""
import os, sys, json, urllib.request, urllib.error
from datetime import datetime, timedelta, timezone
from collections import defaultdict, Counter

USER = os.environ.get('GITLAB_USERNAME', 'zenham')
GL_TOKEN = os.environ.get('GITLAB_TOKEN')  # optional; auth raises rate limits
OUT = os.environ.get('OUT_DIR', 'widgets')
os.makedirs(OUT, exist_ok=True)

THEME = {
    'bg':        '#0d1117',
    'card':      '#161b22',
    'border':    '#30363d',
    'text':      '#e6edf3',
    'text_dim':  '#8b949e',
    'accent':    '#4ade80',  # green like the rest of the profile
    'accent_alt':'#58a6ff',  # blue
    'warn':      '#f59e0b',
    'err':       '#f87171',
    # contribution-grid intensity scale (low→high)
    'grid_0':    '#161b22',
    'grid_1':    '#0e4429',
    'grid_2':    '#006d32',
    'grid_3':    '#26a641',
    'grid_4':    '#39d353',
}

import time

def _fetch_with_retry(url, headers, attempts=4):
    last = None
    for n in range(attempts):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=45) as r:
                return json.loads(r.read())
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, Exception) as e:
            last = e
            wait = (2 ** n) * 2
            print(f'  retry {n+1}/{attempts} after {type(e).__name__}: {str(e)[:80]} (wait {wait}s)', flush=True)
            time.sleep(wait)
    raise last

def gl_get(path, paginate=False):
    """Hit /api/v4 GET endpoint. If paginate, follow next-page links."""
    url = f'https://gitlab.com/api/v4{path}'
    headers = {}
    if GL_TOKEN:
        headers['PRIVATE-TOKEN'] = GL_TOKEN
    if not paginate:
        return _fetch_with_retry(url, headers)
    # paginated
    all_data = []
    sep = '&' if '?' in path else '?'
    page = 1
    while True:
        purl = f'{url}{sep}per_page=100&page={page}'
        batch = _fetch_with_retry(purl, headers)
        if not batch: break
        all_data += batch
        if len(batch) < 100: break
        page += 1
        if page > 50: break  # safety
    return all_data

# ------- Data fetch -------
print(f'fetching gitlab data for {USER}…', flush=True)
user = gl_get(f'/users?username={USER}')
if not user:
    print(f'ERROR: user {USER} not found'); sys.exit(1)
user = user[0]
USER_ID = user['id']
print(f'  user_id={USER_ID}', flush=True)

projects = gl_get(f'/users/{USER_ID}/projects', paginate=True)
print(f'  {len(projects)} public projects', flush=True)
total_stars = sum(p.get('star_count', 0) for p in projects)
total_forks = sum(p.get('forks_count', 0) for p in projects)

# Events for activity / streak / snake. /events without filters caps at recent
# pages; with ?after=<date> we can walk back in time. Fetch 1 year in 30-day
# chunks so each chunk fits within the per-page cap.
events = []
seen_ids = set()
today = datetime.now(timezone.utc).date()
window = 30
chunks = 13  # 13 * 30 = ~390 days
for ci in range(chunks):
    after  = (today - timedelta(days=(ci + 1) * window)).strftime('%Y-%m-%d')
    before = (today - timedelta(days=ci * window) + timedelta(days=1)).strftime('%Y-%m-%d')
    try:
        chunk = gl_get(f'/users/{USER_ID}/events?after={after}&before={before}', paginate=True)
    except urllib.error.HTTPError as e:
        print(f'  chunk {after}..{before} failed {e.code}, skipping', flush=True)
        continue
    new = [e for e in chunk if e.get('id') and e['id'] not in seen_ids]
    for e in new: seen_ids.add(e['id'])
    events += new
    print(f'  chunk {after}..{before}: {len(chunk)} fetched ({len(new)} new, cumulative {len(events)})', flush=True)
print(f'  {len(events)} events total over ~{chunks*window} days', flush=True)

# Tally daily activity counts (any event type counts as "active that day").
daily = Counter()
push_events = 0
for e in events:
    d = e.get('created_at', '')[:10]
    if not d: continue
    daily[d] += 1
    if e.get('action_name') == 'pushed to':
        push_events += 1

# Total commits estimate: each pushed-to event has a push_data with commit_count
total_commits = sum(e.get('push_data', {}).get('commit_count', 1) for e in events
                    if e.get('action_name') == 'pushed to')

# Streak compute
def streaks(daily_counter):
    today = datetime.now(timezone.utc).date()
    cur, longest = 0, 0
    d = today
    # Find current streak (consecutive days going back from today / yesterday)
    started_streak = False
    while True:
        ds = d.strftime('%Y-%m-%d')
        if daily_counter.get(ds, 0) > 0:
            cur += 1
            started_streak = True
        else:
            if started_streak: break
            if d == today:
                # allow gap of "today" itself (haven't committed yet today, streak still valid through yesterday)
                d -= timedelta(days=1)
                continue
            break
        d -= timedelta(days=1)
    # Longest streak: walk all dates
    all_dates = sorted([datetime.strptime(k, '%Y-%m-%d').date() for k in daily_counter.keys()])
    if all_dates:
        run = 1
        for i in range(1, len(all_dates)):
            if (all_dates[i] - all_dates[i-1]).days == 1:
                run += 1
                longest = max(longest, run)
            else:
                run = 1
        longest = max(longest, run)
    return cur, longest

cur_streak, longest_streak = streaks(daily)
print(f'  cur_streak={cur_streak}  longest_streak={longest_streak}  push_events={push_events}  total_stars={total_stars}', flush=True)

# ------- SVG renderers -------
def svg_open(w, h):
    return f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}" font-family="-apple-system, BlinkMacSystemFont, Segoe UI, Helvetica, Arial, sans-serif">'

def svg_close():
    return '</svg>'

def card_bg(w, h, title=None):
    out = f'<rect x="1" y="1" rx="8" ry="8" width="{w-2}" height="{h-2}" fill="{THEME["card"]}" stroke="{THEME["border"]}"/>'
    if title:
        out += f'<text x="20" y="30" fill="{THEME["accent"]}" font-size="16" font-weight="700">{title}</text>'
    return out

# ----- stats.svg
def render_stats():
    w, h = 480, 180
    rows = [
        ('Total Public Repos', len(projects)),
        ('Total Stars Received', total_stars),
        ('Total Forks Received', total_forks),
        ('Commits (recent events)', total_commits),
    ]
    out = [svg_open(w, h), card_bg(w, h, f'{USER} • gitlab stats')]
    y = 65
    for label, val in rows:
        out.append(f'<text x="20" y="{y}" fill="{THEME["text_dim"]}" font-size="13">{label}</text>')
        out.append(f'<text x="{w-20}" y="{y}" fill="{THEME["text"]}" font-size="13" font-weight="600" text-anchor="end">{val:,}</text>')
        y += 25
    out.append(svg_close())
    return ''.join(out)

# ----- streak.svg
def render_streak():
    w, h = 480, 195
    out = [svg_open(w, h), card_bg(w, h, 'commit streak')]
    # Three stats: current, longest, total active days
    active_days = sum(1 for v in daily.values() if v > 0)
    cells = [
        ('Current Streak', f'{cur_streak} days', THEME['accent']),
        ('Longest Streak', f'{longest_streak} days', THEME['accent_alt']),
        ('Active Days',    f'{active_days}',     THEME['warn']),
    ]
    cx = w / len(cells)
    for i, (label, val, color) in enumerate(cells):
        x_center = cx * i + cx / 2
        out.append(f'<text x="{x_center}" y="100" fill="{color}" font-size="28" font-weight="700" text-anchor="middle">{val}</text>')
        out.append(f'<text x="{x_center}" y="135" fill="{THEME["text_dim"]}" font-size="12" text-anchor="middle">{label}</text>')
    out.append(f'<text x="{w/2}" y="170" fill="{THEME["text_dim"]}" font-size="10" text-anchor="middle">(based on recent events, gitlab API caps history depth)</text>')
    out.append(svg_close())
    return ''.join(out)

# ----- activity.svg (line chart of last 90 days)
def render_activity():
    w, h = 720, 220
    out = [svg_open(w, h), card_bg(w, h, 'recent activity (events/day)')]
    days = 90
    today = datetime.now(timezone.utc).date()
    points = []
    for i in range(days):
        d = today - timedelta(days=days - 1 - i)
        ds = d.strftime('%Y-%m-%d')
        points.append((d, daily.get(ds, 0)))
    max_v = max((p[1] for p in points), default=1) or 1
    pad_l, pad_r, pad_t, pad_b = 50, 20, 50, 35
    pw = w - pad_l - pad_r
    ph = h - pad_t - pad_b
    # axis
    out.append(f'<line x1="{pad_l}" y1="{pad_t+ph}" x2="{pad_l+pw}" y2="{pad_t+ph}" stroke="{THEME["border"]}"/>')
    out.append(f'<line x1="{pad_l}" y1="{pad_t}" x2="{pad_l}" y2="{pad_t+ph}" stroke="{THEME["border"]}"/>')
    # line
    coords = []
    for i, (d, v) in enumerate(points):
        x = pad_l + (i / (days - 1)) * pw
        y = pad_t + ph - (v / max_v) * ph
        coords.append((x, y))
    poly = ' '.join(f'{x:.1f},{y:.1f}' for x, y in coords)
    out.append(f'<polyline points="{poly}" fill="none" stroke="{THEME["accent"]}" stroke-width="2"/>')
    # x labels (months)
    last_month = None
    for i, (d, v) in enumerate(points):
        m = d.strftime('%b')
        if m != last_month:
            x = pad_l + (i / (days - 1)) * pw
            out.append(f'<text x="{x}" y="{pad_t+ph+18}" fill="{THEME["text_dim"]}" font-size="10" text-anchor="middle">{m}</text>')
            last_month = m
    # y max label
    out.append(f'<text x="{pad_l-8}" y="{pad_t+5}" fill="{THEME["text_dim"]}" font-size="10" text-anchor="end">{max_v}</text>')
    out.append(f'<text x="{pad_l-8}" y="{pad_t+ph}" fill="{THEME["text_dim"]}" font-size="10" text-anchor="end">0</text>')
    out.append(svg_close())
    return ''.join(out)

# ----- trophies.svg (achievements based on simple thresholds)
def render_trophies():
    w, h = 720, 180
    out = [svg_open(w, h), card_bg(w, h, 'achievements')]
    thresholds = [
        ('🏠', 'Repo Owner',    len(projects),      [1, 5, 15, 30]),
        ('⭐', 'Starred',       total_stars,        [1, 5, 25, 100]),
        ('🔁', 'Forked',        total_forks,        [1, 5, 25, 100]),
        ('📝', 'Commits',       total_commits,      [10, 50, 200, 1000]),
        ('🔥', 'Streak',        longest_streak,     [3, 7, 14, 30]),
        ('🎯', 'Active Days',   sum(1 for v in daily.values() if v > 0), [5, 20, 50, 200]),
    ]
    cols = 6
    cw = w / cols
    for i, (icon, label, val, levels) in enumerate(thresholds):
        # determine tier
        tier = 0
        for L in levels:
            if val >= L: tier += 1
        tier_color = [THEME['border'], '#a78bfa', THEME['accent_alt'], THEME['accent'], THEME['warn']][tier]
        tier_label = ['Locked', 'Bronze', 'Silver', 'Gold', 'Platinum'][tier]
        cx = cw * i + cw / 2
        out.append(f'<circle cx="{cx}" cy="80" r="32" fill="none" stroke="{tier_color}" stroke-width="3"/>')
        out.append(f'<text x="{cx}" y="92" font-size="28" text-anchor="middle">{icon}</text>')
        out.append(f'<text x="{cx}" y="135" fill="{THEME["text"]}" font-size="12" font-weight="600" text-anchor="middle">{label}</text>')
        out.append(f'<text x="{cx}" y="150" fill="{tier_color}" font-size="11" text-anchor="middle">{tier_label}</text>')
        out.append(f'<text x="{cx}" y="165" fill="{THEME["text_dim"]}" font-size="10" text-anchor="middle">{val}</text>')
    out.append(svg_close())
    return ''.join(out)

# ----- snake.svg (contribution grid — 53 weeks x 7 days; no animation yet)
def render_snake():
    cell, gap = 11, 3
    weeks = 53
    days = 7
    pad_l, pad_t = 30, 30
    w = pad_l + weeks * (cell + gap) + 20
    h = pad_t + days * (cell + gap) + 30
    out = [svg_open(w, h), card_bg(w, h, '53-week contribution grid')]
    today = datetime.now(timezone.utc).date()
    # Anchor the grid so the last column ends on today's week (sunday start)
    today_weekday = today.weekday()  # 0=mon
    end_sunday = today - timedelta(days=(today_weekday + 1) % 7)
    start = end_sunday - timedelta(weeks=weeks - 1)
    for wi in range(weeks):
        for di in range(days):
            d = start + timedelta(weeks=wi, days=di)
            ds = d.strftime('%Y-%m-%d')
            v = daily.get(ds, 0)
            level = 0
            if v >= 1: level = 1
            if v >= 3: level = 2
            if v >= 6: level = 3
            if v >= 10: level = 4
            color = THEME[f'grid_{level}']
            x = pad_l + wi * (cell + gap)
            y = pad_t + di * (cell + gap)
            out.append(f'<rect x="{x}" y="{y}" width="{cell}" height="{cell}" rx="2" ry="2" fill="{color}"/>')
    # day labels (left)
    for di, name in enumerate(['', 'M', '', 'W', '', 'F', '']):
        if not name: continue
        y = pad_t + di * (cell + gap) + 9
        out.append(f'<text x="20" y="{y}" fill="{THEME["text_dim"]}" font-size="9" text-anchor="end">{name}</text>')
    # legend
    legend_x = w - 160
    out.append(f'<text x="{legend_x-10}" y="{h-12}" fill="{THEME["text_dim"]}" font-size="9" text-anchor="end">Less</text>')
    for i in range(5):
        out.append(f'<rect x="{legend_x + i*15}" y="{h-22}" width="{cell}" height="{cell}" rx="2" fill="{THEME[f"grid_{i}"]}"/>')
    out.append(f'<text x="{legend_x + 5*15 + 5}" y="{h-12}" fill="{THEME["text_dim"]}" font-size="9">More</text>')
    out.append(svg_close())
    return ''.join(out)

# ------- write files -------
for name, fn in [('stats', render_stats), ('streak', render_streak),
                 ('activity', render_activity), ('trophies', render_trophies),
                 ('snake', render_snake)]:
    p = os.path.join(OUT, f'{name}.svg')
    with open(p, 'w', encoding='utf-8') as f:
        f.write(fn())
    print(f'wrote {p} ({os.path.getsize(p)} bytes)', flush=True)

print('done.')
