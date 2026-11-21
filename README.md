# Mech Royale bot ("survivor")

Python bot for the Builders Mech Royale track (`mechfight/1`). Standard library only.

## Files

- `bot.py`: the bot (entrypoint)
- `worldmap.py`: map model built at startup from `/arena`: walls, ground height, line of sight, budgeted A* pathing
- `mechfight.py`: the kit's protocol wrapper (unchanged)
- `Dockerfile`: `python:3.13-slim`, nothing installed

## What it does

- **Zone:** moves inside the next circle ~25 s before each shrink, boosts when outside.
- **Loot:** walks to and takes weapons (laser > lightning > gun > mortar, swaps when clearly better), cubes, and supply drops; shoots crates and NPC drones for cubes when no mech is in sight.
- **Fight:** picks the closest/weakest visible mech with line of sight, leads gun shots, charges and releases the laser when on target, fires lightning inside its cone, lobs mortar shells over cover when there is no line of sight. Strafes with random direction changes and holds a range that suits the loadout.
- **Survive:** below ~42% HP (or when 3+ enemies are close) it retreats to a nearby point hidden from all known enemies to regenerate, still facing them so hits land on front armor. Dodges incoming mortar shells and laser lines.
- **Upgrades:** armor, plating and nanites first, then weapon upgrades for held weapons.
- **Time:** about 10 ms per answer at worst (budget 50 ms); the A* planner spreads work over ticks.

## Test locally (from the kit folder)

```sh
python3 check/check.py -- python3 -u /path/to/this/repo/bot.py
docker build -t my-bot /path/to/this/repo
python3 check/check.py --map-dir /arena -- docker run --rm -i --network none --cpus 1 --memory 512m --read-only --tmpfs /tmp:rw,size=64m --user 65534:65534 -v "$PWD/arena:/arena:ro" my-bot
```
