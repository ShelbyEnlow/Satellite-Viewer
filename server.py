from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse
from urllib.request import Request, urlopen
import json
import re
import time

HOST = '127.0.0.1'
PORT = 8000

CACHE_TTL_SEC = 300
_cache = {
    'ts': 0.0,
    'payload': None,
}

SATNOGS_URL = 'https://db.satnogs.org/api/tle/'
CELESTRAK_GROUP_URLS = [
    ('active', 'https://celestrak.org/NORAD/elements/gp.php?GROUP=active&FORMAT=tle'),
    ('starlink', 'https://celestrak.org/NORAD/elements/gp.php?GROUP=starlink&FORMAT=tle'),
    ('weather', 'https://celestrak.org/NORAD/elements/gp.php?GROUP=weather&FORMAT=tle'),
    ('stations', 'https://celestrak.org/NORAD/elements/gp.php?GROUP=stations&FORMAT=tle'),
    ('geo', 'https://celestrak.org/NORAD/elements/gp.php?GROUP=geo&FORMAT=tle'),
]


def fetch_satnogs_tle():
    req = Request(SATNOGS_URL, headers={
        'User-Agent': 'sat-viewer-local-proxy/1.0',
        'Accept': 'application/json',
    })
    with urlopen(req, timeout=25) as resp:
        body = resp.read().decode('utf-8', errors='replace')
    data = json.loads(body)
    if not isinstance(data, list):
        return []
    return data


def fetch_text(url):
    req = Request(url, headers={
        'User-Agent': 'sat-viewer-local-proxy/1.0',
        'Accept': 'text/plain, */*',
    })
    with urlopen(req, timeout=25) as resp:
        return resp.read().decode('utf-8', errors='replace')


def parse_tle_triplets(text):
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    out = []
    i = 0
    while i + 2 < len(lines):
        l0 = lines[i]
        l1 = lines[i + 1]
        l2 = lines[i + 2]
        if l1.startswith('1 ') and l2.startswith('2 '):
            out.append((l0, l1, l2))
            i += 3
            continue
        i += 1
    return out


def extract_norad(l1):
    m = re.match(r'^1\s+(\d+)', l1)
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def normalize_record(name, l1, l2, source):
    clean_name = (name or '').strip()
    if clean_name.startswith('0 '):
        clean_name = clean_name[2:].strip()
    norad = extract_norad(l1)
    return {
        'tle0': f'0 {clean_name}' if clean_name else f'0 NORAD {norad or ""}'.strip(),
        'tle1': l1.strip(),
        'tle2': l2.strip(),
        'tle_source': source,
        'norad_cat_id': norad,
        'updated': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
    }


def fetch_merged_live_tle():
    merged = []
    seen = set()

    for row in fetch_satnogs_tle():
        l0 = (row.get('tle0') or '').strip()
        l1 = (row.get('tle1') or '').strip()
        l2 = (row.get('tle2') or '').strip()
        if not (l1.startswith('1 ') and l2.startswith('2 ')):
            continue
        norad = row.get('norad_cat_id') or extract_norad(l1)
        key = str(norad) if norad is not None else f'{l1}|{l2}'
        if key in seen:
            continue
        seen.add(key)
        merged.append(normalize_record(l0, l1, l2, row.get('tle_source') or 'SatNOGS'))

    for group_name, url in CELESTRAK_GROUP_URLS:
        try:
            text = fetch_text(url)
        except Exception:
            continue
        for l0, l1, l2 in parse_tle_triplets(text):
            norad = extract_norad(l1)
            key = str(norad) if norad is not None else f'{l1}|{l2}'
            if key in seen:
                continue
            seen.add(key)
            merged.append(normalize_record(l0, l1, l2, f'CelesTrak {group_name}'))

    return merged


class Handler(SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header('Cache-Control', 'no-store')
        super().end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == '/api/tle':
            return self.handle_api_tle()
        return super().do_GET()

    def handle_api_tle(self):
        global _cache
        now = time.time()
        try:
            if not _cache['payload'] or (now - _cache['ts']) > CACHE_TTL_SEC:
                rows = fetch_merged_live_tle()
                _cache = {
                    'ts': now,
                    'payload': rows,
                }
            payload = json.dumps(_cache['payload'])
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(payload.encode('utf-8'))))
            self.end_headers()
            self.wfile.write(payload.encode('utf-8'))
        except Exception as exc:
            msg = json.dumps({'error': str(exc)})
            self.send_response(502)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(msg.encode('utf-8'))))
            self.end_headers()
            self.wfile.write(msg.encode('utf-8'))


if __name__ == '__main__':
    print(f'Serving sat_viewer with live TLE proxy on http://{HOST}:{PORT}')
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.serve_forever()
