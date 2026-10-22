#!/usr/bin/env python3
"""claude-dash: a terminal dashboard around Claude Code, built on tmux.

  ┌ Sessions ─────────────┬──────────────────────┬ Subagents ───────┐
  │ ● DPG-1543 … working  │                      ├ Approach & Review┤
  │ ✓ DPG-1509 … finished │    Claude (live)     ├ Skills|Agents|MCP┤
  ├ Computer ─┬ Claude ───┤                      ├ Model & Effort ──┤
  └───────────┴───────────┴──────────────────────┴──────────────────┘

  claude-dash            start the dashboard (or reattach to it)
  claude-dash demo       a preview with made-up data, in its own tmux session
  claude-dash stop       close the dashboard
  claude-dash relayout   rebuild the side panes around Claude, keeping it running
  claude-dash help       this text

Each pane runs `dash.py <pane>`; the panes and the Claude mod talk through files in ~/.claude/claude-dash.
"""
import curses, glob, json, locale, os, re, shlex, shutil, subprocess, sys, textwrap, time, uuid
from datetime import datetime

HOME = os.path.expanduser('~')
DIR = os.environ.get('CLAUDE_DASH_HOME', os.path.join(HOME, '.claude', 'claude-dash'))  # state the panes share
STATE = os.path.join(DIR, 'state')
OVERRIDE = os.path.join(DIR, 'override')
CURRENT = os.path.join(DIR, 'current')
MOD = os.environ.get('CLAUDE_DASH_MOD', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'mod'))  # the mod beside this file
TMUX = os.environ.get('CLAUDE_DASH_TMUX', 'claude-dash')
T = f'={TMUX}:'  # exact tmux target: a bare name would also match claude-dash-stash
DEMO = bool(os.environ.get('CLAUDE_DASH_DEMO'))
MAC = sys.platform == 'darwin'
SELF = os.path.abspath(__file__)
JUMP_KEY = 'C-\\'  # Esc stays Claude's (it interrupts a turn)

ORDER = ['running', 'waiting', 'idle', 'completed', 'ended']
ICON = {'running': '●', 'waiting': '◐', 'idle': '○', 'completed': '✓', 'ended': '◌'}
WORD = {'running': 'working', 'waiting': 'input', 'idle': 'idle', 'completed': 'finished', 'ended': 'ended'}
TONE = {'running': 'orange', 'waiting': 'yellow', 'idle': 'grey', 'completed': 'green', 'ended': 'dim'}
MODELS = [('auto', None), ('Opus', 'claude-opus-5-5[1m]'), ('Sonnet', 'claude-sonnet-5-5'),
          ('Haiku', 'claude-haiku-5-5'), ('Fable', 'claude-fable-5-1')]
EFFORTS = [('auto', None), ('low', 'low'), ('med', 'medium'), ('high', 'high'), ('xhigh', 'xhigh'), ('max', 'max')]
BUILTIN_AGENTS = [
    ('general-purpose', 'Research, code search and multi-step tasks.'),
    ('Explore', 'Read-only fast search across many files; returns conclusions.'),
    ('Plan', 'Designs an implementation plan; finds critical files and trade-offs.'),
    ('claude-code-guide', 'Answers questions about Claude Code, the Agent SDK and the API.'),
    ('statusline-setup', 'Configures the Claude Code status line.'),
]

C = {}  # tone name -> curses attr, filled by palette()


def palette():
    curses.start_color()
    curses.use_default_colors()
    rich = curses.COLORS >= 256
    tones = {  # name: (256-colour, 8-colour fallback)
        'green': (114, curses.COLOR_GREEN), 'orange': (208, curses.COLOR_YELLOW), 'yellow': (221, curses.COLOR_YELLOW),
        'grey': (244, curses.COLOR_WHITE), 'cyan': (81, curses.COLOR_CYAN), 'magenta': (176, curses.COLOR_MAGENTA),
        'red': (203, curses.COLOR_RED), 'blue': (75, curses.COLOR_BLUE), 'dim': (240, curses.COLOR_WHITE),
    }
    for i, (name, (c256, c8)) in enumerate(tones.items(), start=1):
        curses.init_pair(i, c256 if rich else c8, -1)
        C[name] = curses.color_pair(i)
        curses.init_pair(40 + i, c256 if rich else c8, 237 if rich else -1)
        C['sel:' + name] = curses.color_pair(40 + i) | (0 if rich else curses.A_REVERSE)
    n = len(tones) + 1
    curses.init_pair(n, 16 if rich else curses.COLOR_BLACK, 114 if rich else curses.COLOR_GREEN)
    C['badge'] = curses.color_pair(n) | curses.A_BOLD
    curses.init_pair(n + 1, 16 if rich else curses.COLOR_BLACK, 81 if rich else curses.COLOR_CYAN)
    C['chip'] = curses.color_pair(n + 1) | curses.A_BOLD
    curses.init_pair(n + 2, -1, 237 if rich else -1)
    C['sel'] = curses.color_pair(n + 2) | (0 if rich else curses.A_REVERSE)
    if not rich:
        C['dim'] |= curses.A_DIM
        C['grey'] |= curses.A_DIM


def paint(tone, selected=False):
    """A tone's attr, on the highlight background when the row is selected."""
    if not tone:
        return C['sel'] if selected else 0
    return C['sel:' + tone] if selected else C[tone]


def tmux(*args):
    return subprocess.run(['tmux', *args], capture_output=True, text=True, stdin=subprocess.DEVNULL).stdout.strip()


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except (OSError, TypeError):
        return False


def registry():
    regs = []
    for f in glob.glob(f'{HOME}/.claude/sessions/*.json'):
        try:
            regs.append(json.load(open(f)))
        except (OSError, ValueError):
            pass
    return regs


def pane_sid(pane):
    return tmux('show', '-p', '-t', pane, '-v', '@dash_sid') if pane else ''


def current_id():
    """The session the middle pane shows: the tag the dashboard put on that pane, else Claude's registry
    entry for it (followed to the background session it handed off to), else the last one opened."""
    main = tmux('show', '-t', T, '-v', '@dash_main')
    tagged = pane_sid(main)
    if tagged:
        return canonical(tagged)
    regs = registry()
    for d in regs:
        if main and (d.get('tmux') or '').startswith(f'{TMUX}:') and d['tmux'].endswith('.' + main) and alive(d.get('pid')):
            job = d.get('parkedJobId')
            bg = next((b['sessionId'] for b in regs if job and b.get('jobId') == job), None)
            return bg or d['sessionId']
    # `claude attach <id>` views a background session without registering itself: read the id off the command
    m = re.search(r'claude attach (\S+)', tmux('display', '-p', '-t', main, '#{pane_start_command}') if main else '')
    if m:
        short = m.group(1).strip('"\'')
        hit = next((d['sessionId'] for d in regs if d.get('jobId') == short or d.get('sessionId', '').startswith(short)), None)
        if hit:
            return hit
    try:
        return open(CURRENT).read().strip()
    except OSError:
        return ''


def claude_cmd(extra):
    return f"claude --plugin-dir {shlex.quote(MOD)} {extra}"


def launcher():
    demo = f' CLAUDE_DASH_DEMO=1 CLAUDE_DASH_HOME={shlex.quote(DIR)}' if DEMO else ''
    return f'CLAUDE_DASH_TMUX={shlex.quote(TMUX)}{demo} {shlex.quote(sys.executable)} {shlex.quote(SELF)}'


def main_cmd(claude):
    """What the middle pane runs: Claude, then (whenever it exits) the idle screen, so the pane stays."""
    return f'{claude}; exec env {launcher()} idle'


STASH = f'{TMUX}-stash'  # hidden tmux session holding the Claude panes not shown in the middle


def show_session(sid, claude, ids=()):
    """Bring the session's own Claude pane into the middle, creating it with `claude` the first time.
    The pane it replaces moves to the hidden stash and keeps running: switching never stops a session."""
    main = main_pane()
    if not main:
        return
    open(CURRENT, 'w').write(sid)
    panes = dict(line.split(' ', 1) for line in tmux('list-panes', '-a', '-F', '#{pane_id} #{@dash_sid}').splitlines()
                 if ' ' in line and line.split(' ', 1)[1])
    target = next((pane for pane, tag in panes.items() if tag == sid or tag in ids), None)
    if target == main:
        tmux('select-pane', '-t', main)
        return
    if not target:
        if subprocess.run(['tmux', 'has-session', '-t', f'={STASH}'], capture_output=True, stdin=subprocess.DEVNULL).returncode:
            size = tmux('display', '-p', '-t', main, '#{pane_width} #{pane_height}').split() or ['120', '40']
            tmux('new-session', '-d', '-s', STASH, '-x', size[0], '-y', size[1], 'sleep 2147483647')
        target = tmux('new-window', '-d', '-t', f'={STASH}:', '-P', '-F', '#{pane_id}', '-c', os.getcwd(), main_cmd(claude))
        tmux('set', '-p', '-t', target, '@dash_sid', sid)
        tmux('set', '-p', '-t', target, 'allow-set-title', 'off')
        tmux('set', '-p', '-t', target, 'remain-on-exit', 'on')
    idle = not pane_sid(main)
    tmux('swap-pane', '-s', target, '-t', main)
    tmux('set', '-t', T, '@dash_main', target)
    tmux('select-pane', '-t', target, '-T', 'Claude')
    tmux('select-pane', '-t', target)
    if idle:
        tmux('kill-pane', '-t', main)  # an empty "no session" screen is not worth keeping


def main_pane():
    """The middle pane, recreated with the whole layout if it is gone."""
    main = tmux('show', '-t', T, '-v', '@dash_main')
    if main and tmux('display', '-p', '-t', main, '#{pane_id}') == main:
        return main
    tmux('run-shell', '-b', f'{launcher()} relayout')  # rebuilt by tmux itself: it replaces this pane too
    return ''



def focus_main():
    main = main_pane()
    if main:
        tmux('select-pane', '-t', main)


# ---------- data ----------

def session_label(s):
    if s.get('state') == 'done':
        return 'completed'
    if s.get('status') == 'busy' or s.get('state') == 'working':
        return 'running'
    if s.get('status') == 'waiting' or s.get('state') == 'blocked':
        return 'waiting'
    return 'idle'


_transcripts = {}  # sessionId -> (path, mtime, last text)


def last_words(sid):
    """Last assistant line of a session's transcript, and when it was written."""
    path = _transcripts.get(sid, (None,))[0]
    if not path or not os.path.exists(path):
        path = next(iter(glob.glob(f'{HOME}/.claude/projects/*/{sid}.jsonl')), None)
    if not path:
        return '', None
    mtime = os.path.getmtime(path)
    cached = _transcripts.get(sid)
    if cached and cached[0] == path and cached[1] == mtime:
        return cached[2], mtime
    text = ''
    with open(path, 'rb') as f:
        f.seek(max(0, os.path.getsize(path) - 65536))
        lines = f.read().decode('utf-8', 'ignore').splitlines()
    for line in reversed(lines):
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get('type') != 'assistant':
            continue
        content = rec.get('message', {}).get('content', [])
        parts = [c.get('text', '') for c in content if isinstance(c, dict) and c.get('type') == 'text']
        text = ' '.join(' '.join(parts).split())
        if text:
            break
    _transcripts[sid] = (path, mtime, text)
    return text, mtime


def transcript(sid):
    hit = _transcripts.get(sid)
    if hit and os.path.exists(hit[0]):
        return hit[0]
    return next(iter(glob.glob(f'{HOME}/.claude/projects/*/{sid}.jsonl')), None)


def records(path, tail=None):
    with open(path, 'rb') as f:
        if tail:
            f.seek(max(0, os.path.getsize(path) - tail))
        data = f.read().decode('utf-8', 'ignore').splitlines()
    out = []
    for line in data:
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


_usage = {}  # path -> (mtime, result)


def transcript_usage(sid):
    """Model and token totals of a session, summed from its transcript."""
    path = transcript(sid)
    if not path:
        return {}
    mtime = os.path.getmtime(path)
    if path in _usage and _usage[path][0] == mtime:
        return _usage[path][1]
    seen, model, read, total, last = set(), None, 0, 0, 0
    for r in records(path):
        if r.get('subtype') == 'compact_boundary':  # compacting shrinks the window to the summary's size
            last = (r.get('compactMetadata') or {}).get('postTokens') or 0
            continue
        m = r.get('message') or {}
        u = m.get('usage')
        if r.get('type') != 'assistant' or not u or m.get('id') in seen:
            continue
        seen.add(m.get('id'))
        model = m.get('model') or model
        read += u.get('cache_read_input_tokens') or 0
        sent = (u.get('input_tokens') or 0) + (u.get('cache_read_input_tokens') or 0) + (u.get('cache_creation_input_tokens') or 0)
        total += sent
        last = sent + (u.get('output_tokens') or 0)  # what the next request re-sends: the window's fill
    result = {'model': model, 'tokens': {'read': read, 'total': total}, 'context_tokens': last}
    _usage[path] = (mtime, result)
    return result


def latest_limits():
    """Rate limits are account-wide: take them from whichever session reported last."""
    files = sorted(glob.glob(os.path.join(STATE, '*.json')), key=os.path.getmtime, reverse=True)
    for f in files[:5]:
        try:
            limits = json.load(open(f)).get('limits')
        except (OSError, ValueError):
            continue
        if limits:
            return limits
    return []


def text_of(content):
    if isinstance(content, str):
        return content
    return ' '.join(c.get('text', '') for c in content or [] if isinstance(c, dict) and c.get('type') == 'text')


def load_subagents(sid):
    """Subagents of a session, from the transcripts Claude keeps beside it."""
    path = transcript(sid)
    if not path:
        return []
    out = []
    for meta_path in glob.glob(path[:-len('.jsonl')] + '/subagents/agent-*.meta.json'):
        log = meta_path.replace('.meta.json', '.jsonl')
        try:
            meta = json.load(open(meta_path))
            rs = records(log)
        except (OSError, ValueError):
            continue
        prompt = next((text_of(r['message'].get('content')) for r in rs if r.get('type') == 'user' and r.get('message')), '')
        result = ''
        for r in reversed(rs):
            content = (r.get('message') or {}).get('content') if r.get('type') == 'assistant' else None
            if not isinstance(content, list):
                continue
            handback = next((c['input'].get('message', '') for c in content if c.get('type') == 'tool_use' and c.get('name') == 'SubagentHandback'), '')
            result = handback or text_of(content)
            if result:
                break
        mtime = os.path.getmtime(log) if os.path.exists(log) else 0
        status = 'completed' if result else 'running' if time.time() - mtime < 90 else 'stopped'
        out.append({'type': meta.get('agentType', 'agent'), 'description': meta.get('description', ''),
                    'prompt': prompt, 'result': result, 'status': status, 'at': mtime})
    return sorted(out, key=lambda a: a['at'])


_titles = {}  # transcript path -> (mtime, titles found in it)


def session_title(sid, name):
    """The name a person would recognise: a title set by hand, the agent name, or Claude's own title
    for sessions that only carry a generic name like myproject-bc."""
    path = transcript(sid)
    generic = not name or name == sid[:8] or re.fullmatch(r'[\w.]+-[0-9a-f]{2}', name)
    if not path:
        return name or sid[:8]
    mtime = os.path.getmtime(path)
    if path not in _titles or _titles[path][0] != mtime:
        found = {}
        for rec in records(path, tail=300_000) or records(path):
            for key, field in (('custom-title', 'customTitle'), ('agent-name', 'agentName'), ('ai-title', 'aiTitle')):
                if rec.get('type') == key and rec.get(field):
                    found[key] = rec[field]
        _titles[path] = (mtime, found)
    found = _titles[path][1]
    return found.get('custom-title') or found.get('agent-name') or (found.get('ai-title') if generic else None) or name or sid[:8]


def parked_links():
    """{original session id: id of the background copy it handed its conversation to}."""
    regs = registry()
    by_job = {d.get('jobId'): d['sessionId'] for d in regs if d.get('jobId')}
    return {d['sessionId']: by_job[d['parkedJobId']] for d in regs
            if d.get('parkedJobId') in by_job and by_job[d['parkedJobId']] != d['sessionId']}


def canonical(sid, links=None):
    links = parked_links() if links is None else links
    return links.get(sid, sid)


def load_sessions():
    try:
        out = subprocess.run(['claude', 'agents', '--json', '--all'], capture_output=True, text=True, timeout=10,
                             stdin=subprocess.DEVNULL).stdout  # never hand our terminal to a child: it would reset its modes
        raw = json.loads(out)
    except (subprocess.SubprocessError, ValueError, OSError):
        return None
    if not isinstance(raw, list):
        return None
    raw = [s for s in raw if isinstance(s, dict) and s.get('sessionId')]
    rows = []
    for s in raw:
        text, mtime = last_words(s['sessionId'])
        rows.append(dict(s, label=session_label(s), name=session_title(s['sessionId'], s.get('name')),
                         text=text, at=mtime or (s.get('startedAt', 0) / 1000)))
    live = {}
    for d in registry():
        if not alive(d.get('pid')):
            continue
        label = {'busy': 'running', 'waiting': 'waiting'}.get(d.get('status'), 'idle')
        mine = str(d.get('tmux') or '').startswith((f'{TMUX}:', f'{STASH}:'))
        best, elsewhere = live.get(d['sessionId'], ('idle', False))
        live[d['sessionId']] = (min(best, label, key=ORDER.index),
                                elsewhere or (d.get('kind') == 'interactive' and bool(d.get('tmux')) and not mine))
    for r in rows:
        best, elsewhere = live.get(r['sessionId'], (r['label'], False))
        if r['label'] != 'completed' and ORDER.index(best) < ORDER.index(r['label']):
            r['label'] = best
        r['elsewhere'] = elsewhere
    # sessions whose Claude is no longer running (closed, or the Mac restarted) are still on disk: list the
    # recent ones too, so they can be resumed. ponytail: last 7 days, newest 40 transcripts
    known = {r['sessionId'] for r in rows}
    base = lambda n: re.sub(r' \(\d+\)$', '', n or '')
    names = {base(session_title(r['sessionId'], r.get('name'))) for r in rows}  # a copy of a live one is not listed again
    recent = sorted(glob.glob(f'{HOME}/.claude/projects/*/*.jsonl'), key=os.path.getmtime, reverse=True)
    for path in recent[:40]:
        sid = os.path.basename(path)[:-len('.jsonl')]
        if sid in known or time.time() - os.path.getmtime(path) > 7 * 86400:
            continue
        name = session_title(sid, None)
        if name == sid[:8] or base(name) in names:
            continue  # no title (a scratch or headless run), or the same conversation is already listed
        names.add(base(name))
        text, mtime = last_words(sid)
        rows.append({'sessionId': sid, 'id': sid[:8], 'name': name, 'label': 'ended', 'text': text,
                     'at': mtime or os.path.getmtime(path), 'kind': 'ended'})
    links = parked_links()
    merged = {}
    for r in rows:
        key = canonical(r['sessionId'], links)
        r['ids'] = [r['sessionId']]
        if key in merged:  # the same conversation: one row, the busiest status, the newest activity
            m = merged[key]
            keep = r if r['sessionId'] == key else m  # the live copy represents it, so opening never forks it
            newest = max(m, r, key=lambda x: x['at'])
            keep['ids'] = m['ids'] + [r['sessionId']]
            keep['label'] = min(m['label'], r['label'], key=ORDER.index)
            keep['at'], keep['text'] = newest['at'], newest['text'] or m['text'] or r['text']
            merged[key] = keep
            continue
        merged[key] = r
    rows = list(merged.values())
    for r in rows:
        if len(r['ids']) > 1:
            r['name'] = re.sub(r' \(\d+\)$', '', r['name'])
    rows.sort(key=lambda r: (ORDER.index(r['label']), -r['at']))
    seen = set()  # a session can be listed twice (the session and the window viewing it): keep the busiest
    return [r for r in rows if not (r['sessionId'] in seen or seen.add(r['sessionId']))]


def load_state(sid=None):
    try:
        return json.load(open(os.path.join(STATE, (sid or current_id()) + '.json')))
    except (OSError, ValueError):
        return {}


def load_override(sid):
    try:
        return json.load(open(os.path.join(OVERRIDE, sid + '.json')))
    except (OSError, ValueError):
        return {}


def save_override(sid, pick):
    path = os.path.join(OVERRIDE, sid + '.json')
    pick = {k: v for k, v in pick.items() if v}
    if pick:
        json.dump(pick, open(path, 'w'))
    elif os.path.exists(path):
        os.remove(path)


def frontmatter(path):
    try:
        text = open(path, encoding='utf-8').read()
    except OSError:
        return {}
    m = re.match(r'^---\n(.*?)\n---', text, re.S)
    meta = {}
    for line in (m.group(1).splitlines() if m else []):
        k, _, v = line.partition(':')
        if v and not k.startswith(' '):
            meta[k.strip()] = v.strip().strip('"\'')
    return meta


def load_skills():
    found = {}
    paths = [p for p in glob.glob(f'{HOME}/.claude/skills/**/SKILL.md', recursive=True) if '/.trash/' not in p]
    paths += glob.glob(f'{os.getcwd()}/.claude/skills/*/SKILL.md')
    for p in paths:
        meta = frontmatter(p)
        name = meta.get('name') or os.path.basename(os.path.dirname(p))
        if '/skills/synced/' in p:
            name = f'anthropic-skills:{name}'
        found.setdefault(name, (f'/{name}', meta.get('description', '')))
    # installed plugins: cache/<marketplace>/<plugin>/<version>/skills/<skill>/SKILL.md
    for p in glob.glob(f'{HOME}/.claude/plugins/cache/*/*/*/skills/*/SKILL.md'):
        plugin = p.split('/plugins/cache/')[1].split('/')[1]
        meta = frontmatter(p)
        name = meta.get('name') or os.path.basename(os.path.dirname(p))
        found.setdefault(f'{plugin}:{name}', (f'/{plugin}:{name}', meta.get('description', '')))
    return sorted(((k, *v) for k, v in found.items()), key=lambda x: x[0].lower())


def load_agent_types():
    rows = [(n, f'Agent(subagent_type="{n}")', d) for n, d in BUILTIN_AGENTS]
    for p in glob.glob(f'{HOME}/.claude/agents/*.md') + glob.glob(f'{os.getcwd()}/.claude/agents/*.md'):
        meta = frontmatter(p)
        n = meta.get('name') or os.path.splitext(os.path.basename(p))[0]
        rows.append((n, f'Ask: "use the {n} agent to …"', meta.get('description', '')))
    return rows


MCP_FIX = {
    'connected': 'Ready to use.',
    'auth': 'Needs sign-in again. Press Enter: Claude opens this server in /mcp and starts its sign-in page.',
    'failed': 'Not reachable. Check it with: claude mcp get "{name}"',
}


def mcp_list_cmd():
    return ['claude', 'mcp', 'list']


def parse_mcp(out):
    """`claude mcp list` lines: '<name>: <url or command> - <status>'."""
    rows = []
    for line in out.splitlines():
        head, sep, status = line.rpartition(' - ')
        name, colon, target = head.partition(': ')
        if not sep or not colon:
            continue
        low = status.lower()
        kind = 'connected' if 'connected' in low else 'auth' if 'auth' in low else 'failed'
        rows.append((name.strip(), target.strip(), kind, status.strip(' ✔✓!✗')))
    order = {'auth': 0, 'failed': 1, 'connected': 2}
    return sorted(rows, key=lambda r: (r[0].startswith('claude.ai '), order[r[2]], r[0].lower()))


def screen(pane):
    return tmux('capture-pane', '-p', '-t', pane)


def wait_for(pane, text, seconds=6):
    end = time.time() + seconds
    while time.time() < end:
        if text in screen(pane):
            return True
        time.sleep(0.25)
    return False


def mcp_sign_in(pane, name):
    """Drive Claude's /mcp screen in `pane` to the server called `name` and choose Authenticate.
    Reads the screen to count rows, so it follows whatever order /mcp lists servers in."""
    # ponytail: screen-scrapes /mcp; breaks if its layout changes, then it falls back to leaving /mcp open
    keys = lambda *k: tmux('send-keys', '-t', pane, *k)
    keys('/mcp')
    time.sleep(0.3)
    keys('Enter')
    if not wait_for(pane, 'Manage MCP servers'):
        return 'could not open /mcp in Claude'
    for _ in range(2):
        lines = [l for l in screen(pane).splitlines() if l.strip()]
        items = [l for l in lines if re.match(r'^\s*(❯\s+)?[✔⚠✗×!↓→]', l)]
        cur = next((i for i, l in enumerate(items) if '❯' in l), 0)
        target = next((i for i, l in enumerate(items) if re.search(rf'[✔⚠✗×!]\s+{re.escape(name)}(\s{{2,}}|$)', l)), None)
        if target is None:
            more = next((i for i, l in enumerate(items) if 'Show unused' in l), None)
            if more is None:
                break
            keys(*(['Down'] * (more - cur) if more >= cur else ['Up'] * (cur - more)))
            keys('Enter')
            time.sleep(0.6)
            continue
        keys(*(['Down'] * (target - cur) if target >= cur else ['Up'] * (cur - target)))
        time.sleep(0.2)
        keys('Enter')
        if not wait_for(pane, 'Authenticate'):
            return f'opened /mcp; pick {name} there'
        tmux('select-pane', '-t', pane)
        keys('Enter')  # "1. Authenticate" is the first option
        return f'sign-in started for {name}: finish it in the browser'
    tmux('select-pane', '-t', pane)
    return f'opened /mcp; pick {name} there'


def sh(*cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=5, stdin=subprocess.DEVNULL).stdout
    except (subprocess.SubprocessError, OSError):
        return ''


def net_bytes():
    rx = tx = 0
    if not MAC:
        try:
            for line in open('/proc/net/dev').read().splitlines()[2:]:
                name, _, data = line.partition(':')
                f = data.split()
                if name.strip() != 'lo' and len(f) >= 9:
                    rx, tx = rx + int(f[0]), tx + int(f[8])
        except (OSError, ValueError):
            pass
        return rx, tx
    for line in sh('netstat', '-ibn').splitlines()[1:]:
        f = line.split()
        if f and f[0].startswith('en') and '<Link' in line and len(f) >= 7:
            try:
                rx, tx = rx + int(f[-5]), tx + int(f[-2])
            except ValueError:
                pass
    return rx, tx


_cpu = {}  # Linux: the last /proc/stat reading, to measure CPU use between two frames


def system_stats():
    s = {}
    if MAC:
        try:
            s['cpu'] = min(100.0, sum(float(x) for x in sh('ps', '-A', '-o', '%cpu=').split()) / (os.cpu_count() or 1))
        except ValueError:
            s['cpu'] = 0.0
        try:
            total = int(sh('sysctl', '-n', 'hw.memsize'))
            vm = sh('vm_stat')
            page = int(re.search(r'page size of (\d+)', vm).group(1))
            pages = lambda k: int(re.search(rf'{k}:\s+(\d+)', vm).group(1))
            used = (pages('Pages active') + pages('Pages wired down') + pages('Pages occupied by compressor')) * page
            s['mem'] = (used, total)
        except (ValueError, AttributeError):
            pass
        m = re.search(r'total = ([\d.]+)M\s+used = ([\d.]+)M', sh('sysctl', '-n', 'vm.swapusage'))
        if m and float(m.group(1)):
            s['swap'] = (float(m.group(2)) * 2**20, float(m.group(1)) * 2**20)
    else:
        try:
            ticks = [int(x) for x in open('/proc/stat').readline().split()[1:]]
            idle, total = ticks[3] + (ticks[4] if len(ticks) > 4 else 0), sum(ticks)
            if _cpu:
                dt = total - _cpu['total']
                s['cpu'] = 100.0 * (1 - (idle - _cpu['idle']) / dt) if dt else 0.0
            _cpu.update(idle=idle, total=total)
            info = {k: int(v.split()[0]) * 1024 for k, v in
                    (line.split(':', 1) for line in open('/proc/meminfo') if ':' in line)}
            s['mem'] = (info['MemTotal'] - info.get('MemAvailable', info.get('MemFree', 0)), info['MemTotal'])
            if info.get('SwapTotal'):
                s['swap'] = (info['SwapTotal'] - info.get('SwapFree', 0), info['SwapTotal'])
        except (OSError, ValueError, KeyError, IndexError):
            pass
    vol = '/System/Volumes/Data' if os.path.exists('/System/Volumes/Data') else '/'
    du = shutil.disk_usage(vol)
    s['disk'] = (du.used, du.total, du.free)
    s['load'] = os.getloadavg()
    return s


# ---------- drawing ----------

def put(win, y, x, text, attr=0):
    h, w = win.getmaxyx()
    if 0 <= y < h and 0 <= x < w - 1:
        win.addnstr(y, x, text, w - x - 1, attr)
    return x + len(text)


def human(n):
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if n < 1024 or unit == 'TB':
            return f'{n:.1f}{unit}' if unit != 'B' else f'{int(n)}B'
        n /= 1024


def tokens_fmt(n):
    return f'{n / 1e6:.1f}M'.replace('.0M', 'M') if n >= 1e6 else f'{n / 1e3:.0f}K' if n >= 1e3 else str(int(n))


def ago(t):
    if not t:
        return ''
    d = max(0, time.time() - t)
    for size, unit in ((86400, 'd'), (3600, 'h'), (60, 'm')):
        if d >= size:
            return f'{int(d // size)}{unit} ago'
    return 'now'


def until(iso):
    try:
        d = datetime.fromisoformat(iso.replace('Z', '+00:00')).timestamp() - time.time()
    except (ValueError, AttributeError):
        return ''
    if d <= 0:
        return ''
    h, m = int(d // 3600), int(d % 3600 // 60)
    return f'↻ {h // 24}d{h % 24}h' if h >= 24 else f'↻ {h}h{m:02d}m'


def bar(win, y, x, width, pct):
    pct = max(0.0, min(100.0, pct))
    tone = C['green'] if pct < 60 else C['yellow'] if pct < 85 else C['red']
    full = round(width * pct / 100)
    put(win, y, x, '━' * full, tone)
    put(win, y, x + full, '─' * (width - full), C['dim'])
    return x + width


def set_locale():
    """A UTF-8 locale, so curses measures ●, ✓, ⟲ as one cell each, like the terminal does."""
    for name in ('', 'C.UTF-8', 'en_US.UTF-8', 'C.utf8'):
        try:
            locale.setlocale(locale.LC_ALL, name)
            if 'utf' in (locale.getlocale()[1] or '').lower().replace('-', ''):
                return
        except locale.Error:
            continue


def is_active_pane():
    pane = os.environ.get('TMUX_PANE')
    return bool(pane) and tmux('display', '-p', '-t', pane, '#{pane_active}') == '1'


class Inset:
    """The inside of a card: the same window, offset by its border, so nothing overlaps."""

    def __init__(self, win):
        self.win = win

    def getmaxyx(self):
        h, w = self.win.getmaxyx()
        return h - 2, w - 3

    def addnstr(self, y, x, text, n, attr=0):
        self.win.addnstr(y + 1, x + 1, text, n, attr)


def frame(scr, title, active, note=''):
    """A rounded card around the pane, its title in the top edge."""
    h, w = scr.getmaxyx()
    w -= 1  # the terminal's last column stays empty: writing there makes the cursor wrap and the rows drift
    tone = C['cyan'] if active else C['dim']

    def cell(y, x, ch):
        try:
            scr.addstr(y, x, ch, tone)
        except curses.error:
            pass  # the bottom-right cell raises after writing; the character is still drawn

    for x in range(1, w - 1):
        cell(0, x, '─')
        cell(h - 1, x, '─')
    for y in range(1, h - 1):
        cell(y, 0, '│')
        cell(y, w - 1, '│')
    cell(0, 0, '╭')
    cell(0, w - 1, '╮')
    cell(h - 1, 0, '╰')
    cell(h - 1, w - 1, '╯')
    put(scr, 0, 2, f' {title} ', C['chip'] if active else C['cyan'] | curses.A_BOLD)
    if note:
        put(scr, h - 1, 2, f' {note} '[:w - 4], C['red'])


def run(draw, on_key=None, every=1.0, on_click=None, title=''):
    def handle(k):
        if k == curses.KEY_MOUSE:
            try:
                _, mx, my, _, b = curses.getmouse()
            except curses.error:
                return
            if b & curses.BUTTON4_PRESSED and on_key:
                on_key(curses.KEY_UP)
            elif b & getattr(curses, 'BUTTON5_PRESSED', 0x200000) and on_key:
                on_key(curses.KEY_DOWN)
            elif b & (curses.BUTTON1_PRESSED | curses.BUTTON1_CLICKED) and on_click:
                on_click(my - 1, mx - 1)  # into the card's inner coordinates
        elif k != -1 and on_key:
            on_key(k)

    def main(scr):
        curses.curs_set(0)
        palette()
        curses.mousemask(curses.ALL_MOUSE_EVENTS | curses.REPORT_MOUSE_POSITION)
        curses.mouseinterval(0)
        scr.timeout(int(every * 1000))
        scr.keypad(True)
        error, error_at, active, active_at, fails = '', 0.0, False, 0.0, 0
        while True:
            h, w = scr.getmaxyx()
            if time.time() - active_at > 0.5:
                active, active_at = is_active_pane(), time.time()
            scr.erase()
            ok = True
            try:
                if h > 2 and w > 4:
                    draw(Inset(scr))
            except curses.error:
                pass
            except Exception as err:
                error, error_at, ok = f'{type(err).__name__}: {err}', time.time(), False
            if ok:  # a frame that failed once is skipped, so the pane does not blink
                fails = 0
                frame(scr, title, active, error if time.time() - error_at < 15 else '')
                scr.refresh()
            else:
                fails += 1
                if fails >= 3:  # failing every time: show the frame and the error rather than a blank space
                    scr.erase()
                    frame(scr, title, active, error)
                    scr.refresh()
            try:
                handle(scr.getch())
            except Exception as err:
                error, error_at = f'{type(err).__name__}: {err}', time.time()

    os.environ.setdefault('ESCDELAY', '25')
    set_locale()
    curses.wrapper(main)


# ---------- panes ----------

CLOSED = os.path.join(DIR, 'closed.json')
PINNED = os.path.join(DIR, 'pinned.json')


def load_pinned():
    try:
        return json.load(open(PINNED))
    except (OSError, ValueError):
        return {}


def toggle_pin(sid, ids=()):
    pinned = load_pinned()
    ids = set(ids) | {sid}
    if ids & pinned.keys():
        for i in ids:
            pinned.pop(i, None)
    else:
        pinned[sid] = time.time()
    json.dump(pinned, open(PINNED, 'w'))


def load_closed():
    try:
        return json.load(open(CLOSED))
    except (OSError, ValueError):
        return {}


def save_closed(closed):
    json.dump(closed, open(CLOSED, 'w'))


def split_closed(rows):
    """Open rows and closed rows. A closed session that has new activity since it was closed reopens itself."""
    closed = load_closed()
    for r in rows:  # a close made on any id of the conversation counts for the merged row
        hit = next((i for i in r.get('ids', []) if i in closed), None)
        if hit and hit != r['sessionId']:
            closed[r['sessionId']] = closed.pop(hit)
    woke = [r['sessionId'] for r in rows if r['sessionId'] in closed
            and (r['label'] == 'running' or r['at'] > closed[r['sessionId']] + 5)]
    if woke:
        for sid in woke:
            closed.pop(sid)
        save_closed(closed)
    return [r for r in rows if r['sessionId'] not in closed], [r for r in rows if r['sessionId'] in closed]


def pane_sessions():
    st = {'rows': [], 'sel': 0, 'at': 0.0, 'top': 0, 'view': [], 'expanded': False}

    def refresh():
        rows = load_sessions()
        if rows is not None:
            st['rows'] = rows
        st['at'] = time.time()

    def build_view():
        live, closed = split_closed(st['rows'])
        pinned = load_pinned()
        for r in st['rows']:
            r['pinned'] = any(i in pinned for i in r.get('ids', [r['sessionId']]))
        live += [r for r in closed if r['pinned']]
        closed = [r for r in closed if not r['pinned']]
        tops = sorted((r for r in live if r['pinned']),
                      key=lambda r: min(pinned[i] for i in r.get('ids', [r['sessionId']]) if i in pinned))
        rest = [r for r in live if not r['pinned']]
        view = [('row', r) for r in tops] + ([('sep', len(tops))] if tops and rest else []) + [('row', r) for r in rest]
        if closed:
            view.append(('head', len(closed)))
            if st['expanded']:
                view += [('closed', r) for r in closed]
        st['view'] = view
        return live

    def draw(scr):
        if time.time() - st['at'] > 5:
            refresh()
        h, w = scr.getmaxyx()
        live = build_view()
        view = st['view']
        x = 1
        for label in ORDER:
            x = put(scr, 0, x, f"{ICON[label]} {sum(r['label'] == label for r in live)}  ", C[TONE[label]] | curses.A_BOLD)
        hint = '⏎ open · p pin · x close · n new'
        if w - x > len(hint) + 2:
            put(scr, 0, w - len(hint) - 2, hint, C['dim'])
        me = current_id()
        st['sel'] = min(st['sel'], max(0, len(view) - 1))
        room = h - 2
        if st['sel'] < st['top']:
            st['top'] = st['sel']
        elif st['sel'] >= st['top'] + room:
            st['top'] = st['sel'] - room + 1
        narrow = w < 56
        name_w = max(8, w - 16) if narrow else max(12, min(28, w // 3))
        for i, (kind, r) in enumerate(view[st['top']:st['top'] + room]):
            y, idx = 2 + i, st['top'] + i
            on = idx == st['sel']
            if on:
                put(scr, y, 0, ' ' * (w - 1), C['sel'])
            if kind == 'sep':
                put(scr, y, 1, '─' * (w - 3), paint('dim', on))
                put(scr, y, 2, ' ★ pinned above ', paint('yellow', on))
                continue
            if kind == 'head':
                arrow = '▾' if st['expanded'] else '▸'
                put(scr, y, 1, f'{arrow} CLOSED ({r})', paint('grey', on) | curses.A_BOLD)
                put(scr, y, 16, '─' * max(0, w - 33), paint('dim', on))
                put(scr, y, w - 15, 'click to ' + ('hide' if st['expanded'] else 'show'), paint('dim', on))
                continue
            shut = kind == 'closed'
            tone = 'dim' if shut else TONE[r['label']]
            put(scr, y, 1, ICON[r['label']], paint(tone, on))
            is_me = me in r.get('ids', [r['sessionId']])
            if r.get('elsewhere') and not shut:
                put(scr, y, 2 + name_w, '⧉', paint('magenta', on))  # also open in another terminal
            put(scr, y, 3, r['name'][:name_w - 1].ljust(name_w - 1),
                paint('dim' if shut else None, on) | (curses.A_BOLD if (is_me or r['label'] == 'running') and not shut else 0))
            x = 3 + name_w + 1
            if is_me and not narrow:
                x = put(scr, y, x, ' FOCUS ', C['badge']) + 1
            when = '' if narrow else f" · {ago(r['at']):>7}"
            right = w - 6 - 8 - len(when)
            if right - x > 4 and r['text']:
                put(scr, y, x, ('↳ ' + r['text'])[:right - x - 1], paint('dim' if shut else 'grey', on))
            x = put(scr, y, right, ('closed' if shut else WORD[r['label']]).rjust(8), paint(tone, on))
            put(scr, y, x, when, paint('dim', on))
            put(scr, y, w - 2, '↺' if shut else '×', paint('green' if shut else 'red', on) | curses.A_BOLD)
            if not shut:
                put(scr, y, w - 4, '★' if r.get('pinned') else '☆', paint('yellow' if r.get('pinned') else 'dim', on) | curses.A_BOLD)

    def open_in_main(cmd, sid, ids=()):
        show_session(sid, cmd, ids)

    def open_session(r):
        if r.get('kind') == 'background' and r.get('status') is not None:
            # ponytail: attach opens the bg session as-is; our mod is not loaded in it
            open_in_main(f"claude attach {r['id']}", r['sessionId'], r.get('ids', ()))
        else:
            open_in_main(claude_cmd(f"--resume {r['sessionId']}"), r['sessionId'], r.get('ids', ()))

    def toggle_closed(r, close):
        closed = load_closed()
        if close:
            closed[r['sessionId']] = time.time()
        else:
            closed.pop(r['sessionId'], None)
        save_closed(closed)

    def activate(idx, x=None):
        if not 0 <= idx < len(st['view']):
            return
        kind, r = st['view'][idx]
        if kind == 'sep':
            return
        if kind == 'head':
            st['expanded'] = not st['expanded']
        elif x is not None and kind == 'row' and st.get('w', 999) - 5 <= x < st.get('w', 999) - 3:  # ☆ / ★
            toggle_pin(r['sessionId'], r.get('ids', ()))
        elif x is not None and x >= st.get('w', 999) - 3:  # the × / ↺ button at the row's end
            if kind == 'row' and r.get('pinned'):
                toggle_pin(r['sessionId'], r.get('ids', ()))
            toggle_closed(r, close=kind == 'row')
        else:
            if kind == 'closed':
                toggle_closed(r, close=False)
            open_session(r)

    def on_key(k):
        n = len(st['view'])
        if k in (curses.KEY_UP, ord('k')):
            st['sel'] = max(0, st['sel'] - 1)
        elif k in (curses.KEY_DOWN, ord('j')):
            st['sel'] = min(n - 1, st['sel'] + 1)
        elif k in (curses.KEY_RIGHT, 27):
            focus_main()
        elif k == ord('n'):
            ask_new_session()
        elif k in (10, 13, curses.KEY_ENTER):
            activate(st['sel'])
        elif k == ord('p') and 0 <= st['sel'] < n and st['view'][st['sel']][0] == 'row':
            toggle_pin(st['view'][st['sel']][1]['sessionId'], st['view'][st['sel']][1].get('ids', ()))
        elif k in (ord('x'), ord('o')) and 0 <= st['sel'] < n and st['view'][st['sel']][0] in ('row', 'closed'):
            kind, r = st['view'][st['sel']]
            if k == ord('x') and r.get('pinned'):
                toggle_pin(r['sessionId'], r.get('ids', ()))
            toggle_closed(r, close=k == ord('x'))
        elif k == ord('r'):
            refresh()

    def on_click(y, x):
        idx = st['top'] + y - 2
        if 0 <= idx < len(st['view']):
            on_button = x >= st.get('w', 999) - 5
            if idx == st['sel'] or on_button or st['view'][idx][0] == 'head':
                activate(idx, x)
            st['sel'] = idx

    def draw_and_size(scr):
        st['w'] = scr.getmaxyx()[1]
        draw(scr)

    run(draw_and_size, on_key, every=0.5, on_click=on_click, title='Sessions')


def pane_agents():
    st = {'sel': None, 'hits': [], 'top': 0}
    marks = {'running': ('●', 'orange'), 'completed': ('✓', 'green'), 'stopped': ('■', 'red')}

    def draw(scr):
        h, w = scr.getmaxyx()
        agents = load_subagents(current_id())
        live = sum(a['status'] == 'running' for a in agents)
        if st['sel'] is None or not 0 <= st['sel'] < len(agents):
            st['sel'] = max(0, len(agents) - 1)
        x = put(scr, 0, 1, f'● {live} running', C['orange'] | curses.A_BOLD)
        x = put(scr, 0, x + 2, f'✓ {len(agents) - live} done', C['green'])
        st['hits'] = []
        if agents:
            x = put(scr, 0, max(x + 2, w - 16), ' ◀ ', C['chip'])
            st['hits'].append((0, x - 3, x, -1))
            x = put(scr, 0, x, f' {st["sel"] + 1}/{len(agents)} ', C['grey'])
            put(scr, 0, x, ' ▶ ', C['chip'])
            st['hits'].append((0, x, x + 3, 1))
        if not agents:
            put(scr, 2, 1, 'No subagents yet in this session.', C['dim'])
            return
        list_rows = max(2, min(len(agents), (h - 2) // 3))
        if st['sel'] < st['top']:
            st['top'] = st['sel']
        elif st['sel'] >= st['top'] + list_rows:
            st['top'] = st['sel'] - list_rows + 1
        for i, a in enumerate(agents[st['top']:st['top'] + list_rows]):
            on, y = st['top'] + i == st['sel'], 1 + i
            if on:
                put(scr, y, 0, ' ' * (w - 1), C['sel'])
            icon, tone = marks[a['status']]
            x = put(scr, y, 1, icon + ' ', paint(tone, on))
            x = put(scr, y, x, a['type'], paint('cyan', on) | curses.A_BOLD)
            put(scr, y, x, '  ' + a['description'], paint(None if a['status'] == 'running' else 'grey', on))
        a = agents[st['sel']]
        y = 2 + list_rows
        put(scr, y - 1, 0, '─' * w, C['dim'])
        for label, tone, body in (('task', 'yellow', a['prompt']), ('result', 'green', a['result'] or '(still running)')):
            if y >= h:
                break
            put(scr, y, 1, label, C[tone] | curses.A_BOLD)
            y += 1
            budget = max(1, (h - y) // 2) if label == 'task' else h - y
            for line in textwrap.wrap(' '.join(body.split()), max(10, w - 3))[:budget]:
                put(scr, y, 2, line, C['grey'] if label == 'task' else 0)
                y += 1

    def step(n):
        st['sel'] = max(0, min(len(load_subagents(current_id())) - 1, (st['sel'] or 0) + n))

    def on_key(k):
        if k in (curses.KEY_UP, curses.KEY_LEFT, ord('k')):
            step(-1)
        elif k in (curses.KEY_DOWN, curses.KEY_RIGHT, ord('j')):
            step(1)
        elif k == 27:
            focus_main()

    def on_click(y, x):
        for hy, x0, x1, n in st['hits']:
            if y == hy and x0 <= x < x1:
                return step(n)
        if y >= 1:
            st['sel'] = st['top'] + y - 1 if st['top'] + y - 1 < len(load_subagents(current_id())) else st['sel']

    run(draw, on_key, every=1.5, on_click=on_click, title='Subagents')


REVIEWS = os.path.join(DIR, 'reviews')
SUMMARY_PROMPT = (
    'You summarize one Claude Code session for the person who ran it. You get their requests, the tools and '
    'files Claude used, and Claude\'s replies. Reply with JSON only, no prose: {"title": "ticket key and a few '
    'words", "points": [{"label": "Problem", "text": "..."}, {"label": "Issue", "text": "..."}, '
    '{"label": "Used", "text": "..."}, {"label": "Approach", "text": "..."}, {"label": "Changes", "text": "..."}, '
    '{"label": "Why", "text": "..."}, {"label": "Status", "text": "..."}]}. Exactly these 7 labels, in this '
    'order: Problem = what the person wanted; Issue = the root cause or blocker found; Used = tools, services, '
    'repos and files involved; Approach = how Claude went about it; Changes = the code or config changed, with '
    'file names; Why = why that approach and those changes; Status = done, blocked, or what is next. One short '
    'sentence each (under 25 words). Use only what the session shows; say "none" when something did not happen.')
POINT_TONE = {'Problem': 'red', 'Issue': 'orange', 'Used': 'blue', 'Approach': 'cyan',
              'Changes': 'magenta', 'Why': 'yellow', 'Status': 'green'}


def session_digest(sid, budget=80_000):
    """The session in brief for the summarizer: every request, the tools and files used, the replies' gist."""
    path = transcript(sid)
    if not path:
        return None
    asks, replies, calls, counts, files = [], [], [], {}, []
    for r in records(path):
        content = (r.get('message') or {}).get('content')
        if r.get('type') == 'user' and not r.get('isMeta'):
            said = text_of(content).strip()
            if said and not said.startswith(('<', '[Request interrupted')) and not (
                    isinstance(content, list) and any(isinstance(c, dict) and c.get('type') == 'tool_result' for c in content)):
                asks.append(' '.join(said.split())[:400])
        elif r.get('type') == 'assistant' and isinstance(content, list):
            for c in content:
                if c.get('type') == 'tool_use':
                    name, args = c.get('name', '?'), c.get('input') or {}
                    counts[name] = counts.get(name, 0) + 1
                    target = next((str(args[k]) for k in ('file_path', 'command', 'pattern', 'description', 'url') if k in args), '')
                    calls.append(f'{name}: {" ".join(target.split())[:140]}')
                    if name in ('Edit', 'Write', 'MultiEdit', 'NotebookEdit') and args.get('file_path') and args['file_path'] not in files:
                        files.append(args['file_path'])
                elif c.get('type') == 'text' and c.get('text', '').strip():
                    replies.append(' '.join(c['text'].split())[:500])
    if not asks and not replies:
        return None
    pick = lambda xs, head, tail: xs if len(xs) <= head + tail else xs[:head] + ['…'] + xs[-tail:]
    text = ('REQUESTS:\n' + '\n'.join(f'- {a}' for a in pick(asks, 15, 60)) +
            '\n\nTOOLS USED: ' + ', '.join(f'{k}×{v}' for k, v in sorted(counts.items(), key=lambda kv: -kv[1])) +
            '\n\nFILES EDITED:\n' + ('\n'.join(files[-60:]) or 'none') +
            '\n\nLATEST TOOL CALLS:\n' + '\n'.join(calls[-80:]) +
            '\n\nCLAUDE\'S REPLIES (start and end):\n' + '\n'.join(f'- {x}' for x in pick(replies, 8, 40)))
    return text[:budget]


def pane_approach():
    st = {'job': None, 'err': '', 'button': None, 'scroll': 0, 'max': 0}

    def path(sid):
        return os.path.join(REVIEWS, sid + '.summary.json')

    def saved(sid):
        try:
            return json.load(open(path(sid)))
        except (OSError, ValueError):
            return None

    def summarize():
        sid = current_id()
        if st['job'] or not sid:
            return
        digest = session_digest(sid)
        if not digest:
            st['err'] = 'Nothing to summarize yet: this session has no messages.'
            return
        # ponytail: one `claude -p` on Haiku per click; the digest is capped at 80k characters
        proc = subprocess.Popen(
            ['claude', '-p', '--model', 'haiku', '--effort', 'low', '--no-session-persistence', '--tools', '',
             '--strict-mcp-config', '--disable-slash-commands', '--system-prompt', SUMMARY_PROMPT,
             'Summarize this session.'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=DIR)
        try:
            proc.stdin.write(digest)
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            out = proc.stdout.read() if proc.stdout else ''
            proc.wait()
            st['err'] = 'Summary failed: ' + (' '.join(out.split())[:90] or 'claude exited early')
            return
        st['job'], st['err'] = {'proc': proc, 'sid': sid, 'at': time.time()}, ''

    def poll():
        job = st['job']
        if not job or job['proc'].poll() is None:
            return
        out = job['proc'].stdout.read()
        job['proc'].stdout.close()
        st['job'] = None
        m = re.search(r'\{.*\}', out, re.S)
        try:
            data = json.loads(m.group(0))
            assert isinstance(data.get('points'), list)
        except (AttributeError, ValueError, AssertionError):
            st['err'] = 'Summary failed: ' + ' '.join(out.split())[:90]
            return
        data['at'] = time.time()
        json.dump(data, open(path(job['sid']), 'w'))

    def button(scr, y, x, text):
        end = put(scr, y, x, text, C['chip'])
        st['button'] = (y, x, end)

    def draw(scr):
        poll()
        h, w = scr.getmaxyx()
        sid = current_id()
        data = saved(sid)
        st['button'] = None
        if st['job']:
            spin = '◐◓◑◒'[int(time.time() * 4) % 4]
            put(scr, 1, 1, f'{spin} Haiku is reading the whole session…', C['cyan'])
            put(scr, 2, 1, f'{int(time.time() - st["job"]["at"])}s', C['dim'])
            if not data:
                return
        if not data:
            lines = ['No summary for this session yet.', '',
                     'Haiku reads the whole session and sums it up', 'in 7 points: problem, issue, what was used,',
                     'approach, code changes, why, and status.']
            top = max(0, h // 2 - 4)
            for i, line in enumerate(lines):
                put(scr, top + i, max(1, (w - len(line)) // 2), line, curses.A_BOLD if i == 0 else C['grey'])
            label = ' ✦ Summarize this session '
            button(scr, top + len(lines) + 1, max(1, (w - len(label)) // 2), label)
            if st['err']:
                put(scr, top + len(lines) + 3, 1, st['err'][:w - 2], C['red'])
            return
        lines = [((data.get('title') or 'Session summary')[:w - 2], '', curses.A_BOLD)]
        for point in data.get('points', [])[:7]:
            label, text = str(point.get('label', '')), str(point.get('text', ''))
            for i, line in enumerate(textwrap.wrap(text, max(10, w - 12)) or ['']):
                lines.append((line, label if i == 0 else '', 0))
        top = 0 if not st['job'] else 4
        room = max(1, h - 1 - top)
        st['max'] = max(0, len(lines) - room)
        st['scroll'] = min(st['scroll'], st['max'])
        for i, (line, label, attr) in enumerate(lines[st['scroll']:st['scroll'] + room]):
            if label:
                put(scr, top + i, 1, f'{label:<9}', C[POINT_TONE.get(label, 'grey')] | curses.A_BOLD)
            put(scr, top + i, 1 if attr else 11, line, attr)
        more = '  ↑↓ more' if st['max'] else ''
        x = put(scr, h - 1, 1, f'summarized {ago(data.get("at"))}{more}', C['dim'])
        button(scr, h - 1, max(x + 2, w - 13), ' ↻ Refresh ')
        if st['err']:
            put(scr, h - 2, 1, st['err'][:w - 2], C['red'])

    def on_click(y, x):
        b = st['button']
        if b and y == b[0] and b[1] <= x < b[2]:
            summarize()

    def on_key(k):
        if k in (curses.KEY_UP, ord('k')):
            st['scroll'] = max(0, st['scroll'] - 1)
        elif k in (curses.KEY_DOWN, ord('j')):
            st['scroll'] = min(st['max'], st['scroll'] + 1)
        elif k in (10, 13, curses.KEY_ENTER, ord('s'), ord('r')):
            summarize()
        elif k == 27:
            focus_main()

    run(draw, on_key, every=0.5, on_click=on_click, title='Approach & Review')


def pane_skills():
    tabs = ['Skills', 'Agents', 'MCP']
    st = {'tab': 0, 'sel': [0, 0, 0], 'top': 0, 'tabs_x': [], 'lists': [load_skills(), load_agent_types(), []],
          'mcp': None, 'mcp_at': 0.0}
    mcp_tone = {'connected': 'green', 'auth': 'yellow', 'failed': 'red'}
    mcp_icon = {'connected': '●', 'auth': '!', 'failed': '✗'}

    def refresh_mcp():
        job = st['mcp']
        if job and job.poll() is not None:
            st['lists'][2] = parse_mcp(job.stdout.read())
            st['mcp'] = None
        # ponytail: `claude mcp list` health-checks every server (~5s), so it runs in the background every 2 min
        if not st['mcp'] and time.time() - st['mcp_at'] > 120:
            st['mcp_at'] = time.time()
            st['mcp'] = subprocess.Popen(mcp_list_cmd(), stdout=subprocess.PIPE, stdin=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL, text=True, cwd=os.getcwd())

    def draw(scr):
        refresh_mcp()
        h, w = scr.getmaxyx()
        x, st['tabs_x'] = 1, []
        for i, t in enumerate(tabs):
            label = t
            if t == 'MCP' and st['lists'][2]:
                label += f" !{sum(r[2] != 'connected' for r in st['lists'][2])}"
            end = put(scr, 0, x, f' {label} ', C['chip'] if i == st['tab'] else C['grey'])
            st['tabs_x'].append((x, end))
            x = end + 1
        put(scr, 0, x + 1, 'tab ↑↓', C['dim'])
        items = st['lists'][st['tab']]
        if st['tab'] == 2 and not items:
            put(scr, 2, 1, 'Checking MCP servers…' if st['mcp'] else 'No MCP servers found.', C['dim'])
            return
        if not items:
            put(scr, 2, 1, 'Nothing here yet.', C['dim'])
            return
        sel = st['sel'][st['tab']] = min(st['sel'][st['tab']], max(0, len(items) - 1))
        help_rows = max(4, h // 2)
        room = max(1, h - 2 - help_rows)
        st['top'] = max(0, sel - room + 1)
        heading = None
        y = 0
        for i, item in enumerate(items[st['top']:st['top'] + room]):
            if st['tab'] == 2:
                group = 'claude.ai connectors' if item[0].startswith('claude.ai ') else 'Your MCP servers'
                if group != heading:
                    heading = group
                    y += 1
                    put(scr, y, 1, group.upper(), C['cyan'] | curses.A_BOLD)
                    if y >= room:
                        break
            y += 1
            on = st['top'] + i == sel
            if y > room + 1:
                break
            if on:
                put(scr, y, 0, ' ' * (w - 1), C['sel'])
            if st['tab'] == 2:
                name, _, kind, status = item
                short = name[len('claude.ai '):] if name.startswith('claude.ai ') else name
                x = put(scr, y, 1, mcp_icon[kind] + ' ', paint(mcp_tone[kind], on) | curses.A_BOLD)
                x = put(scr, y, x, short, paint(None, on) | (curses.A_BOLD if on else 0))
                put(scr, y, max(x + 1, w - len(status) - 2), status, paint(mcp_tone[kind], on))
                continue
            plugin, colon, short = item[0].rpartition(':')
            x = put(scr, y, 1, plugin + colon, paint('dim', on)) if colon else 1
            put(scr, y, x, short, paint('magenta', on) | (curses.A_BOLD if on else 0))
        y = h - help_rows - 1
        put(scr, y, 0, '─' * w, C['dim'])
        put(scr, y, 2, ' live check (claude mcp list) ' if st['tab'] == 2 else '', C['dim'])
        if st['tab'] == 2:
            name, target, kind, status = items[sel]
            x = put(scr, y + 1, 1, name, curses.A_BOLD)
            x = put(scr, y + 1, x + 2, status, C[mcp_tone[kind]] | curses.A_BOLD)
            put(scr, y + 1, x + 2, target, C['dim'])
            desc = (st.get('note') + '  ' if st.get('note') else '') + MCP_FIX[kind].format(name=name)
            if kind == 'connected':
                desc += f' Its tools show up in Claude as mcp__{re.sub(r"[^A-Za-z0-9_-]", "_", name)}__*.'
        else:
            _, use, desc = items[sel]
            x = put(scr, y + 1, 1, 'use ', C['grey'])
            put(scr, y + 1, x, use, C['cyan'] | curses.A_BOLD)
        for i, line in enumerate(textwrap.wrap(desc, max(10, w - 3))[:help_rows - 2]):
            put(scr, y + 2 + i, 1, line, C['grey'])

    def on_key(k):
        t = st['tab']
        if k == 9:
            st['tab'] = (t + 1) % len(tabs)
        elif k in (curses.KEY_UP, ord('k')):
            st['sel'][t] = max(0, st['sel'][t] - 1)
        elif k in (curses.KEY_DOWN, ord('j')):
            st['sel'][t] += 1
        elif k == ord('r'):
            st['mcp_at'] = 0.0
        elif k in (10, 13, curses.KEY_ENTER) and t == 2 and st['lists'][2]:
            authenticate(st['lists'][2][st['sel'][2]])
        elif k in (curses.KEY_RIGHT, 27):
            focus_main()

    def authenticate(item):
        name, _, kind, _ = item
        if kind == 'connected':
            return
        sid = current_id()
        if any(d.get('sessionId') == sid and d.get('status') == 'busy' for d in registry()):
            st['note'] = 'Claude is busy: try when the turn ends'
            return
        st['note'] = f'opening sign-in for {name}…'
        st['note'] = mcp_sign_in(tmux('show', '-t', T, '-v', '@dash_main'), name)
        st['mcp_at'] = time.time() - 100  # re-check about 20s later

    def on_click(y, x):
        if y == 0:
            for i, (x0, x1) in enumerate(st['tabs_x']):
                if x0 <= x < x1:
                    st['tab'] = i
        elif st['tab'] == 2:
            items, heading, row = st['lists'][2], None, 0
            for i, item in enumerate(items[st['top']:], start=st['top']):
                group = item[0].startswith('claude.ai ')
                if group != heading:
                    heading, row = group, row + 1
                row += 1
                if row == y:
                    st['sel'][2] = i
                    return
        else:
            st['sel'][st['tab']] = st['top'] + y - 1

    run(draw, on_key, every=1.0, on_click=on_click, title='Skills | Agents | MCP')


DISK_ALERT = 90  # percent of the data volume used before the dashboard prompts for a cleanup
DISK_PROMPT_MAC = """You are a one-off disk cleanup run, with nobody watching. Free disk space on this Mac, safely.

1. Show usage first: df -h /System/Volumes/Data
2. Measure these regenerable caches with du -sh (skip any that do not exist): ~/Library/Caches,
   ~/Library/Developer/Xcode/DerivedData, ~/Library/Developer/CoreSimulator/Caches, ~/.npm/_cacache,
   ~/Library/Caches/Yarn, ~/Library/pnpm/store, ~/Library/Caches/pip, the Homebrew cache (brew --cache),
   and Docker (docker system df).
3. Clean only these, using each tool's own cleanup where it has one: brew cleanup -s; npm cache clean --force;
   yarn cache clean; pnpm store prune; pip cache purge; the contents of Xcode DerivedData;
   xcrun simctl delete unavailable; docker system prune -f (never volumes); and the contents of
   ~/Library/Caches/<app> only for apps that are not running.
4. Never touch: Documents, Desktop, Downloads, any project or git repository, ~/.claude,
   ~/Library/Application Support, Mail, Photos, Messages, keychains, Docker volumes. Do not empty the Trash.
   Ask me before deleting anything that is not in step 3.
5. Finish with a short table: what you cleaned, the space each freed, and df before and after."""
DISK_PROMPT_LINUX = """You are a one-off disk cleanup run, with nobody watching. Free disk space on this Linux machine, safely.

1. Show usage first: df -h /
2. Measure these regenerable caches with du -sh (skip any that do not exist): ~/.cache (per subfolder),
   ~/.npm/_cacache, ~/.cache/yarn, ~/.local/share/pnpm/store, ~/.cache/pip, and Docker (docker system df).
3. Clean only these, using each tool's own cleanup where it has one: npm cache clean --force; yarn cache clean;
   pnpm store prune; pip cache purge; docker system prune -f (never volumes); and the contents of
   ~/.cache/<app> only for apps that are not running. Do not use sudo.
4. Never touch: ~/Documents, ~/Desktop, ~/Downloads, any project or git repository, ~/.claude, ~/.config,
   ~/.ssh, ~/.gnupg, Docker volumes, or the Trash. Ask me before deleting anything that is not in step 3.
5. Finish with a short table: what you cleaned, the space each freed, and df before and after."""
DISK_PROMPT = DISK_PROMPT_MAC if MAC else DISK_PROMPT_LINUX


CLEAN_LOG = os.path.join(DIR, 'disk-clean.log')
CLEAN_JOB = os.path.join(DIR, 'disk-clean.json')


def clean_status():
    """('running', seconds) while a cleanup runs, ('done', finished_at, freed) after, or None."""
    try:
        job = json.load(open(CLEAN_JOB))
    except (OSError, ValueError):
        return None
    if alive(job.get('pid')):
        return 'running', time.time() - job['at']
    try:
        text = open(CLEAN_LOG).read()
    except OSError:
        text = ''
    freed = (re.findall(r'FREED:\s*(.+)', text) or ['see report'])[-1].strip()[:20]
    return 'done', os.path.getmtime(CLEAN_LOG) if os.path.exists(CLEAN_LOG) else job['at'], freed


def open_disk_cleaner():
    """Run the cleanup in the background, with no session of its own: the dashboard's sessions stay as they are."""
    if (clean_status() or ('',))[0] == 'running':
        return
    # ponytail: one headless `claude -p` in auto mode, bounded by DISK_PROMPT's allow/deny lists
    with open(CLEAN_LOG, 'w') as log:
        proc = subprocess.Popen(
            ['claude', '-p', '--permission-mode', 'auto', '--no-session-persistence',
             DISK_PROMPT + '\n6. End your reply with one line: FREED: <total space freed>'],
            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, cwd=HOME, start_new_session=True)
    json.dump({'pid': proc.pid, 'at': time.time()}, open(CLEAN_JOB, 'w'))


DISK_CHAT = os.path.join(DIR, 'disk-cleaner.json')


def toggle_disk_chat():
    """Swap the "disk cleaning" chat into the middle to ask it things; again to go back to where you were.
    The session you leave keeps running, as with any switch."""
    try:
        saved = json.load(open(DISK_CHAT))
    except (OSError, ValueError):
        saved = {}
    sid = saved.get('id') or str(uuid.uuid4())
    if current_id() == sid and saved.get('back'):
        json.dump({'id': sid}, open(DISK_CHAT, 'w'))
        show_session(saved['back'], claude_cmd(f"--resume {saved['back']}"))
        return
    json.dump({'id': sid, 'back': current_id()}, open(DISK_CHAT, 'w'))
    start = f'--resume {sid}' if transcript(sid) else f'--session-id {sid} -n "disk cleaning"'
    show_session(sid, claude_cmd(start))


def show_clean_report():
    if os.path.exists(CLEAN_LOG):
        tmux('display-popup', '-E', '-w', '80%', '-h', '80%', '-T', ' disk cleanup report · q closes ',
             f'less -R {shlex.quote(CLEAN_LOG)}')


def notify(text):
    tmux('display-message', '-d', '8000', text)
    cmd = (['osascript', '-e', 'on run argv', '-e', 'display notification (item 1 of argv) with title "Claude dash"',
            '-e', 'end run', text] if MAC else ['notify-send', 'Claude dash', text])
    try:
        subprocess.run(cmd, capture_output=True, stdin=subprocess.DEVNULL, timeout=5)
    except (OSError, subprocess.SubprocessError):
        pass  # no notifier installed: the tmux message still shows


def pane_computer():
    st = {'net': (net_bytes(), time.time()), 'rate': (0, 0), 'sys': {}, 'at': 0.0}

    def draw(scr):
        if time.time() - st['at'] > 2:
            (rx0, tx0), t0 = st['net']
            now, (rx, tx) = time.time(), net_bytes()
            st['rate'] = ((rx - rx0) / max(0.1, now - t0), (tx - tx0) / max(0.1, now - t0))
            st['net'] = ((rx, tx), now)
            st['sys'], st['at'] = system_stats(), now
        s = st['sys']
        _, w = scr.getmaxyx()
        bw = min(16, w - 30)
        gb = lambda n: f'{n / 2**30:.0f}' if n >= 10 * 2**30 else f'{n / 2**30:.1f}'

        def row(y, label, pct, note=''):
            put(scr, y, 1, label.ljust(5), C['grey'])
            x = bar(scr, y, 7, bw, pct) + 1 if bw >= 4 else 7  # narrow card: numbers only
            tone = C['green'] if pct < 60 else C['yellow'] if pct < 85 else C['red']
            x = put(scr, y, x, f'{pct:3.0f}%', curses.A_BOLD | (0 if bw >= 4 else tone))
            put(scr, y, x + 1, note, C['dim'])

        l1, l5, l15 = s.get('load', (0, 0, 0))
        row(0, 'cpu', s.get('cpu', 0), f'load {l1:.1f}')
        if 'mem' in s:
            u, t = s['mem']
            row(1, 'mem', 100 * u / t, f'{gb(u)}/{gb(t)}G')
        if 'swap' in s:
            u, t = s['swap']
            row(2, 'swap', 100 * u / t, f'{gb(u)}/{gb(t)}G')
        u, t, free = s.get('disk', (0, 1, 0))
        pct = 100 * u / t
        row(3, 'disk', pct, f'{gb(free)}G free'.replace('.0G', 'G'))
        full = pct >= DISK_ALERT
        if full and not st.get('alerted'):
            notify(f'Disk is {pct:.0f}% full. Click "Clean disk" in the dashboard.')
        st['alerted'] = full
        job = clean_status()
        if job and job[0] == 'running':
            label = f' cleaning… {int(job[1] // 60)}m{int(job[1] % 60):02d}s '
        elif job and time.time() - job[1] < 3600:
            label = f' freed {job[2]} · report '
        else:
            label = f' ! Disk {pct:.0f}% · Clean ' if full else ' Clean disk '
        if w - 1 - len(label) < 30:
            label = ' ! Clean ' if full else ' Clean '
        try:
            chatting = current_id() == json.load(open(DISK_CHAT)).get('id')
        except (OSError, ValueError):
            chatting = False
        ask = ' ← back ' if chatting else ' Ask '
        ax = w - 1 - len(ask)
        bx = ax - 1 - len(label)  # the buttons keep their place; the network figures fit in front of them
        end = put(scr, 4, bx, label, (C['sel:red'] if full else C['badge']) | curses.A_BOLD)
        st['button'] = (4, bx, end)
        st['ask'] = (4, ax, put(scr, 4, ax, ask, C['chip']))
        put(scr, 4, 1, 'net', C['grey'])
        net = f'↓{human(st["rate"][0])}/s ↑{human(st["rate"][1])}/s'
        if 7 + len(net) >= bx - 1:
            net = f'↓{human(st["rate"][0])}/s'
        put(scr, 4, 7, net[:max(0, bx - 8)], C['green'])

    def on_click(y, x):
        a = st.get('ask')
        if a and y == a[0] and a[1] <= x < a[2]:
            return toggle_disk_chat()
        b = st.get('button')
        if b and y == b[0] and b[1] <= x < b[2]:
            job = clean_status()
            if job and job[0] == 'done' and time.time() - job[1] < 3600:
                show_clean_report()
            else:
                open_disk_cleaner()

    def on_key(k):
        if k == ord('c'):
            open_disk_cleaner()

    run(draw, on_key, every=1.0, on_click=on_click, title='Computer')


def default_effort(model):
    """The effort a session runs at when nothing overrides it, from the user's settings."""
    try:
        cfg = json.load(open(f'{HOME}/.claude/settings.json'))
    except (OSError, ValueError):
        return None
    base = (model or '').split('[')[0]
    return (cfg.get('modelSettings', {}).get(base) or {}).get('effortLevel') or cfg.get('effortLevel')


def session_info(sid):
    """What a session runs on now: its name, model and effort, and whether each is our override."""
    pick, state, used = load_override(sid), load_state(sid), transcript_usage(sid)
    model = pick.get('model') or state.get('model') or used.get('model')
    effort = pick.get('effort') or state.get('effort') or default_effort(model)
    name = session_title(sid, next((d.get('name') for d in registry() if d.get('sessionId') == sid and d.get('name')), None))
    if sid in parked_links().values():
        name = re.sub(r' \(\d+\)$', '', name)  # the background copy of a conversation: same name as the original
    return {'name': name, 'model': model, 'effort': effort,
            'model_set': bool(pick.get('model')), 'effort_set': bool(pick.get('effort'))}


def context_fill(sid):
    """(percent, tokens, window, exact) of the session's context window."""
    ctx = load_state(sid).get('context') or {}
    if ctx.get('window') and ctx.get('tokens') is not None:
        return ctx.get('percent') or 100 * ctx['tokens'] / ctx['window'], ctx['tokens'], ctx['window'], True
    used = transcript_usage(sid).get('context_tokens')
    if not used:
        return None
    window = 1_000_000  # ponytail: assumed 1M (current Opus/Sonnet in Claude Code); the mod reports the real window
    return 100 * used / window, used, window, False


def chip_for(value, options):
    """The chip matching a model id or effort, e.g. claude-opus-5-5[1m] -> Opus."""
    v = str(value or '').lower()
    exact = next((name for name, opt in options if opt and opt.lower() == v), None)
    return exact or next((name for name, opt in options if opt and (opt.split('[')[0] in v or name.lower() in v.split('-'))), None)


def pane_stats():
    names = {'five_hour': '5h', 'seven_day': 'week'}

    def draw(scr):
        _, w = scr.getmaxyx()
        sid = current_id()
        state, used, info = load_state(sid), transcript_usage(sid), session_info(sid)
        bw = min(16, w - 30)

        def row(y, label, pct, note=''):
            put(scr, y, 1, label.ljust(5), C['grey'])
            x = bar(scr, y, 7, bw, pct) + 1 if bw >= 4 else 7  # narrow card: numbers only
            tone = C['green'] if pct < 60 else C['yellow'] if pct < 85 else C['red']
            x = put(scr, y, x, f'{pct:3.0f}%', curses.A_BOLD | (0 if bw >= 4 else tone))
            put(scr, y, x + 1, note, C['dim'])

        x = put(scr, 0, 1, (info['model'] or '—').replace('claude-', ''), C['cyan'] | curses.A_BOLD)
        if info['effort']:
            x = put(scr, 0, x + 1, f'· {info["effort"]}', C['magenta'])
        if state.get('costUsd') is not None:
            put(scr, 0, max(x + 2, w - 9), f"${state['costUsd']:.2f}", C['yellow'] | curses.A_BOLD)
        y = 1
        for l in latest_limits()[:2]:
            row(y, names.get(l['kind'], l['kind'])[:5], l['percentUsed'], until(l.get('resetsAt', '')))
            y += 1
        tok = used.get('tokens') or state.get('tokens') or {}
        put(scr, 3, 1, 'cache', C['grey'])
        if tok.get('total'):
            pct = 100 * tok['read'] / tok['total']
            x = bar(scr, 3, 7, bw, 100 - pct) + 1 if bw >= 4 else 7  # the bar fills with misses: green = good
            put(scr, 3, x, f'{pct:3.0f}% hit', curses.A_BOLD)
        else:
            put(scr, 3, 7, '—   no messages yet', C['dim'])
        ctx = context_fill(sid)
        if ctx:
            pct, used_tokens, window, exact = ctx
            row(4, 'ctx', pct, f"{'' if exact else '~'}{tokens_fmt(used_tokens)}/{tokens_fmt(window)}")
        else:
            window = (state.get('context') or {}).get('window')
            row(4, 'ctx', 0, f'0/{tokens_fmt(window)}' if window else 'no messages yet')
    run(draw, title='Claude stats')


def pane_model():
    st = {'row': 0, 'hits': [], 'msg': '', 'msg_at': 0.0}
    rows = [('model', MODELS), ('effort', EFFORTS)]

    def draw(scr):
        _, w = scr.getmaxyx()
        sid = current_id()
        info = session_info(sid)
        current = {'model': chip_for(info['model'], MODELS), 'effort': chip_for(info['effort'], EFFORTS)}
        st['hits'] = []
        col = 11  # the value column starts after "CONTEXT  │ "

        def label(y, text, tone='grey', bold=False):
            put(scr, y, 1, text.ljust(col - 4), C[tone] | curses.A_BOLD)
            put(scr, y, col - 2, '│', C['dim'])

        h = scr.getmaxyx()[0]
        base = 2 if h >= 5 else 1  # a short card drops the divider line, keeping all four rows
        label(0, 'SESSION')
        put(scr, 0, col, info['name'][:w - col - 1], curses.A_BOLD)
        if base == 2:
            put(scr, 1, 0, '─' * (col - 2) + '┼' + '─' * (w - col + 1), C['dim'])
        for r, (key, options) in enumerate(rows):
            y = base + r
            label(y, key.upper(), 'magenta' if info[key + '_set'] else 'grey')  # pink: overridden from this pane
            x = col
            narrow = col + sum(len(n) + 2 for n, _ in options) > w - 1
            for name, value in options:
                text = f' {name[:3] if narrow else name} '
                st['hits'].append((y, x, x + len(text), key, value))
                on = name == current[key] or (value is None and current[key] is None)
                x = put(scr, y, x, text, C['chip'] if on else (0 if r == st['row'] else C['grey']))
        cy = base + 2
        label(cy, 'CONTEXT')
        x = put(scr, cy, col, ' ⟲ Compact ', C['badge'])
        st['hits'].append((cy, col, x, 'compact', None))
        if time.time() - st['msg_at'] < 6:
            put(scr, cy, x + 1, st['msg'], C['yellow'])
        else:
            ctx = context_fill(sid)
            if ctx:
                x = put(scr, cy, x + 1, f'{ctx[0]:.0f}% full ', curses.A_BOLD)
                bar(scr, cy, x, max(4, min(12, w - x - 2)), ctx[0])

    def say(msg):
        st['msg'], st['msg_at'] = msg, time.time()

    def compact():
        sid = current_id()
        if any(d.get('sessionId') == sid and d.get('status') == 'busy' for d in registry()):
            return say('busy: try when the turn ends')
        main = tmux('show', '-t', T, '-v', '@dash_main')
        tmux('send-keys', '-t', main, '/compact')
        time.sleep(0.3)
        tmux('send-keys', '-t', main, 'Enter')
        say('sent /compact')

    def choose(key, step=None, value=None):
        sid = current_id()
        pick = load_override(sid)
        if step is not None:
            values = [v for _, v in (MODELS if key == 'model' else EFFORTS)]
            at = values.index(pick.get(key)) if pick.get(key) in values else 0
            value = values[(at + step) % len(values)]
        pick[key] = value
        save_override(sid, pick)

    def on_key(k):
        key = rows[st['row']][0]
        if k in (curses.KEY_UP, curses.KEY_DOWN, 9):
            st['row'] = 1 - st['row']
        elif k == curses.KEY_LEFT:
            choose(key, -1)
        elif k == curses.KEY_RIGHT:
            choose(key, 1)
        elif k == ord('c'):
            compact()
        elif k == 27:
            focus_main()

    def on_click(y, x):
        for hy, x0, x1, key, value in st['hits']:
            if y == hy and x0 <= x < x1:
                if key == 'compact':
                    return compact()
                st['row'] = 0 if key == 'model' else 1
                choose(key, value=value)

    run(draw, on_key, on_click=on_click, title='Model & Effort')


# ---------- layout ----------

def style(left, main):
    win = T
    for opt, val in [
        ('pane-border-status', 'off'),
        ('pane-border-lines', 'spaces'),
        ('pane-border-style', 'fg=colour238'),
        ('pane-active-border-style', 'fg=colour81'),
        ('pane-border-format', '#{?pane_active,#[fg=colour16 bg=colour81 bold],#[fg=colour81 bold]} #{pane_title} #[default]'),
    ]:
        tmux('set', '-w', '-t', win, opt, val)
    for opt, val in [
        ('mouse', 'on'),
        ('status-style', 'bg=colour235,fg=colour248'),
        ('status-left-length', '30'),
        ('status-right-length', '160'),
        ('status-left', '#[bg=colour81,fg=colour16,bold]  CLAUDE DASH  #[default] '),
        ('status-right', '#[fg=colour81,bold]ctrl+\\ #[fg=colour244]sessions  '
                         '#[fg=colour81,bold]click #[fg=colour244]focus  '
                         '#[fg=colour81,bold]tab #[fg=colour244]skills/agents  '
                         '#[fg=colour81,bold]ctrl+b d #[fg=colour244]detach '),
        ('window-status-format', ''),
        ('window-status-current-format', ''),
    ]:
        tmux('set', '-t', T, opt, val)
    tmux('set', '-g', 'focus-events', 'on')
    tmux('set', '-p', '-t', main, 'allow-set-title', 'off')
    tmux('set', '-p', '-t', main, 'remain-on-exit', 'on')  # if Claude exits, keep its pane so the layout holds
    tmux('bind', '-n', JUMP_KEY, 'run-shell', 'tmux select-pane -t "#{@dash_sessions}"')


CLI_MODELS = {'Opus': 'opus[1m]', 'Sonnet': 'sonnet', 'Haiku': 'haiku', 'Fable': 'fable'}


def ask_new_session():
    """Open the New session form in a popup over the dashboard."""
    tmux('display-popup', '-E', '-w', '64', '-h', '13', '-T', ' New Claude session ', f'{launcher()} newform')


def start_session(name, model, effort):
    """Start a new Claude session in the middle pane, with its name, model and effort (flags: this session only)."""
    sid = str(uuid.uuid4())
    args = [f'--session-id {sid}']
    if name.strip():
        args.append(f'-n {shlex.quote(name.strip())}')
    if model:
        args.append(f'--model {shlex.quote(CLI_MODELS[model])}')
    if effort:
        args.append(f'--effort {effort}')
    show_session(sid, claude_cmd(' '.join(args)))


def form_new_session():
    """Name (e.g. the ticket key), model and effort for a new session; auto keeps your defaults."""
    models = ['auto'] + list(CLI_MODELS)
    efforts = ['auto', 'low', 'medium', 'high', 'xhigh', 'max']
    st = {'name': '', 'field': 0, 'model': 0, 'effort': 0, 'hits': []}
    fields = ['name', 'model', 'effort', 'buttons']

    def draw(scr):
        scr.erase()
        h, w = scr.getmaxyx()
        st['hits'] = []

        def label(y, text, i):
            put(scr, y, 2, text, (C['cyan'] if st['field'] == i else C['grey']) | curses.A_BOLD)

        label(1, 'Name', 0)
        box = st['name'][-(w - 15):] + ('▏' if st['field'] == 0 else '')
        put(scr, 1, 10, box.ljust(w - 13), C['sel'] | curses.A_BOLD)
        if not st['name']:
            put(scr, 1, 11, 'e.g. DPG-1234 short description', C['sel:dim'])
        for row, (key, options) in enumerate((('model', models), ('effort', efforts)), start=1):
            y = 2 + row
            label(y, key.capitalize(), row)
            x = 10
            for i, name in enumerate(options):
                text = f' {name} '
                st['hits'].append((y, x, x + len(text), key, i))
                on = st[key] == i
                x = put(scr, y, x, text, C['chip'] if on else (0 if st['field'] == row else C['grey'])) + 1
        put(scr, 6, 10, 'auto = your default · these apply to this session only', C['dim'])
        y = 8
        x = put(scr, y, 10, ' Start ', (C['badge'] if st['field'] == 3 else C['sel:green']) | curses.A_BOLD)
        st['hits'].append((y, 10, x, 'start', 0))
        x2 = put(scr, y, x + 2, ' Cancel ', C['sel:grey'])
        st['hits'].append((y, x + 2, x2, 'cancel', 0))
        put(scr, h - 1, 2, 'Tab next · ←→ choose · Enter start · Esc cancel', C['dim'])
        scr.refresh()

    def main(scr):
        curses.curs_set(0)
        palette()
        curses.mousemask(curses.ALL_MOUSE_EVENTS)
        curses.mouseinterval(0)
        scr.keypad(True)
        while True:
            draw(scr)
            try:
                k = scr.get_wch()
            except curses.error:
                continue
            field = fields[st['field']]
            if k == curses.KEY_MOUSE:
                try:
                    _, mx, my, _, b = curses.getmouse()
                except curses.error:
                    continue
                for hy, x0, x1, key, i in st['hits']:
                    if my == hy and x0 <= mx < x1:
                        if key == 'start':
                            return True
                        if key == 'cancel':
                            return False
                        st[key] = i
                if my == 1:
                    st['field'] = 0
            elif k == '\x1b':
                return False
            elif k in ('\n', '\r', curses.KEY_ENTER):
                return True
            elif k in ('\t', curses.KEY_DOWN):
                st['field'] = (st['field'] + 1) % len(fields)
            elif k in (curses.KEY_BTAB, curses.KEY_UP):
                st['field'] = (st['field'] - 1) % len(fields)
            elif field in ('model', 'effort') and k in (curses.KEY_LEFT, curses.KEY_RIGHT):
                n = len(models if field == 'model' else efforts)
                st[field] = (st[field] + (1 if k == curses.KEY_RIGHT else -1)) % n
            elif field == 'name':
                if k in (curses.KEY_BACKSPACE, '\x7f', '\b'):
                    st['name'] = st['name'][:-1]
                elif isinstance(k, str) and k.isprintable():
                    st['name'] += k

    os.environ.setdefault('ESCDELAY', '25')
    set_locale()
    if curses.wrapper(main):
        start_session(st['name'], None if st['model'] == 0 else models[st['model']],
                      None if st['effort'] == 0 else efforts[st['effort']])


def pane_idle():
    """Shown in the middle when no Claude session is open there."""
    if os.environ.get('TMUX_PANE'):
        tmux('set', '-p', '-t', os.environ['TMUX_PANE'], '-u', '@dash_sid')
    def draw(scr):
        h, w = scr.getmaxyx()
        lines = [('No Claude session open here.', curses.A_BOLD),
                 ('', 0),
                 ('Pick one in Sessions on the left (Ctrl+\\ jumps there),', C['grey']),
                 ('or press Enter to start a new one (name, model, effort).', C['grey'])]
        top = max(0, h // 2 - len(lines))
        for i, (text, attr) in enumerate(lines):
            put(scr, top + i, max(0, (w - len(text)) // 2), text, attr)

    def on_key(k):
        if k in (10, 13, curses.KEY_ENTER, ord('n')):
            ask_new_session()  # the form restarts this pane with the new session

    run(draw, on_key, every=2.0, title='Claude')


def build_around(main):
    """Lay the side panes out around the middle pane."""
    cols = int(tmux('display', '-p', '-t', main, '#{window_width}') or 200)
    lw, rw = max(34, min(96, round(cols * 0.30))), max(34, min(72, round(cols * 0.26)))
    me = launcher()

    def split(*a):
        pane = tmux('split-window', '-P', '-F', '#{pane_id}', *a)
        if not pane:
            tmux('display-message', 'claude-dash: the window is too small; make it wider and run: claude-dash relayout')
            raise SystemExit(1)
        return pane
    left = split('-hb', '-t', main, '-l', str(lw), f'{me} sessions')
    agents = split('-h', '-t', main, '-l', str(rw), f'{me} agents')
    approach = split('-v', '-t', agents, '-l', '72%', f'{me} approach')
    skills = split('-v', '-t', approach, '-l', '55%', f'{me} skills')
    model = split('-v', '-t', skills, '-l', '7', f'{me} model')
    computer = split('-v', '-t', left, '-l', '7', f'{me} computer')  # same height as Model & Effort
    stats = split('-h', '-t', computer, '-l', '50%', f'{me} stats')
    for pane, title in [(left, 'Sessions'), (main, 'Claude'), (agents, 'Subagents'), (approach, 'Approach & Review'),
                        (skills, 'Skills | Agents | MCP'), (model, 'Model & Effort'), (computer, 'Computer'), (stats, 'Claude stats')]:
        tmux('select-pane', '-t', pane, '-T', title)
    tmux('set', '-t', T, '@dash_main', main)
    tmux('set', '-t', T, '@dash_sessions', left)
    for role, pane in [('left', left), ('agents', agents), ('approach', approach), ('skills', skills),
                       ('model', model), ('computer', computer), ('stats', stats)]:
        tmux('set', '-t', T, f'@dash_{role}', pane)
    style(left, main)
    # re-fit on every resize, so the layout works at any window size
    for hook in ('client-resized', 'client-attached'):
        tmux('set-hook', '-t', T, hook, 'run-shell -b ' + shlex.quote(f'{launcher()} fit'))
    fit()
    tmux('select-pane', '-t', main)


def fit():
    """Size every pane as a share of the window, never below what its content needs."""
    role = lambda r: tmux('show', '-t', T, '-v', f'@dash_{r}')
    size = tmux('display', '-p', '-t', role('left') or TMUX, '#{window_width} #{window_height}').split()
    if len(size) != 2:
        return
    w, h = map(int, size)
    clamp = lambda v, lo, hi: max(lo, min(hi, v))
    lw = clamp(round(w * 0.30), 34, 96)                # Sessions column: 30%
    rw = clamp(round(w * 0.26), 34, 72)                # right column: 26%
    small = 7 if h >= 28 else 6 if h >= 22 else 5      # Computer / Claude stats / Model & Effort rows
    tmux('resize-pane', '-t', role('left'), '-x', str(lw))
    tmux('resize-pane', '-t', role('agents'), '-x', str(rw))
    tmux('resize-pane', '-t', role('computer'), '-y', str(small))
    tmux('resize-pane', '-t', role('computer'), '-x', str(lw // 2))
    tmux('resize-pane', '-t', role('model'), '-y', str(small))
    rest = max(9, h - small - 3)                       # rows left for the right column's three cards
    tmux('resize-pane', '-t', role('agents'), '-y', str(max(5, round(rest * 0.30))))
    tmux('resize-pane', '-t', role('approach'), '-y', str(max(5, round(rest * 0.40))))


def relayout():
    """Rebuild the side panes around the Claude pane, keeping Claude running (or bring the middle back)."""
    main = tmux('show', '-t', T, '-v', '@dash_main')
    panes = tmux('list-panes', '-t', T, '-F', '#{pane_id}').split()
    if not panes:
        sys.exit('claude-dash: no dashboard is running. Start one with: claude-dash')
    if main not in panes:
        main = tmux('split-window', '-P', '-F', '#{pane_id}', '-t', panes[0], '-c', os.getcwd(), main_cmd(f'{launcher()} idle'))
        panes.append(main)
    for pane in panes:
        if pane != main:
            tmux('kill-pane', '-t', pane)
    build_around(main)


def start():
    # '=name' matches exactly: plain '-t claude-dash' would also match claude-dash-stash
    if subprocess.run(['tmux', 'has-session', '-t', f'={TMUX}'], capture_output=True, stdin=subprocess.DEVNULL).returncode == 0:
        os.execvp('tmux', ['tmux', 'attach', '-t', f'={TMUX}'])
    sid = str(uuid.uuid4())
    open(CURRENT, 'w').write(sid)
    cols, rows = os.get_terminal_size()
    first = f'{launcher()} democlaude' if DEMO else main_cmd(claude_cmd(f'--session-id {sid}'))
    main = tmux('new-session', '-d', '-s', TMUX, '-x', str(cols), '-y', str(rows - 1), '-P', '-F', '#{pane_id}',
                '-c', os.getcwd(), first)
    if not globals().get('DEMO'):
        tmux('set', '-p', '-t', main, '@dash_sid', sid)
    build_around(main)
    os.execvp('tmux', ['tmux', 'attach', '-t', f'={TMUX}'])


# ---------- demo: made-up data for a preview (and the README screenshots) ----------

DEMO_ID = '00000000-demo-4000-8000-000000000001'
DEMO_SESSIONS = [
    ('DPG-1543 workbench catalog cleanup', 'running', 'Removing the stale catalog entities, then re-running the sync job.', 40),
    (None, None, None, None),  # the focused one, filled below
    ('DPG-1634 team membership sync', 'running', 'Two of three retries now pass; checking the backoff on the third.', 300),
    ('Release notes for v2.4', 'waiting', 'Draft is ready. Should the breaking change go first or last?', 900),
    ('DPG-1807 backup failure alerts', 'waiting', 'Needs your OK to create the SNS topic in the shared account.', 2700),
    ('Refactor auth middleware', 'idle', 'Split into three files; all 48 tests pass.', 7200),
    ('Bump UI kit to 3.2', 'idle', 'Upgraded; two snapshot tests updated.', 20000),
    ('DPG-1509 start/stop error handling', 'completed', 'PR #136 merged; the alarm now fires on both paths.', 90000),
    ('Flaky e2e test investigation', 'completed', 'Root cause: a shared fixture. Fixed in PR #141.', 180000),
    ('Jira ticket triage', 'completed', 'Labelled 14 tickets and closed 3 duplicates.', 400000),
]
DEMO_FOCUS = ('DPG-2233 GuardDuty endpoint removal', 'running',
              'Endpoint deleted in dev; verifying the VPC has no leftover ENIs.', 5)
DEMO_AGENTS = [
    ('Explore', 'Find every use of the endpoint', 'completed', 'Search the infra repos for references to the GuardDuty VPC endpoint.',
     'Found 3 references: the network stack, a security group rule, and a runbook.'),
    ('Explore', 'List ENIs in the dev VPC', 'completed', 'List network interfaces in the dev VPC and who requested each.',
     'Twelve ENIs; only one belongs to the endpoint, the rest are Lambda and NAT.'),
    ('general-purpose', 'Draft the change plan', 'completed', 'Write a step-by-step plan to remove the endpoint safely.',
     'Plan: remove the SG rule, delete the endpoint, verify flow logs, then update the runbook.'),
    ('Explore', 'Check CloudTrail for denials', 'running', 'Look for AccessDenied events since the endpoint was removed.', ''),
]
DEMO_SUMMARY = {
    'title': 'DPG-2233 GuardDuty endpoint removal',
    'points': [
        {'label': 'Problem', 'text': 'Remove the unused GuardDuty VPC endpoint from the dev VPC without breaking findings.'},
        {'label': 'Issue', 'text': 'A security group rule and a runbook still referenced the endpoint.'},
        {'label': 'Used', 'text': '4 subagents, CloudTrail, VPC flow logs, network/stack.ts, security-groups.ts.'},
        {'label': 'Approach', 'text': 'Dev first, watch flow logs for a day, then staging; no all-account change.'},
        {'label': 'Changes', 'text': 'Deleted the endpoint and its SG rule; updated the runbook.'},
        {'label': 'Why', 'text': 'Small blast radius, and flow logs prove nothing else depended on it.'},
        {'label': 'Status', 'text': 'Done in dev, 48 tests pass; staging is next after clean flow logs.'},
    ],
}
DEMO_SKILLS = [
    ('code-review', '/code-review', 'Review the current diff for correctness bugs, at the effort level you choose.'),
    ('simplify', '/simplify', 'Review changed code for reuse, simplification and efficiency, then apply the fixes.'),
    ('pdf', '/pdf', 'Read, merge, split, fill or create PDF files.'),
    ('xlsx', '/xlsx', 'Open, clean, edit or create spreadsheets.'),
    ('ponytail:ponytail', '/ponytail:ponytail', 'Forces the simplest solution that actually works.'),
]
DEMO_MCP = """github: https://api.githubcopilot.com/mcp - ✔ Connected
atlassian: https://mcp.atlassian.com/v1/sse (SSE) - ✔ Connected
claude.ai Notion: https://mcp.notion.com/mcp - ! Needs authentication
claude.ai Linear: https://mcp.linear.app/mcp - ! Needs authentication
claude.ai Figma: https://mcp.figma.com/mcp - ✔ Connected
"""


def demo_rows():
    now = time.time()
    rows = []
    for i, (name, label, text, age) in enumerate(DEMO_SESSIONS):
        sid = DEMO_ID if name is None else f'00000000-demo-4000-8000-{i:012d}'
        name, label, text, age = DEMO_FOCUS if name is None else (name, label, text, age)
        rows.append({'sessionId': sid, 'id': sid[:8], 'name': name, 'label': label, 'text': text, 'at': now - age,
                     'kind': 'interactive', 'status': 'busy' if label == 'running' else 'idle'})
    return rows


def demo_seed():
    """Write the files the panes read, so the demo needs no real sessions."""
    for d in (STATE, OVERRIDE, os.path.join(DIR, 'reviews')):
        os.makedirs(d, exist_ok=True)
    reset = lambda h: time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(time.time() + h * 3600))
    json.dump({'model': 'claude-opus-5-5[1m]', 'effort': 'high', 'costUsd': 4.18,
               'context': {'tokens': 342000, 'window': 1000000, 'percent': 34.2},
               'limits': [{'kind': 'five_hour', 'percentUsed': 42, 'resetsAt': reset(2.4)},
                          {'kind': 'seven_day', 'percentUsed': 63, 'resetsAt': reset(52)}],
               'tokens': {'read': 9_100_000, 'total': 9_480_000}}, open(os.path.join(STATE, DEMO_ID + '.json'), 'w'))
    json.dump(dict(DEMO_SUMMARY, at=time.time() - 120), open(os.path.join(DIR, 'reviews', DEMO_ID + '.summary.json'), 'w'))
    json.dump({'00000000-demo-4000-8000-000000000009': time.time() + 10 ** 9}, open(os.path.join(DIR, 'closed.json'), 'w'))
    json.dump({DEMO_ID: 1}, open(os.path.join(DIR, 'pinned.json'), 'w'))


def pane_democlaude():
    """A still picture of a Claude conversation for the demo's middle pane."""
    dim, bold, cyan, green, off = '\033[2m', '\033[1m', '\033[36m', '\033[32m', '\033[0m'
    print(f"""
 {cyan}✻{off} {bold}Claude Code{off}
   Opus 5.5 (1M context) with high effort
   {dim}~/work/infra{off}

{dim}❯{off} {bold}DPG-2233: remove the GuardDuty VPC endpoint from the dev VPC safely{off}

{green}●{off} I'll find everything that references the endpoint first, then plan the removal.

{dim}  Ran 4 subagents · Explore ×3, general-purpose ×1{off}

{green}●{off} Three things depend on it: the network stack, one security group rule
  and a runbook. Removing it in dev first and watching flow logs keeps
  the blast radius small.

{dim}  Edited network/stack.ts · security-groups.ts{off}
{dim}  Ran npm test · 48 passed{off}

{green}●{off} Done in dev: the endpoint and its rule are gone and GuardDuty still reports
  findings. I'll check CloudTrail for AccessDenied events before we touch staging.

{dim}✻ Worked for 3m 12s{off}

{cyan}────────────────────────────────────────────────────────────────{off}
{bold}❯{off} """)
    while True:
        time.sleep(3600)


if DEMO:
    load_sessions = demo_rows
    current_id = lambda: DEMO_ID
    session_title = lambda sid, name: next((r['name'] for r in demo_rows() if r['sessionId'] == sid), name or sid[:8])
    transcript_usage = lambda sid: {'model': 'claude-opus-5-5[1m]', 'tokens': {'read': 9_100_000, 'total': 9_480_000},
                                    'context_tokens': 342000}
    load_subagents = lambda sid: [{'type': t, 'description': d, 'status': st, 'prompt': p, 'result': r,
                                   'at': time.time() - 60 * (4 - i)} for i, (t, d, st, p, r) in enumerate(DEMO_AGENTS)]
    load_skills = lambda: DEMO_SKILLS
    mcp_list_cmd = lambda: ['printf', '%s', DEMO_MCP]
    registry = lambda: []


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'start'
    if cmd in ('help', '-h', '--help'):
        print(__doc__)
    elif cmd == 'stop':
        for name in ('claude-dash', 'claude-dash-demo', 'claude-dash-stash', 'claude-dash-demo-stash'):
            tmux('kill-session', '-t', f'={name}')
    elif cmd == 'demo' and not DEMO:
        os.environ.update(CLAUDE_DASH_DEMO='1', CLAUDE_DASH_TMUX='claude-dash-demo',
                          CLAUDE_DASH_HOME=os.path.join(DIR, 'demo'))
        os.execv(sys.executable, [sys.executable, SELF, 'demo'])
    else:
        os.makedirs(STATE, exist_ok=True)
        os.makedirs(OVERRIDE, exist_ok=True)
        os.makedirs(REVIEWS, exist_ok=True)
        if DEMO:
            demo_seed()
        panes = {'sessions': pane_sessions, 'agents': pane_agents, 'approach': pane_approach, 'skills': pane_skills,
                 'computer': pane_computer, 'stats': pane_stats, 'model': pane_model, 'idle': pane_idle,
                 'democlaude': pane_democlaude, 'relayout': relayout, 'fit': fit, 'newform': form_new_session,
                 'start': start, 'demo': start}
        if cmd not in panes:
            sys.exit(f'claude-dash: unknown command "{cmd}". Try: claude-dash help')
        panes[cmd]()


if __name__ == '__main__':
    main()
