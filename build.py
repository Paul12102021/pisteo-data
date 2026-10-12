#!/usr/bin/env python3
"""Prépare les parcours balisés d'OpenStreetMap pour Pisteo.

Entrée : un extrait .osm.pbf déjà filtré sur les relations route=hiking,foot,bicycle,mtb
         (osmium tags-filter … r/route=hiking,foot,bicycle,mtb).
Sortie : des tuiles JSON de 0,25° × 0,25° (t/<x>_<y>.json) + manifest.json.

Chaque tuile contient les chemins (ways) des parcours qui la traversent, simplifiés,
encodés en « polyline » (précision 1e-5), et la fiche de chaque parcours concerné.
Données © contributeurs OpenStreetMap, licence ODbL.
"""
import json, math, os, sys, time
from array import array
from collections import defaultdict

import osmium

GRID = 0.25            # taille d'une tuile, en degrés
TOL = 6.0              # simplification : écart maximal toléré, en mètres
KINDS = {'hiking': 'h', 'foot': 'f', 'bicycle': 'b', 'mtb': 'm'}
SKIP_ROLES = {'alternative', 'excursion', 'approach', 'connection', 'link', 'backward',
              'platform', 'stop', 'guidepost', 'start', 'end', 'information'}
TAGS_KEEP = ('name', 'name:fr', 'ref', 'network', 'distance', 'operator', 'osmc:symbol')


def hav(a_lat, a_lon, b_lat, b_lon):
    r = math.radians
    dl, dn = r(b_lat - a_lat), r(b_lon - a_lon)
    x = math.sin(dl / 2) ** 2 + math.cos(r(a_lat)) * math.cos(r(b_lat)) * math.sin(dn / 2) ** 2
    return 2 * 6371000 * math.asin(min(1, math.sqrt(x)))


def simplify(pts):
    """Douglas-Peucker (itératif) sur des points (lat, lon) en degrés ; tolérance TOL mètres."""
    n = len(pts)
    if n < 3:
        return pts
    lat0 = math.radians(pts[0][0])
    kx, ky = 111320 * math.cos(lat0), 110540
    xy = [(p[1] * kx, p[0] * ky) for p in pts]
    keep = bytearray(n)
    keep[0] = keep[-1] = 1
    stack = [(0, n - 1)]
    tol2 = TOL * TOL
    while stack:
        a, b = stack.pop()
        if b <= a + 1:
            continue
        ax, ay = xy[a]
        bx, by = xy[b]
        dx, dy = bx - ax, by - ay
        L2 = dx * dx + dy * dy
        best, bi = -1.0, -1
        for i in range(a + 1, b):
            px, py = xy[i]
            if L2 == 0:
                d2 = (px - ax) ** 2 + (py - ay) ** 2
            else:
                t = ((px - ax) * dx + (py - ay) * dy) / L2
                t = 0 if t < 0 else 1 if t > 1 else t
                qx, qy = ax + t * dx - px, ay + t * dy - py
                d2 = qx * qx + qy * qy
            if d2 > best:
                best, bi = d2, i
        if best > tol2:
            keep[bi] = 1
            stack.append((a, bi))
            stack.append((bi, b))
    return [p for p, k in zip(pts, keep) if k]


def encode(pts):
    """Polyline de Google, précision 1e-5."""
    out, plat, plon = [], 0, 0
    for lat, lon in pts:
        ilat, ilon = round(lat * 1e5), round(lon * 1e5)
        for v in (ilat - plat, ilon - plon):
            v = ~(v << 1) if v < 0 else v << 1
            while v >= 0x20:
                out.append(chr((0x20 | (v & 0x1f)) + 63))
                v >>= 5
            out.append(chr(v + 63))
        plat, plon = ilat, ilon
    return ''.join(out)


def tile_of(lat, lon):
    return int(math.floor((lon + 180) / GRID)), int(math.floor((lat + 90) / GRID))


def main(src, out):
    t0 = time.time()
    ways = {}          # id -> array('i') [lat1e6, lon1e6, …]
    routes = []        # (id, kind, tags, [(way_id, role)])
    wanted = set()
    for o in osmium.FileProcessor(src).with_locations():
        if o.is_way():
            coords = array('i')
            ok = True
            for nd in o.nodes:
                loc = nd.location
                if not loc.valid():
                    ok = False
                    continue
                coords.append(round(loc.lat * 1e6))
                coords.append(round(loc.lon * 1e6))
            if len(coords) >= 4:
                ways[o.id] = coords
        elif o.is_relation():
            t = dict(o.tags)
            if t.get('type') not in ('route', 'superroute') or t.get('route') not in KINDS:
                continue
            if t.get('state') == 'proposed' or 'disused' in t or 'abandoned' in t:
                continue
            mem = [(m.ref, m.role or '') for m in o.members if m.type == 'w' and (m.role or '') not in SKIP_ROLES]
            if not mem:
                continue  # super-itinéraire sans chemins propres : ses sections ont leur propre fiche
            routes.append((o.id, KINDS[t['route']], {k: t[k] for k in TAGS_KEEP if k in t}, mem))
    print(f'lecture : {len(ways)} chemins, {len(routes)} parcours, {time.time() - t0:.0f} s', flush=True)

    # simplification et encodage de chaque chemin utilisé (une seule fois, même s'il sert à plusieurs parcours)
    enc, wlen, wtiles, wbb = {}, {}, {}, {}
    used = {w for r in routes for w, _ in r[3]}
    npts = 0
    for wid in used:
        c = ways.get(wid)
        if c is None:
            continue
        pts = [(c[i] / 1e6, c[i + 1] / 1e6) for i in range(0, len(c), 2)]
        L = sum(hav(*pts[i - 1], *pts[i]) for i in range(1, len(pts)))
        s = simplify(pts)
        npts += len(s)
        enc[wid] = encode(s)
        wlen[wid] = L
        wtiles[wid] = {tile_of(lat, lon) for lat, lon in pts}
        la, lo = [p[0] for p in pts], [p[1] for p in pts]
        wbb[wid] = (min(la), min(lo), max(la), max(lo))
    del ways
    print(f'simplification : {npts} points gardés, {time.time() - t0:.0f} s', flush=True)

    # répartition par tuile
    tiles = defaultdict(lambda: {'r': [], 'w': {}})
    nroutes = 0
    for rid, kind, tags, mem in routes:
        mw = [(w, role) for w, role in mem if w in enc]
        if not mw:
            continue
        length = round(sum(wlen[w] for w, _ in mw))
        rtiles = set()
        for w, _ in mw:
            rtiles |= wtiles[w]
        bb = [round(min(wbb[w][0] for w, _ in mw), 5), round(min(wbb[w][1] for w, _ in mw), 5),
              round(max(wbb[w][2] for w, _ in mw), 5), round(max(wbb[w][3] for w, _ in mw), 5)]
        nroutes += 1
        for tk in rtiles:
            T = tiles[tk]
            idx = []
            for w, role in mw:
                if tk not in wtiles[w]:
                    continue
                if w not in T['w']:
                    T['w'][w] = len(T['w'])
                idx.append(T['w'][w])
            T['r'].append([rid, kind, tags, length, bb, idx])

    os.makedirs(os.path.join(out, 't'), exist_ok=True)
    total = 0
    names = []
    for (x, y), T in tiles.items():
        wl = [None] * len(T['w'])
        for w, i in T['w'].items():
            wl[i] = [w, enc[w]]
        data = json.dumps({'r': T['r'], 'w': wl}, ensure_ascii=False, separators=(',', ':'))
        name = f'{x}_{y}'
        with open(os.path.join(out, 't', name + '.json'), 'w', encoding='utf-8') as f:
            f.write(data)
        total += len(data)
        names.append(name)
    names.sort()
    manifest = {'v': 1, 'date': time.strftime('%Y-%m-%d'), 'grid': GRID, 'routes': nroutes,
                'tiles': names, 'bytes': total,
                'attribution': 'Données © contributeurs OpenStreetMap (ODbL)'}
    with open(os.path.join(out, 'manifest.json'), 'w', encoding='utf-8') as f:
        json.dump(manifest, f, ensure_ascii=False, separators=(',', ':'))
    print(f'écriture : {len(names)} tuiles, {nroutes} parcours, {total / 1e6:.1f} Mo, {time.time() - t0:.0f} s', flush=True)


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


if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2])
