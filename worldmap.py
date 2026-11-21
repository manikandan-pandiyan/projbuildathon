"""Static map model: ground height, solid obstacles, line of sight, and a budgeted A* planner.

Everything is precomputed once at `hello` on a 2 m grid (LOS / walls) and a 4 m grid (pathing).
Pure standard library so the image needs nothing but python:3.13-slim.
"""
from __future__ import annotations

import array
import heapq
import json
import math
import os
import sys
import time

CELL = 2.0      # fine grid (LOS, walls)
NAV = 4.0       # coarse grid (A*)
NO_SOLID = -1e9


def _log(*a):
    print(*a, file=sys.stderr, flush=True)


class WorldMap:
    def __init__(self, map_dir: str, step_up: float = 1.2, height: float = 9.0):
        t0 = time.perf_counter()
        with open(os.path.join(map_dir, "v2", "world.json")) as f:
            w = json.load(f)
        ter = w["terrain"]
        self.tnx, self.tny = int(ter["nx"]), int(ter["ny"])
        self.tminx, self.tminy, self.tstep = float(ter["minX"]), float(ter["minY"]), float(ter["step"])
        raw = array.array("h")
        with open(os.path.join(map_dir, "v2", ter.get("file", "terrain.i16")), "rb") as f:
            raw.frombytes(f.read())
        if sys.byteorder != "little":
            raw.byteswap()
        scale = float(ter.get("scale", 0.01))
        self.terrain = array.array("f", (v * scale for v in raw))

        b = w["bounds"]
        self.minx, self.miny = float(b["minX"]), float(b["minY"])
        self.maxx, self.maxy = float(b["maxX"]), float(b["maxY"])
        self.nx = int(math.ceil((self.maxx - self.minx) / CELL)) + 1
        self.ny = int(math.ceil((self.maxy - self.miny) / CELL)) + 1
        n = self.nx * self.ny

        # ground height per fine cell (at the cell center)
        g = array.array("f", bytes(4 * n))
        for j in range(self.ny):
            y = self.miny + (j + 0.5) * CELL
            row = j * self.nx
            for i in range(self.nx):
                g[row + i] = self.ground(self.minx + (i + 0.5) * CELL, y)
        self.g = g

        lo = array.array("f", [1e9]) * n     # lowest solid base in the cell
        hi = array.array("f", [NO_SOLID]) * n  # highest solid top in the cell
        wall = bytearray(n)                  # 1 = blocks a walking mech (relative to terrain)
        for p in w["prisms"]:
            ring = p["ring"]
            holes = p.get("holes") or []
            pb, pt = float(p["base"]), float(p["top"])
            xs, ys = ring[0::2], ring[1::2]
            i0 = max(0, int((min(xs) - self.minx) / CELL))
            i1 = min(self.nx - 1, int((max(xs) - self.minx) / CELL))
            j0 = max(0, int((min(ys) - self.miny) / CELL))
            j1 = min(self.ny - 1, int((max(ys) - self.miny) / CELL))
            for j in range(j0, j1 + 1):
                y = self.miny + (j + 0.5) * CELL
                xsct = _row_crossings(ring, y)
                if not xsct:
                    continue
                hx = [_row_crossings(h, y) for h in holes]
                for i in range(i0, i1 + 1):
                    x = self.minx + (i + 0.5) * CELL
                    if not _inside_sorted(xsct, x):
                        continue
                    if any(_inside_sorted(h, x) for h in hx if h):
                        continue
                    k = j * self.nx + i
                    if pb < lo[k]:
                        lo[k] = pb
                    if pt > hi[k]:
                        hi[k] = pt
                    gk = g[k]
                    if pt > gk + step_up and pb < gk + height:
                        wall[k] = 1
        self.lo, self.hi, self.wall = lo, hi, wall

        # coarse nav grid: blocked if any fine sub-cell is a wall (keeps the 5 m wide mech out of slits)
        r = int(NAV / CELL)
        self.cnx = (self.nx + r - 1) // r
        self.cny = (self.ny + r - 1) // r
        cn = self.cnx * self.cny
        nb = bytearray(cn)
        ng = array.array("f", bytes(4 * cn))
        for cj in range(self.cny):
            for ci in range(self.cnx):
                cnt = 0
                gs = 0.0
                m = 0
                for dj in range(r):
                    j = cj * r + dj
                    if j >= self.ny:
                        continue
                    for di in range(r):
                        i = ci * r + di
                        if i >= self.nx:
                            continue
                        k = j * self.nx + i
                        cnt += wall[k]
                        gs += g[k]
                        m += 1
                kk = cj * self.cnx + ci
                nb[kk] = 1 if cnt >= 1 else 0
                ng[kk] = gs / max(1, m)
        # map border is a wall
        for ci in range(self.cnx):
            nb[ci] = 1
            nb[(self.cny - 1) * self.cnx + ci] = 1
        for cj in range(self.cny):
            nb[cj * self.cnx] = 1
            nb[cj * self.cnx + self.cnx - 1] = 1
        self.nblock, self.ng = nb, ng
        self.max_climb = NAV * math.tan(math.radians(48))
        self._search = None
        _log(f"worldmap built in {time.perf_counter() - t0:.2f}s grid {self.nx}x{self.ny} nav {self.cnx}x{self.cny}")

    # ---------------- terrain / solids ----------------
    def ground(self, x: float, y: float) -> float:
        fx = (x - self.tminx) / self.tstep
        fy = (y - self.tminy) / self.tstep
        i = int(fx)
        j = int(fy)
        if i < 0 or j < 0 or i >= self.tnx - 1 or j >= self.tny - 1:
            i = min(max(i, 0), self.tnx - 1)
            j = min(max(j, 0), self.tny - 1)
            return self.terrain[j * self.tnx + i]
        tx, ty = fx - i, fy - j
        t = self.terrain
        k = j * self.tnx + i
        a = t[k] * (1 - tx) + t[k + 1] * tx
        c = t[k + self.tnx] * (1 - tx) + t[k + self.tnx + 1] * tx
        return a * (1 - ty) + c * ty

    def cell(self, x, y):
        i = int((x - self.minx) / CELL)
        j = int((y - self.miny) / CELL)
        if i < 0 or j < 0 or i >= self.nx or j >= self.ny:
            return -1
        return j * self.nx + i

    def is_wall(self, x, y) -> bool:
        k = self.cell(x, y)
        return k < 0 or self.wall[k] == 1

    def solid_at(self, x, y, z) -> bool:
        k = self.cell(x, y)
        if k < 0:
            return True
        if z < self.g[k] - 0.3:
            return True
        return self.lo[k] <= z <= self.hi[k]

    def top_at(self, x, y) -> float:
        """Height of the highest surface at (x, y) (roof or ground)."""
        k = self.cell(x, y)
        if k < 0:
            return 0.0
        return max(self.g[k], self.hi[k])

    def los(self, a, b, step: float = 2.0) -> bool:
        """True if the straight segment a->b (3D points) clears buildings and terrain."""
        dx, dy, dz = b[0] - a[0], b[1] - a[1], b[2] - a[2]
        d = math.hypot(dx, dy)
        n = int(d / step) + 1
        if n <= 1:
            return True
        lo, hi, g = self.lo, self.hi, self.g
        nx, minx, miny = self.nx, self.minx, self.miny
        inv = 1.0 / n
        for s in range(1, n):
            t = s * inv
            x = a[0] + dx * t
            y = a[1] + dy * t
            i = int((x - minx) / CELL)
            j = int((y - miny) / CELL)
            if i < 0 or j < 0 or i >= nx or j >= self.ny:
                return False
            k = j * nx + i
            z = a[2] + dz * t
            if z < g[k] - 0.5:
                return False
            if lo[k] <= z <= hi[k]:
                return False
        return True

    # ---------------- pathing ----------------
    def ncell(self, x, y):
        ci = int((x - self.minx) / NAV)
        cj = int((y - self.miny) / NAV)
        ci = min(max(ci, 0), self.cnx - 1)
        cj = min(max(cj, 0), self.cny - 1)
        return cj * self.cnx + ci

    def ncenter(self, k):
        cj, ci = divmod(k, self.cnx)
        return (self.minx + (ci + 0.5) * NAV, self.miny + (cj + 0.5) * NAV)

    def nearest_free(self, k, maxr=6):
        if not self.nblock[k]:
            return k
        cj, ci = divmod(k, self.cnx)
        for r in range(1, maxr + 1):
            for dj in range(-r, r + 1):
                for di in (-r, r) if abs(dj) != r else range(-r, r + 1):
                    i, j = ci + di, cj + dj
                    if 0 <= i < self.cnx and 0 <= j < self.cny:
                        kk = j * self.cnx + i
                        if not self.nblock[kk]:
                            return kk
        return k

    def start_search(self, start_xy, goal_xy):
        s = self.nearest_free(self.ncell(*start_xy), 3)
        gl = self.nearest_free(self.ncell(*goal_xy))
        self._search = {
            "start": s, "goal": gl, "open": [(0.0, s)], "gcost": {s: 0.0}, "came": {s: -1},
            "best": s, "besth": 1e18, "done": False, "path": None, "exp": 0,
        }
        return self._search

    def step_search(self, budget_s: float):
        S = self._search
        if S is None or S["done"]:
            return S
        t_end = time.perf_counter() + budget_s
        cnx, cny = self.cnx, self.cny
        nb, ng = self.nblock, self.ng
        goal = S["goal"]
        gj, gi = divmod(goal, cnx)
        openh, gcost, came = S["open"], S["gcost"], S["came"]
        mc = self.max_climb
        start = S["start"]
        n_iter = 0
        while openh:
            n_iter += 1
            if (n_iter & 63) == 0 and time.perf_counter() > t_end:
                return S
            f, k = heapq.heappop(openh)
            gk = gcost[k]
            cj, ci = divmod(k, cnx)
            h = math.hypot(ci - gi, cj - gj)
            if h < S["besth"]:
                S["besth"], S["best"] = h, k
            if k == goal:
                break
            S["exp"] += 1
            if S["exp"] > 40000:
                break
            z0 = ng[k]
            for di, dj, c in _NEI:
                i, j = ci + di, cj + dj
                if i < 0 or j < 0 or i >= cnx or j >= cny:
                    continue
                kk = j * cnx + i
                if nb[kk] and kk != goal:
                    continue
                if di and dj and (nb[cj * cnx + i] or nb[j * cnx + ci]):
                    continue  # no corner cutting
                dz = ng[kk] - z0
                if dz > mc * c and k != start:
                    continue
                ng_ = gk + c + (0.15 * dz if dz > 0 else 0.0)
                old = gcost.get(kk)
                if old is None or ng_ < old - 1e-9:
                    gcost[kk] = ng_
                    came[kk] = k
                    heapq.heappush(openh, (ng_ + math.hypot(i - gi, j - gj), kk))
        # finished (or exhausted): build path to goal or to closest reached node
        end = goal if goal in came else S["best"]
        path = []
        k = end
        while k != -1:
            path.append(self.ncenter(k))
            k = came[k]
        path.reverse()
        S["path"] = _smooth(self, path)
        S["done"] = True
        return S

    def block_near(self, x, y):
        k = self.ncell(x, y)
        self.nblock[k] = 1

    def walk_clear(self, a, b) -> bool:
        """Is the straight ground walk a->b (2D) free of walls (sampled on the fine grid, mech-wide)?"""
        dx, dy = b[0] - a[0], b[1] - a[1]
        d = math.hypot(dx, dy)
        if d < 1e-6:
            return True
        n = int(d / 1.0) + 1
        px, py = -dy / d * 2.6, dx / d * 2.6
        wall = self.wall
        for s in range(1, n + 1):
            t = s / n
            x, y = a[0] + dx * t, a[1] + dy * t
            for ox, oy in ((0.0, 0.0), (px, py), (-px, -py)):
                k = self.cell(x + ox, y + oy)
                if k < 0 or wall[k]:
                    return False
        return True


_S2 = math.sqrt(2)
_NEI = [(1, 0, 1.0), (-1, 0, 1.0), (0, 1, 1.0), (0, -1, 1.0),
        (1, 1, _S2), (1, -1, _S2), (-1, 1, _S2), (-1, -1, _S2)]


def _smooth(m: WorldMap, path):
    if len(path) <= 2:
        return path
    out = [path[0]]
    i = 0
    while i < len(path) - 1:
        j = min(len(path) - 1, i + 10)
        while j > i + 1 and not m.walk_clear(path[i], path[j]):
            j -= 1
        out.append(path[j])
        i = j
    return out


def _row_crossings(ring, y):
    xs = []
    n = len(ring) // 2
    for a in range(n):
        x1, y1 = ring[2 * a], ring[2 * a + 1]
        b = (a + 1) % n
        x2, y2 = ring[2 * b], ring[2 * b + 1]
        if (y1 > y) != (y2 > y):
            xs.append(x1 + (y - y1) * (x2 - x1) / (y2 - y1))
    xs.sort()
    return xs


def _inside_sorted(xs, x):
    c = 0
    for v in xs:
        if v < x:
            c += 1
        else:
            break
    return (c & 1) == 1
