#!/usr/bin/env python3
"""Incremental weekly refresh of the embedded RAW_DATA in the CFB dashboard.

Usage:  CFBD_API_KEY=... python refresh_cfb.py index.html [--selftest]

Starting at the latest week that has completed games, it looks for completed games
that are not yet marked completed in teamSchedule, pulls team/player/play data for
that week, merges it, and moves on to the next week. It stops at the first week with
no newly completed games, so a half-finished week is simply finished on the next run.
Stdlib only.
"""
import os, sys, re, json, time, datetime, urllib.request, urllib.parse, urllib.error

YEAR = 2026
BASE = 'https://api.collegefootballdata.com'
CONF_CODES = ['ACC', 'SEC', 'B12', 'B1G', 'MWC', 'PAC', 'AAC', 'CUSA', 'MAC', 'SBC']
INDEPENDENTS = ['Notre Dame', 'UConn']
CALLS = 0


# ----------------------------------------------------------------- helpers
def rnd(x, nd=0):
    """round half away from zero"""
    m = 10 ** nd
    return (int(x * m + 0.5) if x >= 0 else -int(-x * m + 0.5)) / m


def f1(x):
    return '%.1f' % rnd(x, 1)


def f3(x):
    return '%.3f' % rnd(x, 3)


def num(s, default=0.0):
    try:
        return float(str(s).replace(',', ''))
    except Exception:
        return default


def fmtn(x):
    x = float(x)
    return str(int(x)) if x == int(x) else repr(round(x, 4))


def frac(s):
    """'12/19' -> (12, 19)"""
    m = re.match(r'^\s*(-?\d+)\s*[/-]\s*(-?\d+)\s*$', str(s))
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def api(path, params):
    global CALLS
    key = os.environ.get('CFBD_API_KEY', '')
    url = BASE + path + '?' + urllib.parse.urlencode(params)
    for attempt in range(4):
        req = urllib.request.Request(url, headers={'Authorization': 'Bearer ' + key,
                                                   'Accept': 'application/json'})
        try:
            CALLS += 1
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.loads(r.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < 3:
                time.sleep(5 * (attempt + 1))
                continue
            raise SystemExit('API error %s for %s' % (e.code, path))
        except urllib.error.URLError:
            if attempt < 3:
                time.sleep(5)
                continue
            raise


def pull(path, week):
    """Call a per-week endpoint once per conference code and once per independent."""
    out = []
    base = {'year': YEAR, 'week': week, 'seasonType': 'regular'}
    for c in CONF_CODES:
        out.append(api(path, dict(base, conference=c)))
    for t in INDEPENDENTS:
        out.append(api(path, dict(base, team=t)))
    return out


# ----------------------------------------------------------------- data io
RAW_RE = re.compile(r'const RAW_DATA = (\{.*?\});\s*const \{', re.S)


def load(html):
    m = RAW_RE.search(html)
    if not m:
        raise SystemExit('RAW_DATA not found')
    return m, json.loads(m.group(1))


def store(html, m, D):
    new = 'const RAW_DATA = ' + json.dumps(D, separators=(',', ':'), ensure_ascii=False) + ';'
    # keep the trailing "const {" that the regex consumed context for
    return html[:m.start()] + new + html[m.end(1) + 1:]


# ----------------------------------------------------------------- schedule
def sched_entry(D, team, week):
    for e in D['teamSchedule'].get(team, []):
        if e['week'] == week:
            return e
    return None


def merge_schedule(D, games):
    """Apply completed games to teamSchedule for tracked teams. Returns count."""
    tracked = set(D['teamSchedule'])
    n = 0
    for g in games:
        if not g.get('completed') or g.get('homePoints') is None or g.get('awayPoints') is None:
            continue
        sides = [(g['homeTeam'], g['awayTeam'], 'home', g['homePoints'], g['awayPoints']),
                 (g['awayTeam'], g['homeTeam'], 'away', g['awayPoints'], g['homePoints'])]
        for team, opp, ha, tp, op in sides:
            if team not in tracked:
                continue
            res = 'W' if tp > op else ('L' if tp < op else 'T')
            old = sched_entry(D, team, g['week'])
            date = (old or {}).get('date') or g['startDate'][:10]
            ent = {'week': g['week'], 'opponent': opp, 'homeAway': ha, 'date': date,
                   'completed': True, 'teamPoints': tp, 'oppPoints': op, 'result': res,
                   'neutralSite': bool(g.get('neutralSite'))}
            lst = D['teamSchedule'][team]
            if old is not None:
                lst[lst.index(old)] = ent
            else:
                lst.append(ent)
                lst.sort(key=lambda e: e['week'])
            n += 1
    return n


def new_completed(D, games):
    """completed API games where a tracked team's schedule entry isn't completed yet"""
    tracked = set(D['teamSchedule'])
    out = []
    for g in games:
        if not g.get('completed') or g.get('homePoints') is None:
            continue
        for team in (g['homeTeam'], g['awayTeam']):
            if team in tracked:
                e = sched_entry(D, team, g['week'])
                if e is None or not e.get('completed'):
                    out.append(g)
                    break
    return out


# ----------------------------------------------------------------- team stats
def team_stat_map(tm):
    return {s['category']: s['stat'] for s in tm.get('stats', [])}


def ival(st, k):
    return int(num(st.get(k, 0)))


def merge_team_stats(D, gt_lists, sched_games):
    """Rewrite teamGameStats entries for tracked teams from /games/teams payloads."""
    tracked = set(D['teamSchedule'])
    seen = set()
    last = {}   # team -> dict of last-game extras for teamBoxScores
    week_of = {g['id']: g for g in sched_games}
    for lst in gt_lists:
        for gm in lst:
            if gm['id'] in seen or len(gm.get('teams', [])) != 2:
                continue
            seen.add(gm['id'])
            g = week_of.get(gm['id'])
            if g is None:
                continue
            a, b = gm['teams']
            sa, sb = team_stat_map(a), team_stat_map(b)
            for me, op, sm, so in ((a, b, sa, sb), (b, a, sb, sa)):
                name = me['team']
                if name not in tracked:
                    continue
                old = sched_entry(D, name, g['week']) or {}
                td = ival(sm, 'rushingTDs') + ival(sm, 'passingTDs')
                ent = {'week': g['week'], 'date': old.get('date') or g['startDate'][:10],
                       'opponent': op['team'], 'points': me['points'], 'pointsAllowed': op['points'],
                       'totalYards': ival(sm, 'totalYards'), 'passingYards': ival(sm, 'netPassingYards'),
                       'rushingYards': ival(sm, 'rushingYards'), 'turnovers': ival(sm, 'turnovers'),
                       'turnoversForced': ival(so, 'turnovers'), 'firstDowns': ival(sm, 'firstDowns'),
                       'sacks': ival(sm, 'sacks'), 'touchdowns': td,
                       '_oppTotalYards': ival(so, 'totalYards'),
                       '_oppPassingYards': ival(so, 'netPassingYards'),
                       '_oppRushingYards': ival(so, 'rushingYards')}
                lst2 = D['teamGameStats'].setdefault(name, [])
                lst2[:] = [e for e in lst2 if e['week'] != g['week']]
                lst2.append(ent)
                lst2.sort(key=lambda e: e['week'])
                last.setdefault(name, {})[g['week']] = {
                    'thirdDownEff': sm.get('thirdDownEff'), 'fourthDownEff': sm.get('fourthDownEff'),
                    'possessionTime': sm.get('possessionTime'),
                    'penaltyYards': sm.get('totalPenaltiesYards')}
    return last


BOX_SUM = ['points', 'pointsAllowed', 'totalYards', 'passingYards', 'rushingYards', 'turnovers',
           'turnoversForced', 'firstDowns', 'sacks', 'touchdowns']


def recompute_box(D, last=None):
    for team, games in D['teamGameStats'].items():
        box = D['teamBoxScores'].setdefault(team, {})
        for k in BOX_SUM:
            box[k] = sum(g.get(k, 0) for g in games)
        if last and team in last:
            wk = max(last[team])
            for k, v in last[team][wk].items():
                if v is not None:
                    box[k] = v


# ----------------------------------------------------------------- standings etc
def recompute_standings(D):
    t2c = D['teamToConf']
    old = {s['t']: s for s in D['standings']}
    max_week = max((e['week'] for l in D['teamSchedule'].values() for e in l if e['completed']), default=0)
    out = []
    for s in D['standings']:
        team = s['t']
        sched = D['teamSchedule'][team]
        done = sorted([e for e in sched if e['completed']], key=lambda e: e['week'])
        w = sum(1 for e in done if e['result'] == 'W')
        l = sum(1 for e in done if e['result'] == 'L')
        cw = cl = 0
        for e in done:
            if t2c.get(e['opponent']) and t2c.get(e['opponent']) == t2c.get(team) \
                    and t2c.get(team) != 'Independents':
                if e['result'] == 'W':
                    cw += 1
                elif e['result'] == 'L':
                    cl += 1
        pf = sum(e['teamPoints'] for e in done)
        pa = sum(e['oppPoints'] for e in done)
        streak = ''
        if done:
            r = done[-1]['result']
            n = 0
            for e in reversed(done):
                if e['result'] == r:
                    n += 1
                else:
                    break
            streak = r + str(n)
        nxt = next((e for e in sorted(sched, key=lambda e: e['week']) if not e['completed']), None)
        ns = dict(s)
        ns.update({'opponent': done[-1]['opponent'] if done else s.get('opponent'), 'week': done[-1]['week'] if done else max_week,
                   'wins': w, 'losses': l, 'confWins': cw, 'confLosses': cl, 'pointsFor': pf,
                   'pointsAgainst': pa, 'pointDiff': pf - pa, 'streak': streak, 'gamesPlayed': len(done)})
        out.append(ns)
    D['standings'][:] = out


def recompute_next_opponent(D):
    for team, sched in D['teamSchedule'].items():
        nxt = next((e for e in sorted(sched, key=lambda e: e['week']) if not e['completed']), None)
        if nxt:
            D['nextOpponent'][team] = {'week': nxt['week'], 'opponent': nxt['opponent'],
                                       'homeAway': nxt['homeAway'], 'date': nxt['date']}


# ----------------------------------------------------------------- plays
def merge_plays(D, play_lists, week, game_ids):
    tracked = set(D['teamSchedule'])
    seen = set()
    new = {t: [] for t in tracked}
    for lst in play_lists:
        for p in lst:
            if p['id'] in seen or p['gameId'] not in game_ids or p.get('ppa') is None:
                continue
            seen.add(p['id'])
            for team, other, off in ((p['offense'], p['defense'], True), (p['defense'], p['offense'], False)):
                if team in tracked:
                    new[team].append({'down': p['down'], 'dist': p['distance'], 'ytg': p['yardsToGoal'],
                                      'yds': p['yardsGained'], 'type': p['playType'], 'ppa': round(p['ppa'], 2),
                                      'scoring': bool(p['scoring']), 'text': (p['playText'] or '')[:110], 'wk': week,
                                      'opp': other, 'off': off})
    for team, add in new.items():
        cur = D['teamPlays'].setdefault(team, [])
        if add or any(p['wk'] == week for p in cur):
            cur[:] = [p for p in cur if p['wk'] != week] + add
    return len(seen)


# ----------------------------------------------------------------- players
LONG_CATS = {'rushing', 'receiving', 'kickReturns', 'puntReturns', 'punting', 'kicking'}


def parse_player_game_stats(cat, types):
    """types: {typeName: stat string} -> dict in dashboard format"""
    g = lambda k: types.get(k, '0')
    primary = {'passing': None, 'rushing': 'CAR', 'receiving': 'REC', 'kickReturns': 'NO',
               'puntReturns': 'NO', 'punting': 'NO', 'interceptions': 'INT'}
    if cat in primary and cat != 'passing' and num(g(primary[cat])) == 0:
        return None
    if cat == 'passing':
        c, a = frac(g('C/ATT'))
        return {'ATT': str(a), 'COMPLETIONS': str(c), 'INT': g('INT'), 'PCT': f3(c / a) if a else '0.000',
                'TD': g('TD'), 'YDS': g('YDS'), 'YPA': g('AVG')}
    if cat == 'rushing':
        return {'CAR': g('CAR'), 'LONG': g('LONG'), 'TD': g('TD'), 'YDS': g('YDS'), 'YPC': g('AVG')}
    if cat == 'receiving':
        return {'LONG': g('LONG'), 'REC': g('REC'), 'TD': g('TD'), 'YDS': g('YDS'), 'YPR': g('AVG')}
    if cat in ('kickReturns', 'puntReturns'):
        return {'AVG': g('AVG'), 'LONG': g('LONG'), 'NO': g('NO'), 'TD': g('TD'), 'YDS': g('YDS')}
    if cat == 'defensive':
        if num(g('TOT')) == 0:
            return None
        return {k: g(k) for k in ('PD', 'QB HUR', 'SACKS', 'SOLO', 'TD', 'TFL', 'TOT')}
    if cat == 'fumbles':
        return {k: g(k) for k in ('FUM', 'LOST', 'REC')}
    if cat == 'interceptions':
        return {'AVG': f1(num(g('YDS')) / num(g('INT'))), 'INT': g('INT'), 'TD': g('TD'), 'YDS': g('YDS')}
    if cat == 'kicking':
        fm, fa = frac(g('FG'))
        xm, xa = frac(g('XP'))
        return {'FGA': str(fa), 'FGM': str(fm), 'LONG': g('LONG'), 'PCT': f3(fm / fa) if fa else '0', 'PTS': g('PTS'),
                'XPA': str(xa), 'XPM': str(xm)}
    if cat == 'punting':
        return {'In 20': g('In 20'), 'LONG': g('LONG'), 'NO': g('NO'), 'TB': g('TB'), 'YDS': g('YDS'),
                'YPP': g('AVG')}
    return None


def recompute_player_season(games):
    """games: iterable of per-game 'stats' dicts -> season dict"""
    acc = {}
    for gs in games:
        for cat, st in gs.items():
            a = acc.setdefault(cat, {})
            for k, v in st.items():
                x = num(v)
                if k == 'LONG':
                    a[k] = max(a.get(k, x), x)
                elif k in ('PCT', 'YPA', 'YPC', 'YPR', 'AVG', 'YPP'):
                    continue
                else:
                    a[k] = a.get(k, 0) + x
    out = {}
    for cat, a in acc.items():
        o = {}
        if cat == 'passing':
            att, comp, yds = a.get('ATT', 0), a.get('COMPLETIONS', 0), a.get('YDS', 0)
            o = {'ATT': fmtn(att), 'COMPLETIONS': fmtn(comp), 'INT': fmtn(a.get('INT', 0)),
                 'TD': fmtn(a.get('TD', 0)), 'YDS': fmtn(yds),
                 'PCT': f3(comp / att) if att else '0.000', 'YPA': f1(yds / att) if att else '0.0'}
        elif cat == 'rushing':
            c, y = a.get('CAR', 0), a.get('YDS', 0)
            o = {'CAR': fmtn(c), 'TD': fmtn(a.get('TD', 0)), 'YDS': fmtn(y), 'LONG': fmtn(a.get('LONG', 0)),
                 'YPC': f1(y / c) if c else '0.0'}
        elif cat == 'receiving':
            c, y = a.get('REC', 0), a.get('YDS', 0)
            o = {'REC': fmtn(c), 'TD': fmtn(a.get('TD', 0)), 'YDS': fmtn(y), 'LONG': fmtn(a.get('LONG', 0)),
                 'YPR': f1(y / c) if c else '0.0'}
        elif cat in ('kickReturns', 'puntReturns'):
            c, y = a.get('NO', 0), a.get('YDS', 0)
            o = {'NO': fmtn(c), 'TD': fmtn(a.get('TD', 0)), 'YDS': fmtn(y), 'LONG': fmtn(a.get('LONG', 0)),
                 'AVG': f1(y / c) if c else '0.0'}
        elif cat == 'defensive':
            o = {k: fmtn(a.get(k, 0)) for k in ('PD', 'QB HUR', 'SACKS', 'SOLO', 'TD', 'TFL', 'TOT')}
        elif cat == 'fumbles':
            o = {k: fmtn(a.get(k, 0)) for k in ('FUM', 'LOST', 'REC')}
        elif cat == 'interceptions':
            c, y = a.get('INT', 0), a.get('YDS', 0)
            o = {'INT': fmtn(c), 'TD': fmtn(a.get('TD', 0)), 'YDS': fmtn(y), 'AVG': f1(y / c) if c else '0.0'}
        elif cat == 'kicking':
            fa, fm, xa, xm = a.get('FGA', 0), a.get('FGM', 0), a.get('XPA', 0), a.get('XPM', 0)
            o = {'FGA': fmtn(fa), 'FGM': fmtn(fm), 'PTS': fmtn(a.get('PTS', 0)), 'XPA': fmtn(xa),
                 'XPM': fmtn(xm), 'LONG': fmtn(a.get('LONG', 0)), 'PCT': f3(fm / fa) if fa else '0.000'}
        elif cat == 'punting':
            c, y = a.get('NO', 0), a.get('YDS', 0)
            o = {'In 20': fmtn(a.get('In 20', 0)), 'NO': fmtn(c), 'TB': fmtn(a.get('TB', 0)), 'YDS': fmtn(y),
                 'LONG': fmtn(a.get('LONG', 0)), 'YPP': f1(y / c) if c else '0.0'}
        out[cat] = o
    return out


def extract_game_players(gp_lists, tracked, week_games):
    """-> {(playerId, gameId): (name, team, opponent, {cat: stats})}"""
    res = {}
    for lst in gp_lists:
        for gm in lst:
            g = week_games.get(gm['id'])
            if g is None or len(gm.get('teams', [])) != 2:
                continue
            for tm, other in ((gm['teams'][0], gm['teams'][1]), (gm['teams'][1], gm['teams'][0])):
                if tm['team'] not in tracked:
                    continue
                ath = {}
                for c in tm.get('categories', []):
                    for ty in c.get('types', []):
                        for a in ty.get('athletes', []):
                            d = ath.setdefault(a['id'], {'name': a['name'], 'cats': {}})
                            d['cats'].setdefault(c['name'], {})[ty['name']] = a['stat']
                for pid, d in ath.items():
                    stats = {}
                    for cat, types in d['cats'].items():
                        p = parse_player_game_stats(cat, types)
                        if p is not None:
                            stats[cat] = p
                    res[(pid, gm['id'])] = (d['name'], tm['team'], other['team'], stats)
    return res


def merge_players(D, gp_lists, week, week_games):
    tracked = set(D['teamSchedule'])
    got = extract_game_players(gp_lists, tracked, week_games)
    byid = {p['id']: p for p in D['players']}
    touched = set()
    wk = str(week)
    for (pid, gid), (name, team, opp, stats) in got.items():
        if pid.startswith('-'):
            continue                       # the "Team" pseudo-athlete
        p = byid.get(pid)
        if p is None:
            p = {'id': pid, 'name': name.strip(), 'team': team, 'position': '', 'season': {}, 'games': {}}
            D['players'].append(p)
            byid[pid] = p
        p['team'] = team
        p['games'][wk] = {'week': week, 'opponent': opp, 'gameId': gid, 'stats': stats}
        touched.add(pid)
    # a week re-pull rebuilds every game of that week, so drop stale entries for players not returned
    for p in D['players']:
        if p['id'] not in touched and wk in p['games'] and p['games'][wk].get('gameId') in week_games:
            del p['games'][wk]
            touched.add(p['id'])
    for pid in touched:
        p = byid[pid]
        p['season'] = recompute_player_season([g['stats'] for g in p['games'].values()])
    return len(got)


# ----------------------------------------------------------------- main
def selftest(D):
    """Recompute derived structures from current data and report differences."""
    import copy
    E = copy.deepcopy(D)
    recompute_box(E)
    bad = 0
    for t in D['teamBoxScores']:
        for k in BOX_SUM:
            if E['teamBoxScores'][t].get(k) != D['teamBoxScores'][t].get(k):
                bad += 1
                if bad < 6:
                    print('box diff', t, k, D['teamBoxScores'][t].get(k), E['teamBoxScores'][t].get(k))
    recompute_standings(E)
    sd = 0
    for a, b in zip(D['standings'], E['standings']):
        for k in b:
            if a.get(k) != b.get(k):
                sd += 1
                if sd < 8:
                    print('standing diff', a['t'], k, a.get(k), b.get(k))
    pd = 0
    for p in D['players']:
        s = recompute_player_season([g['stats'] for g in p['games'].values()])
        for cat, st in s.items():
            for k, v in st.items():
                ov = p['season'].get(cat, {}).get(k)
                if ov is None or abs(num(ov) - num(v)) > 0.051:
                    pd += 1
                    if pd < 8:
                        print('player diff', p['name'], cat, k, ov, v)
    print('selftest: box diffs %d, standings diffs %d, player season diffs %d' % (bad, sd, pd))


def process_week(D, week, games):
    """games: list of API game dicts for the week (deduped)."""
    done = [g for g in games if g.get('completed') and g.get('homePoints') is not None]
    wk_games = {g['id']: g for g in done}
    print('Week %d: %d completed games; pulling team stats, players, plays' % (week, len(done)))
    n = merge_schedule(D, done)
    gt = pull('/games/teams', week)
    last = merge_team_stats(D, gt, done)
    gp = pull('/games/players', week)
    npl = merge_players(D, gp, week, wk_games)
    pl = pull('/plays', week)
    nplays = merge_plays(D, pl, week, set(wk_games))
    recompute_box(D, last)
    print('  schedule entries %d, players-games %d, plays %d' % (n, npl, nplays))


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    path = sys.argv[1]
    html = open(path, encoding='utf-8').read()
    m, D = load(html)
    if '--selftest' in sys.argv:
        selftest(D)
        return
    if not os.environ.get('CFBD_API_KEY'):
        raise SystemExit('CFBD_API_KEY is not set')
    week = max((e['week'] for l in D['teamSchedule'].values() for e in l if e['completed']), default=0)
    start = week or 1
    changed = False
    while week <= 16:
        if week == 0:
            week = 1
        base = {'year': YEAR, 'week': week, 'seasonType': 'regular'}
        games, seen = [], set()
        for c in CONF_CODES:
            for g in api('/games', dict(base, conference=c)):
                if g['id'] not in seen:
                    seen.add(g['id']); games.append(g)
        for t in INDEPENDENTS:
            for g in api('/games', dict(base, team=t)):
                if g['id'] not in seen:
                    seen.add(g['id']); games.append(g)
        fresh = new_completed(D, games)
        if not fresh:
            if week == start:          # latest week already fully loaded; look at the next one
                week += 1
                continue
            if not changed:
                print('No completed games for this week yet (week %d) -- exiting without changes.' % week)
            break
        process_week(D, week, games)
        changed = True
        week += 1
    if not changed:
        return
    recompute_standings(D)
    recompute_next_opponent(D)
    html = store(html, m, D)
    today = datetime.date.today()
    html = re.sub(r"LAST_UPDATE_DATE\s*=\s*'[^']*'",
                  "LAST_UPDATE_DATE = '%s'" % (today.strftime('%B ') + str(today.day) + today.strftime(', %Y')), html)
    open(path, 'w', encoding='utf-8').write(html)
    print('Done. API calls used: %d' % CALLS)


if __name__ == '__main__':
    main()
