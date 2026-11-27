#!/usr/bin/env python3
"""MoneyMintSheets Pinterest autoposter (runs hourly on GitHub Actions).

Publishes pins from autopost/queue.json whose publish_at time has come, using the official
Pinterest API v5. State is kept in autopost/posted.json (committed back by the workflow).

Secrets (GitHub → Settings → Secrets and variables → Actions):
  PINTEREST_APP_ID, PINTEREST_APP_SECRET, PINTEREST_REFRESH_TOKEN
Optional variable: PINTEREST_API (default https://api.pinterest.com/v5; use https://api-sandbox.pinterest.com/v5 for Trial).
"""
import base64, datetime as dt, json, os, sys, time, urllib.parse, urllib.request, urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
API = os.environ.get('PINTEREST_API') or 'https://api.pinterest.com/v5'
MAX_PER_RUN = int(os.environ.get('MAX_PER_RUN', '5'))
STALE_HOURS = 72
MAX_TRIES = 3

def log(*a): print(*a, flush=True)

def http(method, url, token=None, body=None, form=None, basic=None, raw=None, headers=None):
    h = dict(headers or {})
    data = None
    if token: h['Authorization'] = 'Bearer ' + token
    if basic: h['Authorization'] = 'Basic ' + base64.b64encode(basic.encode()).decode()
    if body is not None: data = json.dumps(body).encode(); h['Content-Type'] = 'application/json'
    if form is not None: data = urllib.parse.urlencode(form).encode(); h['Content-Type'] = 'application/x-www-form-urlencoded'
    if raw is not None: data = raw
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            t = r.read().decode() or '{}'
            return r.status, (json.loads(t) if t.strip().startswith(('{', '[')) else {'text': t})
    except urllib.error.HTTPError as e:
        t = e.read().decode(errors='replace')
        try: return e.code, json.loads(t)
        except Exception: return e.code, {'text': t[:500]}

def access_token():
    app, sec, rt = (os.environ.get(k) for k in ('PINTEREST_APP_ID', 'PINTEREST_APP_SECRET', 'PINTEREST_REFRESH_TOKEN'))
    if not (app and sec and rt):
        log('Secrets not set yet – nothing to do.'); sys.exit(0)
    st, r = http('POST', API.replace('/v5', '/v5') + '/oauth/token', form={'grant_type': 'refresh_token', 'refresh_token': rt}, basic=f'{app}:{sec}')
    if st != 200 or 'access_token' not in r:
        log('Token refresh failed:', st, r); sys.exit(1)
    return r['access_token']

def boards(token):
    out, bm = {}, None
    while True:
        q = '?page_size=250' + (f'&bookmark={bm}' if bm else '')
        st, r = http('GET', API + '/boards' + q, token)
        if st != 200: log('List boards failed', st, r); sys.exit(1)
        for b in r.get('items', []): out[b['name']] = b['id']
        bm = r.get('bookmark')
        if not bm: return out

def board_id(token, cache, name):
    if name in cache: return cache[name]
    st, r = http('POST', API + '/boards', token, body={'name': name, 'privacy': 'PUBLIC'})
    if st in (200, 201): cache[name] = r['id']; log('Created board', name); return r['id']
    log('Create board failed', name, st, r); return None

def upload_video(token, url):
    st, r = http('POST', API + '/media', token, body={'media_type': 'video'})
    if st not in (200, 201): return None, f'media register {st} {r}'
    mid, up, params = r['media_id'], r['upload_url'], r['upload_parameters']
    with urllib.request.urlopen(url, timeout=120) as f: video = f.read()
    bnd = '----mm' + str(int(time.time() * 1000))
    parts = []
    for k, v in params.items():
        parts.append(f'--{bnd}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    parts.append(f'--{bnd}\r\nContent-Disposition: form-data; name="file"; filename="v.mp4"\r\nContent-Type: video/mp4\r\n\r\n'.encode() + video + b'\r\n')
    parts.append(f'--{bnd}--\r\n'.encode())
    st, r2 = http('POST', up, raw=b''.join(parts), headers={'Content-Type': 'multipart/form-data; boundary=' + bnd})
    if st not in (200, 201, 204): return None, f'S3 upload {st} {r2}'
    for _ in range(30):
        time.sleep(10)
        st, s = http('GET', API + f'/media/{mid}', token)
        if s.get('status') == 'succeeded': return mid, None
        if s.get('status') == 'failed': return None, f'media failed {s}'
    return None, 'media timeout'

def create_pin(token, bid, p):
    if p['media'].endswith('.mp4'):
        mid, err = upload_video(token, p['media'])
        if not mid: return None, err
        src = {'source_type': 'video_id', 'media_id': mid, 'cover_image_url': p['cover']}
    else:
        src = {'source_type': 'image_url', 'url': p['media']}
    body = {'board_id': bid, 'title': p['title'][:100], 'description': p['description'][:500], 'link': p['link'],
            'alt_text': p['title'][:500], 'media_source': src}
    st, r = http('POST', API + '/pins', token, body=body)
    if st in (200, 201): return r.get('id'), None
    return None, f'{st} {r}'

def main():
    queue = json.load(open(os.path.join(HERE, 'queue.json'), encoding='utf-8'))
    sp = os.path.join(HERE, 'posted.json')
    state = json.load(open(sp, encoding='utf-8')) if os.path.exists(sp) else {'posted': {}, 'failed': {}}
    now = dt.datetime.now(dt.timezone.utc)
    due = [p for p in queue if p['id'] not in state['posted'] and state['failed'].get(p['id'], {}).get('tries', 0) < MAX_TRIES
           and now - dt.timedelta(hours=STALE_HOURS) <= dt.datetime.fromisoformat(p['publish_at'].replace('Z', '+00:00')) <= now]
    log(f'{len(due)} pins due, posting up to {MAX_PER_RUN}')
    if not due: return
    token = access_token(); cache = boards(token)
    for p in due[:MAX_PER_RUN]:
        bid = board_id(token, cache, p['board'])
        pid, err = create_pin(token, bid, p) if bid else (None, 'no board')
        if pid:
            state['posted'][p['id']] = {'pin': pid, 'at': now.isoformat(timespec='seconds')}; log('OK', p['id'], p['title'])
        else:
            f = state['failed'].setdefault(p['id'], {'tries': 0}); f['tries'] += 1; f['error'] = str(err)[:300]; log('FAIL', p['id'], err)
        json.dump(state, open(sp, 'w', encoding='utf-8'), ensure_ascii=False, indent=0)
        time.sleep(3)

if __name__ == '__main__':
    main()
