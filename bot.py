"""Mech Royale bot: survive the zone, farm cubes and weapons safely, fight with led shots, break line of sight to regenerate."""
from __future__ import annotations

import math
import random
import sys
import time

import mechfight as mf
from worldmap import WorldMap

TAU = 2 * math.pi

# ---- strategy knobs (variants differ only here) ----
VARIANT = "J"
PRESETS = {
    # balanced: chase enemies we can't see, retreat at 42%
    "A": dict(retreat_hp=0.42, crowd_retreat=True, resume_hp=0.8, no_los="chase", chase_hp=0.5,
              want_gun=75, want_gun_vs_laser=55, want_laser=90, farm_hp=0.55),
    # aggressive: fight longer, close in, ambush less
    "B": dict(retreat_hp=0.25, crowd_retreat=False, resume_hp=0.65, no_los="chase", chase_hp=0.3,
              want_gun=55, want_gun_vs_laser=40, want_laser=70, farm_hp=0.45),
    # B + actively hunt: with nothing to loot, head for the last known enemy or the latest gunfire
    "D": dict(retreat_hp=0.25, crowd_retreat=False, resume_hp=0.65, no_los="chase", chase_hp=0.3,
              want_gun=55, want_gun_vs_laser=40, want_laser=70, farm_hp=0.45, seek=True),
    # D + never retreat, fight even closer
    "E": dict(retreat_hp=0.0, crowd_retreat=False, resume_hp=0.5, no_los="chase", chase_hp=0.0,
              want_gun=40, want_gun_vs_laser=30, want_laser=60, farm_hp=0.4, seek=True),
    # D + fighting skill: focus fire, grab cubes/weapons mid-fight, save energy for lightning
    "F": dict(retreat_hp=0.25, crowd_retreat=False, resume_hp=0.65, no_los="chase", chase_hp=0.3,
              want_gun=55, want_gun_vs_laser=40, want_laser=70, farm_hp=0.45, seek=True, skill=True),
    # F + loot harder: wider cube grabbing in fights, farm drones/crates from further and at lower HP
    "H": dict(retreat_hp=0.25, crowd_retreat=False, resume_hp=0.65, no_los="chase", chase_hp=0.3,
              want_gun=55, want_gun_vs_laser=40, want_laser=70, farm_hp=0.35, seek=True, skill=True,
              grab_r=50, loot_r=260, npc_hp=0.45),
    # F + offense: stronger focus fire, damage upgrades first
    "I": dict(retreat_hp=0.25, crowd_retreat=False, resume_hp=0.65, no_los="chase", chase_hp=0.3,
              want_gun=55, want_gun_vs_laser=40, want_laser=70, farm_hp=0.45, seek=True, skill=True,
              focus=0.22, offense=True),
    # F + duck behind nearby cover while every weapon is reloading / cooling down
    "J": dict(retreat_hp=0.25, crowd_retreat=False, resume_hp=0.65, no_los="chase", chase_hp=0.3,
              want_gun=55, want_gun_vs_laser=40, want_laser=70, farm_hp=0.45, seek=True, skill=True,
              peek=True),
    # E + fighting skill
    "G": dict(retreat_hp=0.0, crowd_retreat=False, resume_hp=0.5, no_los="chase", chase_hp=0.0,
              want_gun=40, want_gun_vs_laser=30, want_laser=60, farm_hp=0.4, seek=True, skill=True),
    # ambusher: hold still behind cover when the enemy has no line of sight, retreat early
    "C": dict(retreat_hp=0.5, crowd_retreat=True, resume_hp=0.85, no_los="hold", chase_hp=0.7,
              want_gun=75, want_gun_vs_laser=55, want_laser=95, farm_hp=0.6),
}
P = PRESETS[VARIANT]
WEAPON_VALUE = {"laser": 3.0, "lightning": 2.6, "gun": 2.0, "mortar": 1.2}
UPGRADE_PRIORITY = {
    "armor": 10, "plating": 9.5, "nanites": 8.5, "laser_charge": 8, "laser_damage": 7.5, "gun_rate": 7.5,
    "lightning_efficiency": 7, "gun_damage": 6.5, "lightning_range": 6.5, "gyros": 5.5, "servos": 5.5,
    "gun_mag": 5, "capacitor": 4.5, "reactor": 4.5, "gun_velocity": 4.5, "lightning_chain": 4, "jets": 3.5,
    "sensors": 3, "mortar_reload": 3, "mortar_velocity": 2.5, "mortar_splash": 2.5, "laser_pierce": 2,
}


def wrap(a):
    return (a + math.pi) % TAU - math.pi


def dist2(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def unit(dx, dy):
    d = math.hypot(dx, dy)
    if d < 1e-9:
        return 0.0, 0.0
    return dx / d, dy / d


class Bot:
    name = "survivor"

    def __init__(self, hello):
        self.cfg = hello["config"]
        self.rng = random.Random(int(hello.get("seed", 0)))
        mech = self.cfg["mech"]
        self.map = None
        try:
            self.map = WorldMap(hello["map"]["dir"], mech.get("step_up", 1.2), mech.get("height", 9))
        except Exception as e:  # the bot still plays without the map, just worse
            mf.log("map load failed:", repr(e))
        W = self.cfg["weapons"]
        self.gun_range = W["gun"]["range"]
        self.loot_r = self.cfg["loot"]["pickup_radius"]
        self.vis = self.cfg["vision"]
        self.known = {}          # id -> dict(pos, vel, t, hp, max_hp, weapons, charging)
        self.dead = set()
        self.threat_bearing = None
        self.threat_t = -99
        self.sound_bearing = None
        self.sound_t = -99
        self.last_damage_t = -99
        self.strafe_sign = 1
        self.strafe_until = 0
        self.path = []
        self.path_goal = None
        self.path_t = -99
        self.searching = False
        self.stuck_n = 0
        self.unstick_until = -1
        self.unstick_dir = (0.0, 0.0)
        self.last_move = (0.0, 0.0)
        self.cover = None
        self.cover_t = -99
        self.mode = "start"
        self.scan_phase = self.rng.random() * TAU
        self.retreating = False
        self.boosting = False
        self.bad_crates = {}
        self.bad_targets = {}
        self.goal_item = None
        self.prev_pos = None
        self.hold_since = 0.0
        self.last_target = None

    def lob_ready(self, kinds, weapons):
        return "mortar" in kinds and weapons[kinds.index("mortar")]["cooldown"] <= 0.3

    # ------------------------------------------------------------------ helpers
    def los(self, a, b):
        return True if self.map is None else self.map.los(a, b)

    def inside_wall(self, x, y):
        return False if self.map is None else self.map.is_wall(x, y)

    def zone_goal_circle(self, obs):
        z = obs["zone"]
        t = obs["t"]
        # Once the next shrink is near, the next circle is the one that matters.
        if t >= z["shrink_start"] - 25:
            return z["next_center"], z["next_radius"]
        return z["center"], z["radius"]

    def safe_point(self, obs, me_xy):
        c, r = self.zone_goal_circle(obs)
        margin = min(45.0, r * 0.45)
        d = dist2(me_xy, c)
        if d <= r - margin:
            return None
        ux, uy = unit(me_xy[0] - c[0], me_xy[1] - c[1])
        rr = max(0.0, r - margin)
        return (c[0] + ux * rr, c[1] + uy * rr)

    def in_circle(self, p, obs, slack=0.0):
        c, r = self.zone_goal_circle(obs)
        return dist2(p, c) <= r - slack

    # ------------------------------------------------------------------ main
    def act(self, obs):
        t0 = time.perf_counter()
        t = obs["t"]
        me = obs["self"]
        pos = me["pos"]
        xy = (pos[0], pos[1])
        eye = (pos[0], pos[1], pos[2] + me["stats"].get("eye_height", 8))
        muzzle = (pos[0], pos[1], pos[2] + me["stats"].get("muzzle_height", 7))
        hp_frac = me["hp"] / max(1.0, me["max_hp"])
        act = {"tick": obs["tick"]}
        dbg = []

        # ---- events
        for ev in obs["events"]:
            typ = ev.get("type")
            if typ == "damaged":
                if ev.get("weapon") != "zone":
                    self.last_damage_t = t
                    if ev.get("bearing") is not None:
                        self.threat_bearing = ev["bearing"]
                        self.threat_t = t
            elif typ == "kill":
                self.dead.add(ev["target"])
                self.known.pop(ev["target"], None)
            elif typ == "take_failed":
                self.bad_targets[ev["id"]] = t + 10
        for s in obs["sounds"]:
            if s["kind"] in ("gun", "laser", "laser_charge", "lightning", "mortar", "step", "boost", "jet", "reload"):
                if s["dist"] != "far" or self.sound_bearing is None or t - self.sound_t > 2:
                    self.sound_bearing = s["bearing"]
                    self.sound_t = t - s.get("age", 0)

        # ---- offer
        off = obs.get("offer")
        if off:
            held = {w["kind"] for w in me["weapons"] if w}
            best, bi = -1e9, 0
            for i, ch in enumerate(off["choices"]):
                v = UPGRADE_PRIORITY.get(ch["id"], 1)
                if P.get("offense") and ch["id"] in ("gun_rate", "gun_damage", "laser_charge", "laser_damage", "lightning_efficiency"):
                    v += 4
                if ch["id"].startswith(("laser", "lightning", "mortar", "gun")):
                    k = ch["id"].split("_")[0]
                    if k not in held:
                        v -= 4
                if v > best:
                    best, bi = v, i
            act["pick"] = bi

        # ---- world knowledge
        enemies, npcs = [], []
        for m in obs["mechs"]:
            if m["kind"] == "mech":
                if m["id"] in self.dead:
                    continue
                enemies.append(m)
                self.known[m["id"]] = {"pos": m["pos"], "vel": m["vel"], "t": t, "hp": m.get("hp", 1400),
                                       "max_hp": m.get("max_hp", 1400), "weapons": m.get("weapons", [])}
            else:
                npcs.append(m)
        for k in [k for k, v in self.known.items() if t - v["t"] > 12]:
            del self.known[k]
        seen_ids = {m["id"] for m in enemies}
        threats = [v["pos"] for k, v in self.known.items()]

        weapons = me["weapons"]
        kinds = [w["kind"] if w else None for w in weapons]
        has = set(k for k in kinds if k)

        # ---- loadout: take pickups in reach
        near_enemy = min((dist2(xy, m["pos"]) for m in enemies), default=1e9)
        self.loadout_take(act, obs, me, kinds, near_enemy, t)

        # ---- choose target (enemy mech)
        target, tlos = self.choose_target(enemies, muzzle, xy)

        # ---- decide mode
        n_close = sum(1 for m in enemies if dist2(xy, m["pos"]) < 160)
        recent_hit = t - self.last_damage_t < 3
        if self.retreating:
            if hp_frac > P["resume_hp"] or (target and dist2(xy, target["pos"]) < 28):
                self.retreating = False
        else:
            if (hp_frac < P["retreat_hp"] and (enemies or recent_hit)) or (P["crowd_retreat"] and n_close >= 3 and hp_frac < 0.75):
                if not (target and target.get("hp", 1e9) < 0.5 * me["hp"] and dist2(xy, target["pos"]) < 120):
                    self.retreating = True
        zone_pt = self.safe_point(obs, xy)
        urgent_zone = (not me["in_zone"]) or (zone_pt is not None and obs["zone"]["shrink_start"] - t < 12)

        move = (0.0, 0.0)
        boost = False
        goal = None
        aim_target = None   # 3D point to look at
        strafe = False

        if urgent_zone and zone_pt is not None:
            self.mode = "zone"
            goal = zone_pt
            boost = not me["in_zone"] or dist2(xy, zone_pt) > 80
        elif not me["in_zone"]:
            self.mode = "zone"
            c = obs["zone"]["center"]
            goal = (c[0], c[1])
            boost = True
        elif self.retreating:
            self.mode = "retreat"
            cv = self.find_cover(obs, xy, threats, t)
            goal = cv if cv else None
            if goal is None and threats:
                # run directly away from the threats' centroid, but toward the zone
                cx = sum(p[0] for p in threats) / len(threats)
                cy = sum(p[1] for p in threats) / len(threats)
                ux, uy = unit(xy[0] - cx, xy[1] - cy)
                c, r = self.zone_goal_circle(obs)
                zx, zy = unit(c[0] - xy[0], c[1] - xy[1])
                goal = (xy[0] + (ux + 0.5 * zx) * 40, xy[1] + (uy + 0.5 * zy) * 40)
            boost = me["energy"] > 25 and hp_frac < 0.5
        elif target is not None and not tlos and not self.lob_ready(kinds, weapons):
            # we know where it is but can't shoot it
            if P["no_los"] == "hold" and t - self.hold_since < 6:
                self.mode = "ambush"
                goal = None
            elif hp_frac >= P["chase_hp"]:
                self.mode = "chase"
                goal = (target["pos"][0], target["pos"][1])
            else:
                self.mode = "ambush"
        elif target is not None:
            self.mode = "fight"
            strafe = True
        else:
            self.mode = "roam"
            goal = self.pick_loot_goal(obs, xy, me, kinds, hp_frac, t, npcs)
            if goal is None and zone_pt is None and P.get("seek"):
                goal = self.seek_point(obs, xy, t)
                if goal is not None:
                    self.mode = "seek"
            if goal is None:
                goal = zone_pt or self.roam_point(obs, xy, t)

        if self.mode != "ambush":
            self.hold_since = t
        # ---- movement
        ducking = False
        if strafe and target is not None and P.get("peek") and self.weapons_down(me, xy, target):
            cv = self.find_cover(obs, xy, [m["pos"] for m in enemies], t, max_r=26)
            if cv is not None:
                ducking = True
                self.mode = "duck"
                move = self.follow(xy, cv, t, t0)
                if dist2(xy, cv) < 2.5:
                    move = (0.0, 0.0)
                boost = me["energy"] > 30
        if ducking:
            pass
        elif strafe and target is not None:
            move, boost = self.fight_move(obs, me, xy, target, has, t)
        elif goal is not None:
            move = self.follow(xy, goal, t, t0)
            if self.mode == "retreat" and dist2(xy, goal) < 3:
                move = (0.0, 0.0)
        # dodge laser lines and mortar shells
        dodge = self.dodge(obs, me, xy, enemies)
        if dodge is not None:
            move = dodge
            boost = me["energy"] > 15
        # stuck handling
        move, jump, jetting = self.unstick(me, xy, move, t)
        if jetting:
            boost = True
        # zone safety: never walk further out of the safe circle when it's shrinking
        self.last_move = move
        act["move"] = [round(move[0], 3), round(move[1], 3)]
        if boost and me["energy"] < 12 and me["grounded"] and not jetting:
            boost = False
        act["boost"] = bool(boost)
        act["jump"] = bool(jump)

        # ---- aim and fire
        fire = [False, False]
        aim = None
        shoot_obj = None
        lob = False
        if target is not None:
            aim, shoot_obj = self.aim_at_actor(me, muzzle, target, kinds), target
            mi = kinds.index("mortar") if "mortar" in kinds else -1
            if mi >= 0 and weapons[mi]["cooldown"] <= 0.3:
                g0 = weapons[0] if kinds[0] == "gun" else (weapons[1] if kinds[1] == "gun" else None)
                gun_busy = g0 is None or g0.get("reload", 0) > 0 or g0.get("ammo", 1) == 0
                if not tlos or (gun_busy and "laser" not in kinds):
                    ma = self.mortar_aim(me, pos, target, weapons[mi]["stats"], high=not tlos)
                    if ma is not None:
                        aim, lob = ma, True
        else:
            # NPC or crate we are farming
            obj = self.farm_target(obs, xy, muzzle, npcs, enemies, hp_frac)
            if obj is not None:
                aim, shoot_obj = self.aim_at_actor(me, muzzle, obj, kinds), obj
        if aim is None:
            aim = self.idle_aim(me, xy, t, move)
        act["aim"] = [round(aim[0], 4), round(max(-0.69, min(1.29, aim[1])), 4)]

        if shoot_obj is not None:
            fire = self.fire_decision(obs, me, muzzle, shoot_obj, kinds, aim, lob)
        else:
            # keep a charging laser charging only briefly, then let it go
            for i, w in enumerate(weapons):
                if w and w["kind"] == "laser" and w.get("charge", 0) > 0:
                    fire[i] = False
        act["fire"] = fire

        # reload while safe
        for w in weapons:
            if w and w["kind"] == "gun" and w.get("reload", 0) == 0 and w.get("ammo", 12) < w["stats"]["mag"] * 0.5 and shoot_obj is None:
                act["reload"] = True

        mf.draw.label(f"{self.mode} hp{me['hp']:.0f} e{me['energy']:.0f} {'T%d' % target['id'] if target else ''}")
        if goal is not None:
            mf.draw.point([goal[0], goal[1], pos[2] + 1], "#40c0ff", 6)
        return act

    # ------------------------------------------------------------------ targets
    def choose_target(self, enemies, muzzle, xy):
        best, bs, blos = None, 1e18, False
        for m in enemies:
            p = m["pos"]
            d = dist2(xy, p)
            c = (p[0], p[1], p[2] + 4.5)
            l = d < 300 and self.los(muzzle, c)
            hp = m.get("hp", 1400)
            if P.get("skill"):
                s = d * 1.0 + hp * P.get("focus", 0.12) + (0 if l else 250) - (40 if m["id"] == self.last_target else 0)
            else:
                s = d * 1.0 + hp * 0.06 + (0 if l else 250)
            if s < bs:
                best, bs, blos = m, s, l
        if best is not None:
            self.last_target = best["id"]
        return best, blos

    def farm_target(self, obs, xy, muzzle, npcs, enemies, hp_frac):
        if enemies:
            return None
        best, bs = None, 1e18
        if hp_frac > P["farm_hp"]:
            for n in npcs:
                d = dist2(xy, n["pos"])
                if d < 150:
                    c = (n["pos"][0], n["pos"][1], n["pos"][2] + 1.75)
                    if self.los(muzzle, c) and d < bs:
                        best, bs = {"id": n["id"], "pos": n["pos"], "vel": n["vel"], "zoff": 1.75, "npc": True}, d
        if best is None:
            for c in obs["crates"]:
                d = dist2(xy, c["pos"])
                if d < 110 and self.bad_crates.get(c["id"], 0) < 60:
                    cc = (c["pos"][0], c["pos"][1], c["pos"][2] + 1.2)
                    if self.los(muzzle, cc) and d < bs:
                        best, bs = {"id": c["id"], "pos": c["pos"], "vel": [0, 0, 0], "zoff": 1.2, "crate": True}, d
            if best is not None:
                self.bad_crates[best["id"]] = self.bad_crates.get(best["id"], 0) + 1
        return best

    def aim_at_actor(self, me, muzzle, tgt, kinds):
        p, v = tgt["pos"], tgt.get("vel", [0, 0, 0])
        zoff = tgt.get("zoff", 4.5)
        speed = me["weapons"][0]["stats"]["speed"] if me["weapons"][0] and me["weapons"][0]["kind"] == "gun" else None
        # primary aim mode: gun lead if we have a gun, else laser/lightning direct aim
        tx, ty, tz = p[0], p[1], p[2] + zoff
        if speed:
            tt = 0.0
            for _ in range(3):
                px = p[0] + v[0] * tt
                py = p[1] + v[1] * tt
                d = math.hypot(px - muzzle[0], py - muzzle[1])
                tt = max(0.0, (d - 3.0) / speed) + 0.03
            tx, ty = p[0] + v[0] * tt, p[1] + v[1] * tt
            vz = v[2] if len(v) > 2 else 0
            tz = p[2] + zoff + max(-6, min(6, vz * tt))
        else:
            tx, ty = p[0] + v[0] * 0.08, p[1] + v[1] * 0.08
        yaw = math.atan2(ty - muzzle[1], tx - muzzle[0])
        dh = math.hypot(tx - muzzle[0], ty - muzzle[1])
        pitch = math.atan2(tz - muzzle[2], max(1e-3, dh))
        return (yaw, pitch)

    def mortar_aim(self, me, pos, tgt, st, high):
        v, g = st["speed"], st["gravity"]
        h0 = me["stats"].get("height", 9) + 0.5
        p, tv = tgt["pos"], tgt.get("vel", [0, 0, 0])
        tt = 1.5
        for _ in range(3):
            tx, ty = p[0] + tv[0] * tt, p[1] + tv[1] * tt
            d = math.hypot(tx - pos[0], ty - pos[1]) - 3.0
            dz = (p[2] + 1.0) - (pos[2] + h0)
            disc = v ** 4 - g * (g * d * d + 2 * dz * v * v)
            if disc < 0 or d < 20:
                return None
            r = math.sqrt(disc)
            th = math.atan2(v * v + r, g * d) if high else math.atan2(v * v - r, g * d)
            tt = d / max(1e-3, v * math.cos(th))
        if th > me["stats"].get("max_pitch", 1.3) - 0.02:
            return None
        return (math.atan2(ty - pos[1], tx - pos[0]), th)

    def fire_decision(self, obs, me, muzzle, tgt, kinds, aim, lob=False):
        fire = [False, False]
        p = tgt["pos"]
        zoff = tgt.get("zoff", 4.5)
        c = (p[0], p[1], p[2] + zoff)
        d = math.hypot(c[0] - muzzle[0], c[1] - muzzle[1])
        los = self.los(muzzle, c)
        ay, ap = me["aim"]
        yaw_err = abs(wrap(ay - aim[0]))
        pitch_err = abs(ap - aim[1])
        rad = 2.5 if not tgt.get("npc") and not tgt.get("crate") else 1.5
        lateral = d * math.sin(min(yaw_err, 1.5)) + d * math.sin(min(pitch_err, 1.0)) * 0.6
        aimed = lateral < rad * 0.9
        true_yaw = math.atan2(c[1] - muzzle[1], c[0] - muzzle[0])
        for i, w in enumerate(me["weapons"]):
            if not w:
                continue
            k, st = w["kind"], w["stats"]
            if k == "gun":
                if not lob and los and d < st["range"] * 0.97 and aimed and w.get("reload", 0) == 0:
                    fire[i] = True
            elif k == "laser":
                ch = w.get("charge", 0)
                if lob and ch == 0:
                    continue
                full = st["charge"] * 0.97
                # aim error against the laser's own (no-lead) line
                lerr = abs(wrap(ay - true_yaw)) * d
                if ch > 0:
                    # charging: release when full and on target, otherwise keep holding
                    if ch >= full and los and lerr < rad * 0.8 and d < st["range"]:
                        fire[i] = False
                    elif ch >= st["charge"] + st.get("hold_s", 2) * 0.8:
                        fire[i] = False
                    else:
                        fire[i] = True
                elif w["cooldown"] <= 0 and los and d < st["range"] * 0.95:
                    fire[i] = True  # start charging
            elif k == "lightning":
                cone = math.radians(st.get("cone_deg", 30)) * 0.8
                if d < st["range"] * 0.95 and los and abs(wrap(ay - true_yaw)) < cone and me["energy"] > st.get("energy", 3) + 4:
                    fire[i] = True
            elif k == "mortar":
                if lob and 25 < d < 230 and w["cooldown"] <= 0 and yaw_err < 0.03 and pitch_err < 0.03:
                    fire[i] = True
        return fire

    # ------------------------------------------------------------------ movement
    def fight_move(self, obs, me, xy, target, has, t):
        p = target["pos"]
        d = dist2(xy, p)
        ew = target.get("weapons", []) or []
        if "lightning" in has and (target.get("hp", 1400) < me["hp"] * 1.3 or "laser" in ew):
            want = 28
        elif "laser" in has:
            want = P["want_laser"] + (20 if "lightning" in ew else 0)
        elif "gun" in has:
            want = P["want_gun_vs_laser"] if "laser" in ew else P["want_gun"]
        else:
            want = 60
        if t > self.strafe_until:
            self.strafe_sign = -self.strafe_sign if self.rng.random() < 0.75 else self.strafe_sign
            self.strafe_until = t + self.rng.uniform(0.45, 1.3)
        ux, uy = unit(p[0] - xy[0], p[1] - xy[1])
        px, py = -uy * self.strafe_sign, ux * self.strafe_sign
        radial = max(-1.0, min(1.0, (d - want) / 25.0))
        mx, my = px + ux * radial, py + uy * radial
        # keep inside the zone
        c, r = self.zone_goal_circle(obs)
        dz = dist2(xy, c)
        if dz > r - 25:
            zx, zy = unit(c[0] - xy[0], c[1] - xy[1])
            mx += zx * 1.2
            my += zy * 1.2
        # don't strafe into walls: try the other direction
        if self.map is not None:
            ax, ay = unit(mx, my)
            if self.map.is_wall(xy[0] + ax * 5, xy[1] + ay * 5):
                self.strafe_sign = -self.strafe_sign
                px, py = -px, -py
                mx, my = px + ux * radial, py + uy * radial
        if P.get("skill"):
            grab = self.fight_grab(obs, me, xy)
            if grab is not None:
                gx, gy = unit(grab[0] - xy[0], grab[1] - xy[1])
                mx, my = mx * 0.4 + gx, my * 0.4 + gy
        mx, my = unit(mx, my)
        e = me["energy"]
        lo, hi = (40, 75) if (P.get("skill") and "lightning" in has) else (20, 55)
        if self.boosting:
            if e < lo:
                self.boosting = False
        elif e > hi:
            self.boosting = True
        return (mx, my), self.boosting

    def fight_grab(self, obs, me, xy):
        best, bd = None, 1e9
        myz = me["pos"][2]
        for c in obs["cubes"]:
            d = dist2(xy, c["pos"])
            if d < P.get("grab_r", 30) and d < bd and abs(c["pos"][2] - myz) < 4 and self.in_circle(c["pos"], obs, 5):
                best, bd = c["pos"], d
        kinds = [w["kind"] if w else None for w in me["weapons"]]
        held = [WEAPON_VALUE.get(k, 0) if k else -1 for k in kinds]
        for pk in obs["pickups"]:
            if pk["kind"] in kinds:
                continue
            gain = WEAPON_VALUE.get(pk["kind"], 0) - min(held)
            d = dist2(xy, pk["pos"])
            if gain > 0.3 and d < P.get("grab_r", 30) + 5 and d - 15 < bd and abs(pk["pos"][2] - myz) < 4:
                best, bd = pk["pos"], d - 15
        return best

    def follow(self, xy, goal, t, t0):
        m = self.map
        if m is None:
            return unit(goal[0] - xy[0], goal[1] - xy[1])
        need = (self.path_goal is None or dist2(goal, self.path_goal) > 12 or t - self.path_t > 5 or not self.path)
        if need and not self.searching:
            m.start_search(xy, goal)
            self.searching = True
            self.path_goal = goal
            self.path_t = t
        if self.searching:
            budget = max(0.001, 0.009 - (time.perf_counter() - t0))
            S = m.step_search(budget)
            if S["done"]:
                self.searching = False
                self.path = list(S["path"][1:]) or [goal]
                if len(S["path"]) <= 1:
                    self.path = [goal]
        if not self.path:
            return unit(goal[0] - xy[0], goal[1] - xy[1])
        while len(self.path) > 1 and (dist2(xy, self.path[0]) < 5 or m.walk_clear(xy, self.path[1])):
            self.path.pop(0)
        wp = self.path[0]
        if len(self.path) == 1 and dist2(xy, wp) < 4:
            wp = goal
        if dist2(xy, wp) < 1.5:
            return (0.0, 0.0)
        return unit(wp[0] - xy[0], wp[1] - xy[1])

    def unstick(self, me, xy, move, t):
        jump = False
        jetting = False
        spd = math.hypot(me["vel"][0], me["vel"][1])
        want = math.hypot(*move)
        if t < self.unstick_until:
            move = self.unstick_dir
            if me["grounded"]:
                jump = True
            elif me["energy"] > 8:
                jetting = True
            return move, jump, jetting
        if want > 0.5 and spd < 2.0 and me["grounded"]:
            self.stuck_n += 1
        else:
            self.stuck_n = max(0, self.stuck_n - 1)
        if self.stuck_n >= 6:
            self.stuck_n = 0
            self.path = []
            self.path_goal = None
            if self.map is not None and want > 0.5:
                self.map.block_near(xy[0] + move[0] * 4, xy[1] + move[1] * 4)
            # first try climbing over in the desired direction; if energy is low, sidestep
            if me["energy"] > 35:
                self.unstick_dir = move
            else:
                a = math.atan2(move[1], move[0]) + self.rng.choice((-1, 1)) * self.rng.uniform(1.2, 2.0)
                self.unstick_dir = (math.cos(a), math.sin(a))
            self.unstick_until = t + 1.4
            jump = True
        return move, jump, jetting

    def dodge(self, obs, me, xy, enemies):
        pos = me["pos"]
        # lasers being charged at us
        for m in enemies:
            if m.get("charging"):
                p = m["pos"]
                ay = m["aim"][0]
                to_me = math.atan2(xy[1] - p[1], xy[0] - p[0])
                d = dist2(xy, p)
                if abs(wrap(ay - to_me)) * d < 6 and d < 300:
                    ux, uy = unit(xy[0] - p[0], xy[1] - p[1])
                    s = self.strafe_sign
                    return (-uy * s, ux * s)
        # mortar shells
        if self.map is None:
            return None
        g = self.cfg["weapons"]["mortar"]["gravity"]
        rad = self.cfg["weapons"]["mortar"]["splash_radius"] + 4
        for pr in obs["projectiles"]:
            if pr["kind"] != "mortar":
                continue
            px, py, pz = pr["pos"]
            vx, vy, vz = pr["vel"]
            land = None
            for i in range(1, 70):
                tt = i * 0.1
                x, y, z = px + vx * tt, py + vy * tt, pz + vz * tt - 0.5 * g * tt * tt
                if z <= self.map.top_at(x, y) or self.map.solid_at(x, y, z):
                    land = (x, y, tt)
                    break
            if land and dist2(xy, land) < rad + 3 and land[2] < 4:
                ux, uy = unit(xy[0] - land[0], xy[1] - land[1])
                if ux == 0 and uy == 0:
                    ux, uy = 1.0, 0.0
                return (ux, uy)
        # npc / gun bullets heading straight at us: sidestep
        return None

    def weapons_down(self, me, xy, target):
        """True when nothing we hold can hurt the target for at least ~0.8 s."""
        d = dist2(xy, target["pos"])
        any_w = False
        for w in me["weapons"]:
            if not w:
                continue
            any_w = True
            k = w["kind"]
            if k == "gun":
                if w.get("reload", 0) < 0.8 and w.get("ammo", 1) > 0:
                    return False
            elif k == "laser":
                if w["cooldown"] < 0.8 or w.get("charge", 0) > 0:
                    return False
            elif k == "lightning":
                if d < w["stats"]["range"] and me["energy"] > 10:
                    return False
            elif k == "mortar":
                if w["cooldown"] < 0.8 and 25 < d < 220:
                    return False
        return any_w

    def find_cover(self, obs, xy, threats, t, max_r=58):
        if self.map is None or not threats:
            return None
        if self.cover is not None and t - self.cover_t < 0.8:
            return self.cover
        m = self.map
        best, bs = None, 1e18
        tp = [(p[0], p[1], p[2] + 8) for p in threats[:6]]
        for r in [r for r in (14, 26, 40, 58) if r <= max_r]:
            for k in range(14):
                a = k * TAU / 14 + r * 0.1
                x, y = xy[0] + math.cos(a) * r, xy[1] + math.sin(a) * r
                if m.is_wall(x, y) or not self.in_circle((x, y), obs, 10):
                    continue
                gz = m.ground(x, y)
                seen = 0
                for e in tp:
                    if m.los(e, (x, y, gz + 7)):
                        seen += 1
                if seen:
                    continue
                cost = r
                if not m.walk_clear(xy, (x, y)):
                    cost += 35
                # prefer points away from threats
                dmin = min(dist2((x, y), e) for e in tp)
                cost -= min(dmin, 120) * 0.15
                if cost < bs:
                    best, bs = (x, y), cost
            if best is not None and r >= 26:
                break
        self.cover = best
        self.cover_t = t
        return best

    def pick_loot_goal(self, obs, xy, me, kinds, hp_frac, t, npcs):
        best, bs = None, 1e18
        myz = me["pos"][2]

        def ok(p):
            return self.in_circle(p, obs, 15) and abs(p[2] - myz) < 6 and not self.inside_wall(p[0], p[1])

        held_vals = [WEAPON_VALUE.get(k, 0) if k else -1 for k in kinds]
        for pk in obs["pickups"]:
            if self.bad_targets.get(pk["id"], 0) > t:
                continue
            v = WEAPON_VALUE.get(pk["kind"], 0)
            if pk["kind"] in kinds:
                continue
            gain = v - min(held_vals)
            if gain <= 0.3:
                continue
            d = dist2(xy, pk["pos"])
            if d < 240 and ok(pk["pos"]):
                s = d - 70 * gain
                if s < bs:
                    best, bs = pk["pos"], s
        for c in obs["cubes"]:
            d = dist2(xy, c["pos"])
            if d < P.get("loot_r", 170) and ok(c["pos"]):
                s = d - 50
                if s < bs:
                    best, bs = c["pos"], s
        # drop: go if close enough and nobody seen
        for dr in obs.get("drops", []):
            d = dist2(xy, dr["pos"])
            if d < 220 and dr["lands_in"] < d / 11 + 5 and self.in_circle(dr["pos"], obs, 0):
                s = d - 80
                if s < bs:
                    best, bs = dr["pos"], s
        # walk toward crates and NPCs that are visible but too far to shoot well
        if best is None:
            for c in obs["crates"]:
                d = dist2(xy, c["pos"])
                if 70 < d < P.get("loot_r", 200) and ok(c["pos"]):
                    if d < bs:
                        best, bs = c["pos"], d
        if best is None and hp_frac > P.get("npc_hp", 0.6):
            for n in npcs:
                d = dist2(xy, n["pos"])
                if 90 < d < P.get("loot_r", 200) and self.in_circle(n["pos"], obs, 15):
                    if d < bs:
                        best, bs = n["pos"], d
        return (best[0], best[1]) if best is not None else None

    def seek_point(self, obs, xy, t):
        best, bt = None, 1e9
        for k, v in self.known.items():
            if t - v["t"] < bt:
                best, bt = v["pos"], t - v["t"]
        if best is not None and bt < 12 and self.in_circle(best, obs, 10):
            return (best[0], best[1])
        if self.sound_bearing is not None and t - self.sound_t < 4:
            p = (xy[0] + math.cos(self.sound_bearing) * 70, xy[1] + math.sin(self.sound_bearing) * 70)
            if self.in_circle(p, obs, 10):
                return p
        return None

    def roam_point(self, obs, xy, t):
        c, r = self.zone_goal_circle(obs)
        # wander inside the inner part of the safe circle; change every 20 s
        if self.goal_item is None or t > self.goal_item[2] or not self.in_circle(self.goal_item[:2], obs, 20):
            for _ in range(20):
                a = self.rng.random() * TAU
                rr = self.rng.random() ** 0.5 * r * 0.55
                p = (c[0] + math.cos(a) * rr, c[1] + math.sin(a) * rr)
                if not self.inside_wall(*p):
                    break
            self.goal_item = (p[0], p[1], t + 20)
        return self.goal_item[:2]

    def idle_aim(self, me, xy, t, move):
        # look toward the most recent threat cue, else sweep around the direction of travel
        if t - self.threat_t < 3 and self.threat_bearing is not None:
            return (self.threat_bearing, 0.0)
        best, bt = None, 1e9
        for k, v in self.known.items():
            age = t - v["t"]
            if age < bt:
                best, bt = v, age
        if best is not None and bt < 8:
            p = best["pos"]
            return (math.atan2(p[1] - xy[1], p[0] - xy[0]), 0.0)
        if t - self.sound_t < 2.5 and self.sound_bearing is not None:
            return (self.sound_bearing, 0.0)
        base = math.atan2(move[1], move[0]) if math.hypot(*move) > 0.1 else me["aim"][0]
        self.scan_phase += 0.25
        return (base + math.sin(self.scan_phase) * 1.4, 0.0)

    def loadout_take(self, act, obs, me, kinds, near_enemy, t):
        pos = me["pos"]
        best = None
        held_vals = [WEAPON_VALUE.get(k, 0) if k else -1 for k in kinds]
        for pk in obs["pickups"]:
            if self.bad_targets.get(pk["id"], 0) > t:
                continue
            p = pk["pos"]
            if dist2(pos, p) > self.loot_r * 0.95 or abs(p[2] - pos[2]) > 3.8:
                continue
            k = pk["kind"]
            if k in kinds:
                continue
            v = WEAPON_VALUE.get(k, 0)
            if None in kinds:
                cand = (v, pk["id"], None)
            else:
                slot = 0 if held_vals[0] <= held_vals[1] else 1
                w = me["weapons"][slot]
                if v - held_vals[slot] <= 0.3 or near_enemy < (25 if P.get("skill") else 60):
                    continue
                if w and w["kind"] == "laser" and w.get("charge", 0) > 0:
                    continue
                cand = (v, pk["id"], slot)
            if best is None or cand[0] > best[0]:
                best = cand
        if best is not None:
            act["take"] = best[1]
            if best[2] is not None:
                act["drop"] = best[2]


if __name__ == "__main__":
    mf.run(Bot)
