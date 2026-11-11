#!/usr/bin/env python3
"""Photos et lieux traversés des parcours balisés, préparés d'avance pour Pisteo.

Entrée : le dossier publié (branche gh-pages) avec manifest.json et t/<x>_<y>.json (voir build.py).
Sortie : dans le même dossier, i/<x>_<y>.json et info.json (liste des tuiles prêtes).

Pour chaque parcours :
  - les photos libres de Wikimedia Commons prises à moins de 150 m du tracé (JPEG, format paysage,
    1000 px de large au moins), avec leur auteur et leur licence ;
  - les lieux qui ont un article sur Wikipédia à moins de 250 m du tracé, dans la ou les langues du pays
    (cover.json dit dans quel pays est le parcours ; France : français, Allemagne : allemand, Suisse : allemand, français, italien…).
La fiche d'un parcours est rangée dans UNE tuile : celle du coin sud-ouest de son rectangle englobant.

  i/<x>_<y>.json = {"<id du parcours>": {"h": empreinte du tracé, "t": "AAAA-MM-JJ",
                    "p": [[chemin de la vignette, lat, lon, auteur, licence, primée 0/1], …],
                    "w": [[nom du lieu, lat, lon], …]}, …}
  chemin de la vignette : « a/ab/Nom.jpg » → <thumb de info.json>a/ab/Nom.jpg/330px-Nom.jpg (ou 960px-)
                          page de la photo : https://commons.wikimedia.org/wiki/File:Nom.jpg

Le calcul est repris d'un passage à l'autre : une fiche déjà faite est gardée tant que le tracé n'a pas
changé (et pendant 150 jours au plus). Chaque passage s'arrête au bout du temps donné (--minutes) ; les
tuiles sont traitées de la plus proche à la plus lointaine d'un point (--near lat,lon).

Photos © leurs auteurs (licences libres, Wikimedia Commons). Lieux : Wikipédia (CC BY-SA).
"""
import argparse, hashlib, json, math, os, re, sys, threading, time
import urllib.error, urllib.parse, urllib.request
from collections import OrderedDict, defaultdict
from concurrent.futures import ThreadPoolExecutor

GRID = 0.25                 # tuiles de données (comme build.py)
PHOTO_M, PLACE_M = 150, 250  # distance maximale au tracé, en mètres
KEEP_DAYS = 150             # une fiche est recalculée au bout de ce délai, même si le tracé n'a pas changé
RATE = 12                   # demandes par seconde, au plus
THREADS = 10
UA = 'PisteoData/1.0 (https://pisteo.app; pisteo.app@gmail.com)'
# mêmes filtres que dans l'appli (app.js : RT_PH_SKIP, RT_WIKI_SKIP)
PH_SKIP = re.compile(r"plaque|panneau|blason|armoiries|\bcarte\b|\bplan\b|\bmap\b|logo|int[ée]rieur|interior|vitrail|vitraux|tombe|cimeti[èe]re|monument aux morts|portrait|affiche|timbre|inscription|\bborne\b|carte postale|\bCPA\b|d[ée]tail|\bgare\b|mairie|[ée]cole|signalisation|autoroute|cadastre|document|archives?\b|\bscan\b|overlord|\bchart\b", re.I)
WIKI_SKIP = re.compile(r"^(Liste|Canton|Communauté|Arrondissement|Ligne|Gare|Halte|Route|Autoroute|Circonscription|Élection|Unité urbaine|Aire d'attraction|Bataille|Paroisse|Diocèse|Intercommunalité|Tramway|Métro|Station"
                       r"|Bahnhof|Haltepunkt|Bahnstrecke|Bundesstraße|Bundesautobahn|Landesstraße|Kreisstraße|Staatsstraße|Landkreis|Verwaltungsgemeinschaft|Hauptstrasse|Autobahn"
                       r"|Stazione|Ferrovia|Strada (statale|provinciale|regionale)|Autostrada|Lista|Estación|Línea|Carretera|Autovía|Autopista|Anexo|Apeadero"
                       r"|List of|Lijst|Spoorlijn|Station|[ABMN]\d+ (road|motorway)|[A-Z]{1,2}[ -]?\d+$)\b"
                       r"|(railway station|railway line|\(Bahnhof\)| station)$", re.I)
# Wikipédia interrogé selon le pays du parcours (zones de cover.json = extraits Geofabrik), dans cet ordre
WIKIS = {'france': ['fr'], 'andorra': ['ca', 'fr'], 'belgium': ['fr', 'nl'], 'luxembourg': ['fr', 'de'], 'switzerland': ['de', 'fr', 'it'],
         'austria': ['de'], 'germany': ['de'], 'italy': ['it'], 'spain': ['es'], 'great-britain': ['en']}
# adresse de vignette donnée par Wikimedia (thumb.wikimedia.org depuis 2026, upload.wikimedia.org avant), suivie ou non de « ?utm_… »
THUMB = re.compile(r'^(https://(?:thumb|upload)\.wikimedia\.org/wikipedia/commons/thumb/)([0-9a-f]/[0-9a-f]{2}/([^/?]+))/\d+px-([^/?]+)(?:\?.*)?$')
thumb_base = ['https://thumb.wikimedia.org/wikipedia/commons/thumb/']
TAG = re.compile(r'<[^>]+>')


def tile_of(lat, lon):
    return int(math.floor((lon + 180) / GRID)), int(math.floor((lat + 90) / GRID))


def dist(a_lat, a_lon, b_lat, b_lon):
    """Distance en mètres (approximation plane, bien assez précise à ces échelles)."""
    x = math.radians(b_lon - a_lon) * math.cos(math.radians((a_lat + b_lat) / 2))
    y = math.radians(b_lat - a_lat)
    return 6371000 * math.hypot(x, y)


def decode(s):
    pts, i, lat, lon = [], 0, 0, 0
    while i < len(s):
        vals = []
        for _ in range(2):
            shift = res = 0
            while True:
                b = ord(s[i]) - 63
                i += 1
                res |= (b & 0x1f) << shift
                shift += 5
                if b < 0x20:
                    break
            vals.append(~(res >> 1) if res & 1 else res >> 1)
        lat += vals[0]
        lon += vals[1]
        pts.append((lat / 1e5, lon / 1e5))
    return pts


# ---------- demandes à Wikimedia : débit limité, nouvelles tentatives ----------
_lock, _next, stats = threading.Lock(), [0.0], defaultdict(int)


def api(host, params):
    params = dict(params, action='query', format='json', formatversion='2')
    data = urllib.parse.urlencode(params).encode()
    for attempt in range(6):
        with _lock:
            wait = _next[0] - time.time()
            _next[0] = max(time.time(), _next[0]) + 1 / RATE
        if wait > 0:
            time.sleep(wait)
        try:
            req = urllib.request.Request(f'https://{host}/w/api.php', data=data, headers={'User-Agent': UA})
            with urllib.request.urlopen(req, timeout=40) as r:
                j = json.loads(r.read().decode('utf-8'))
            if 'error' in j:
                raise RuntimeError(str(j['error'])[:200])
            stats['demandes'] += 1
            return j
        except urllib.error.HTTPError as e:
            stats['erreurs'] += 1
            pause = float(e.headers.get('Retry-After') or 0) if e.code in (429, 503) else 0
            time.sleep(min(120, max(pause, 2 ** attempt)))
        except Exception:  # réseau, délai dépassé, réponse illisible
            stats['erreurs'] += 1
            time.sleep(2 ** attempt)
    raise RuntimeError(f'pas de réponse de {host}')


# ---------- photos et articles par case : chaque case n'est demandée qu'une fois par passage ----------
class Cells:
    """Résultats de « geosearch » rangés par case carrée ; une case pleine (500 réponses) est redemandée en 4 quarts."""

    def __init__(self, host, size, margin, namespace, keep, cap=6000):
        self.host, self.size, self.margin, self.ns, self.keep, self.cap = host, size, margin, namespace, keep, cap
        self.memo = OrderedDict()

    def _fetch(self, ix, iy, level):
        s = self.size / (1 << level)
        lat0, lon0 = iy * s, ix * s
        clat, clon = lat0 + s / 2, lon0 + s / 2
        radius = min(10000, int(dist(clat, clon, lat0, lon0) + self.margin + 10))
        j = api(self.host, {'list': 'geosearch', 'gscoord': f'{clat:.5f}|{clon:.5f}', 'gsradius': max(10, radius),
                            'gslimit': 500, 'gsnamespace': self.ns})
        res = j.get('query', {}).get('geosearch', [])
        stats[f'{self.host[:7]} réponses'] += len(res)
        if res and not stats[f'{self.host[:7]} exemple']:
            stats[f'{self.host[:7]} exemple'] = 1
            print('exemple', self.host, json.dumps(res[0], ensure_ascii=False)[:300], flush=True)
        if len(res) >= 500 and level < 3:
            out = []
            for dx in (0, 1):
                for dy in (0, 1):
                    out += self._fetch(ix * 2 + dx, iy * 2 + dy, level + 1)
            return out
        return [(g['title'], g['lat'], g['lon']) for g in res if self.keep(g['title'])]

    def load(self, keys, pool):
        todo = [k for k in keys if k not in self.memo]
        for k, res in zip(todo, pool.map(lambda k: self._fetch(k[0], k[1], 0), todo)):
            self.memo[k] = res
        for k in keys:
            self.memo.move_to_end(k)
        while len(self.memo) > self.cap:
            self.memo.popitem(last=False)

    def key(self, lat, lon):
        return int(math.floor(lon / self.size)), int(math.floor(lat / self.size))


# ---------- tracé d'un parcours ----------
class Shape:
    """Points du tracé tous les 40 m, rangés par cases de 0,005° pour mesurer vite la distance d'un point au tracé."""
    C = 0.005

    def __init__(self, ways):
        self.grid = defaultdict(list)
        self.pts = []
        for w in ways:
            for (a_lat, a_lon), (b_lat, b_lon) in zip(w, w[1:]):
                n = max(1, int(dist(a_lat, a_lon, b_lat, b_lon) // 40))
                for i in range(n):
                    self._add(a_lat + (b_lat - a_lat) * i / n, a_lon + (b_lon - a_lon) * i / n)
            if w:
                self._add(*w[-1])

    def _add(self, lat, lon):
        self.pts.append((lat, lon))
        self.grid[(int(math.floor(lat / self.C)), int(math.floor(lon / self.C)))].append((lat, lon))

    def near(self, lat, lon):
        ky, kx, best = int(math.floor(lat / self.C)), int(math.floor(lon / self.C)), 1e9
        for y in (ky - 1, ky, ky + 1):
            for x in (kx - 1, kx, kx + 1):
                for p_lat, p_lon in self.grid.get((y, x), ()):
                    d = dist(lat, lon, p_lat, p_lon)
                    if d < best:
                        best = d
        return best

    def cells(self, cells, margin_m):
        """Cases (de la taille demandée) que touche le couloir du tracé."""
        m_lat = margin_m / 111320
        out = set()
        for lat, lon in self.pts:
            m_lon = m_lat / max(0.2, math.cos(math.radians(lat)))
            for dy in (-m_lat, m_lat):
                for dx in (-m_lon, m_lon):
                    out.add(cells.key(lat + dy, lon + dx))
        return out


def in_ring(lat, lon, ring):
    inside, j = False, len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


class Cover:
    """Zones couvertes (cover.json) : dans quel pays est un point ? Sans fichier : la France."""

    def __init__(self, path):
        self.areas = []
        if os.path.exists(path):
            for a in json.load(open(path, encoding='utf-8')).get('areas', []):
                rings = [p[0] for p in a['polys']]
                box = [(min(x for x, _ in r), min(y for _, y in r), max(x for x, _ in r), max(y for _, y in r)) for r in rings]
                self.areas.append((a['id'], rings, box))

    def of(self, lat, lon):
        for name, rings, box in self.areas:
            for r, (w, s_, e, n) in zip(rings, box):
                if w <= lon <= e and s_ <= lat <= n and in_ring(lat, lon, r):
                    return name
        return 'france'


def spot(lat, lon):
    """Case d'environ 500 m, pour répartir les photos le long du parcours."""
    return int(lat / 0.0045), int(lon / 0.0065)


def text(html):
    return re.sub(r'\s+', ' ', TAG.sub('', html or '')).replace('&amp;', '&').replace('&quot;', '"').replace('&#039;', "'").replace('&lt;', '<').replace('&gt;', '>').strip()


def photo_of(ii):
    """Photo utilisable ? → (chemin de vignette, auteur, licence, primée) ; sinon None."""
    if not ii:
        stats['photo sans détails'] += 1
        return None
    if ii.get('mime') != 'image/jpeg' or not ii.get('thumburl'):
        stats['photo pas JPEG ou sans vignette'] += 1
        return None
    w, h = ii.get('width') or 0, ii.get('height') or 1
    if w < 1000 or not 1.15 <= w / h <= 2.4:
        stats['photo petite ou pas paysage'] += 1
        return None
    m = THUMB.match(ii['thumburl'])
    if not m or m.group(3) != m.group(4):
        stats['photo adresse inattendue'] += 1
        if stats['photo adresse inattendue'] <= 3:
            print('adresse inattendue', ii['thumburl'][:300], flush=True)
        return None  # nom trop long (vignette renommée) ou adresse inattendue
    stats['photo gardée'] += 1
    thumb_base[0] = m.group(1)
    em = ii.get('extmetadata') or {}
    val = lambda k: (em.get(k) or {}).get('value') or ''
    return m.group(2), text(val('Artist'))[:50], text(val('LicenseShortName'))[:30], 1 if val('Assessments') else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('site')
    ap.add_argument('--minutes', type=float, default=300)
    ap.add_argument('--near', default='49.18,-0.37')
    ap.add_argument('--tiles', type=int, default=0, help='nombre maximal de tuiles à traiter (0 = pas de limite)')
    ap.add_argument('--force', action='store_true', help='recalculer même les fiches déjà faites')
    a = ap.parse_args()
    t0 = time.time()
    today = time.strftime('%Y-%m-%d')
    near = tuple(float(v) for v in a.near.split(','))
    man = json.load(open(os.path.join(a.site, 'manifest.json'), encoding='utf-8'))

    # 1. tous les parcours : chemins (encodés) et tuile de rangement
    ways, routes = {}, {}
    for name in man['tiles']:
        T = json.load(open(os.path.join(a.site, 't', name + '.json'), encoding='utf-8'))
        for rid, kind, tags, length, bb, idx in T['r']:
            R = routes.get(rid)
            if R is None:
                R = routes[rid] = {'len': length, 'bb': bb, 'w': set()}
            for i in idx:
                wid, enc = T['w'][i][0], T['w'][i][1]
                ways.setdefault(wid, enc)
                R['w'].add(wid)
    home = defaultdict(list)
    for rid, R in routes.items():
        home[tile_of(R['bb'][0], R['bb'][1])].append(rid)
    order = sorted(home, key=lambda k: dist(near[0], near[1], (k[1] + 0.5) * GRID - 90, (k[0] + 0.5) * GRID - 180))
    print(f'{len(routes)} parcours dans {len(home)} tuiles, lecture {time.time() - t0:.0f} s', flush=True)

    os.makedirs(os.path.join(a.site, 'i'), exist_ok=True)
    photos = Cells('commons.wikimedia.org', 0.05, PHOTO_M, 6, lambda t: bool(re.search(r'\.jpe?g$', t, re.I)) and not PH_SKIP.search(t))
    wikis = {}   # langue → cases de ce Wikipédia
    cover = Cover(os.path.join(a.site, 'cover.json'))
    grid01 = Cells('', 0.1, PLACE_M, 0, lambda t: True)   # sert seulement à nommer les cases de 0,1°
    infos = {}   # titre de fichier → photo_of(...) ou None
    done = fresh = reused = 0
    pool = ThreadPoolExecutor(THREADS)
    try:
        for tk in order:
            name = f'{tk[0]}_{tk[1]}'
            path = os.path.join(a.site, 'i', name + '.json')
            old = {}
            if os.path.exists(path):
                try:
                    old = json.load(open(path, encoding='utf-8'))
                except Exception:
                    old = {}
            # 2. fiches à refaire : tracé changé, fiche trop ancienne ou absente
            out, todo = {}, []
            for rid in home[tk]:
                R = routes[rid]
                h = hashlib.md5((','.join(map(str, sorted(R['w']))) + f':{R["len"]}').encode()).hexdigest()[:8]
                o = old.get(str(rid))
                age = (time.mktime(time.strptime(today, '%Y-%m-%d')) - time.mktime(time.strptime(o['t'], '%Y-%m-%d'))) / 86400 if o else 1e9
                if o and o.get('h') == h and age < KEEP_DAYS and not a.force:
                    out[str(rid)] = o
                    reused += 1
                else:
                    todo.append((rid, h))
            if todo:
                if time.time() - t0 > a.minutes * 60 or (a.tiles and done >= a.tiles):
                    continue  # plus de temps : les tuiles restantes gardent leurs fiches actuelles
                shapes = {rid: Shape([decode(ways[w]) for w in routes[rid]['w']]) for rid, _ in todo}
                pc = {rid: s.cells(photos, PHOTO_M) for rid, s in shapes.items()}
                wc = {rid: s.cells(grid01, PLACE_M) for rid, s in shapes.items()}
                photos.load(sorted(set().union(*pc.values())), pool)
                lang = {}
                for rid, _ in todo:
                    bb = routes[rid]['bb']
                    lang[rid] = WIKIS.get(cover.of((bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2), ['fr'])
                for lg in sorted({l for ls in lang.values() for l in ls}):
                    if lg not in wikis:
                        wikis[lg] = Cells(f'{lg}.wikipedia.org', 0.1, PLACE_M, 0, lambda t: True, cap=3000)
                    wikis[lg].load(sorted(set().union(*(wc[rid] for rid, _ in todo if lg in lang[rid]))), pool)
                # 3. photos candidates : près du tracé, 2 au plus par case de 500 m, les plus proches d'abord
                cand = {}
                for rid, _ in todo:
                    s, seen, near_ph = shapes[rid], set(), []
                    for k in pc[rid]:
                        for title, lat, lon in photos.memo[k]:
                            if title in seen:
                                continue
                            seen.add(title)
                            d = s.near(lat, lon)
                            if d <= PHOTO_M:
                                near_ph.append((d, title, lat, lon))
                    near_ph.sort()
                    per, keep = defaultdict(int), []
                    limit = min(80, 16 + int(routes[rid]['len'] / 1000 * 3))
                    for c in near_ph:
                        if per[spot(c[2], c[3])] < 2 and len(keep) < limit:
                            per[spot(c[2], c[3])] += 1
                            keep.append(c)
                    cand[rid] = keep
                    stats['photos près d\'un tracé'] += len(near_ph)
                    stats['photos candidates'] += len(keep)
                need = sorted({c[1] for cs in cand.values() for c in cs if c[1] not in infos})
                batches = [need[i:i + 50] for i in range(0, len(need), 50)]

                def details(titles):
                    j = api('commons.wikimedia.org', {'titles': '|'.join(titles), 'prop': 'imageinfo', 'iiprop': 'url|size|mime|extmetadata',
                                                      'iiurlwidth': 330, 'iiextmetadatafilter': 'Artist|LicenseShortName|Assessments', 'iiextmetadatalanguage': 'fr'})
                    q = j.get('query', {})
                    if not stats['détails exemple']:
                        stats['détails exemple'] = 1
                        print('exemple détails', json.dumps(j, ensure_ascii=False)[:900], flush=True)
                    back = {n['to']: n['from'] for n in q.get('normalized', [])}
                    res = {t: None for t in titles}
                    for pg in q.get('pages', []):
                        res[back.get(pg.get('title'), pg.get('title'))] = photo_of((pg.get('imageinfo') or [None])[0])
                    return res
                for res in pool.map(details, batches):
                    infos.update(res)
                # 4. fiche de chaque parcours
                for rid, h in todo:
                    s, km = shapes[rid], routes[rid]['len'] / 1000
                    ok = [(-(infos[t][3]), d, t, lat, lon) for d, t, lat, lon in cand[rid] if infos.get(t)]
                    ok.sort()
                    n_max = int(min(40, max(6, math.ceil(km * 0.8))))
                    picked, used = [], set()
                    for o in ok:                       # une photo par case de 500 m d'abord (primées en tête)…
                        if spot(o[3], o[4]) not in used and len(picked) < n_max:
                            used.add(spot(o[3], o[4]))
                            picked.append(o)
                    for o in ok:                       # …puis on complète
                        if len(picked) < min(n_max, 6) and o not in picked:
                            picked.append(o)
                    p = [[infos[t][0], round(lat, 5), round(lon, 5), infos[t][1], infos[t][2], infos[t][3]] for _, _, t, lat, lon in picked]
                    seen, near_pl = set(), []
                    for lg in lang[rid]:                 # la première langue du pays d'abord ; un même lieu n'est gardé qu'une fois
                        got = []
                        for k in wc[rid]:
                            for title, lat, lon in wikis[lg].memo[k]:
                                nm = re.sub(r'\s*\([^)]*\)$', '', title)
                                if nm in seen or len(nm) > 45 or WIKI_SKIP.search(nm) or WIKI_SKIP.search(title):
                                    continue
                                if any(dist(lat, lon, o[2], o[3]) < 150 for o in near_pl):
                                    continue
                                d = s.near(lat, lon)
                                if d <= PLACE_M:
                                    seen.add(nm)
                                    got.append((d, nm, lat, lon))
                        near_pl += got
                    near_pl.sort()
                    w = [[nm, round(lat, 5), round(lon, 5)] for _, nm, lat, lon in near_pl[:int(min(40, max(6, math.ceil(km))))]]
                    out[str(rid)] = {'h': h, 't': today, 'p': p, 'w': w}
                    fresh += 1
                if len(infos) > 300000:
                    infos.clear()
                done += 1
                if done % 10 == 0 or done <= 3:
                    print(f'{done} tuiles calculées ({fresh} fiches), {stats["demandes"]} demandes, {stats["erreurs"]} erreurs, {(time.time() - t0) / 60:.1f} min', flush=True)
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(out, f, ensure_ascii=False, separators=(',', ':'))
    finally:
        pool.shutdown(wait=False)
        # liste des tuiles prêtes (toutes leurs fiches faites), même si le passage s'arrête sur une erreur
        ready, n_ready = [], 0
        for tk in home:
            path = os.path.join(a.site, 'i', f'{tk[0]}_{tk[1]}.json')
            if os.path.exists(path):
                try:
                    have = json.load(open(path, encoding='utf-8'))
                except Exception:
                    continue
                if all(str(r) in have for r in home[tk]):
                    ready.append(f'{tk[0]}_{tk[1]}')
                    n_ready += len(home[tk])
        ready.sort()
        with open(os.path.join(a.site, 'info.json'), 'w', encoding='utf-8') as f:
            json.dump({'v': 1, 'date': today, 'grid': GRID, 'tiles': ready, 'routes': n_ready, 'total': len(routes), 'thumb': thumb_base[0],
                       'attribution': 'Photos © leurs auteurs (Wikimedia Commons, licences libres) ; lieux : Wikipédia (CC BY-SA)'}, f, ensure_ascii=False, separators=(',', ':'))
        print('détail :', dict(stats), flush=True)
        print(f'fin : {fresh} fiches calculées, {reused} gardées, {len(ready)}/{len(home)} tuiles prêtes ({n_ready}/{len(routes)} parcours), '
              f'{stats["demandes"]} demandes, {stats["erreurs"]} erreurs, {(time.time() - t0) / 60:.1f} min', flush=True)


if __name__ == '__main__':
    main()
