"""灯塔救援游戏核心逻辑。

包含海况（潮位/迷雾）、求救信号、救援任务、救生艇（容量/燃料/疲劳）、
撤销与 JSON 存档。所有状态变更操作均为幂等语义。
"""
from __future__ import annotations

import copy
import json
import random

WIDTH = 8
HEIGHT = 8
TIDE_LEVELS = (0, 1, 2)  # 0=低潮 1=平潮 2=高潮
SHOAL_MIN_TIDE = 1       # 浅滩通行所需的最低潮位（含边界，潮位 >= 1 可通行）
FOG_CHANCE = 0.5         # 每回合迷雾事件概率
FOG_DRIFT = 1            # 迷雾使进行中航线偏移的格数
FATIGUE_PER_SAIL = 10    # 出航一次增加的疲劳
FATIGUE_PER_REST = -20   # 休息回合恢复的疲劳（负值，绝不增加）
MAX_FATIGUE = 100
DEFAULT_SEED = 20260929

MISSION_PENDING = "pending"
MISSION_ACTIVE = "active"
MISSION_DONE = "done"
MISSION_CANCELLED = "cancelled"


class Signal:
    """求救信号。"""

    def __init__(self, sid, x, y, victims=1, active=True):
        self.sid = sid
        self.x = x
        self.y = y
        self.victims = victims
        self.active = active

    def to_dict(self):
        return {
            "sid": self.sid,
            "x": self.x,
            "y": self.y,
            "victims": self.victims,
            "active": self.active,
        }

    @classmethod
    def from_dict(cls, data):
        return cls(
            data["sid"], data["x"], data["y"],
            victims=data.get("victims", 1),
            active=data.get("active", True),
        )


class Mission:
    """由求救信号生成的救援任务。"""

    def __init__(self, mid, signal_id, victims, route=None,
                 status=MISSION_PENDING, fuel_spent=0, boat=None):
        self.mid = mid
        self.signal_id = signal_id
        self.victims = victims
        self.route = route or []
        self.status = status
        self.fuel_spent = fuel_spent
        self.boat = boat

    def to_dict(self):
        return {
            "mid": self.mid,
            "signal_id": self.signal_id,
            "victims": self.victims,
            "route": [list(cell) for cell in self.route],
            "status": self.status,
            "fuel_spent": self.fuel_spent,
            "boat": self.boat,
        }

    @classmethod
    def from_dict(cls, data):
        return cls(
            data["mid"], data["signal_id"], data["victims"],
            route=[list(cell) for cell in data.get("route", [])],
            status=data.get("status", MISSION_PENDING),
            fuel_spent=data.get("fuel_spent", 0),
            boat=data.get("boat"),
        )


class Boat:
    """救生艇。"""

    def __init__(self, name, x=0, y=0, capacity=4, fuel=30, max_fuel=30,
                 fatigue=0, rescued=0):
        self.name = name
        self.x = x
        self.y = y
        self.capacity = capacity
        self.fuel = fuel
        self.max_fuel = max_fuel
        self.fatigue = fatigue
        self.rescued = rescued

    def to_dict(self):
        return {
            "name": self.name,
            "x": self.x,
            "y": self.y,
            "capacity": self.capacity,
            "fuel": self.fuel,
            "max_fuel": self.max_fuel,
            "fatigue": self.fatigue,
            "rescued": self.rescued,
        }

    @classmethod
    def from_dict(cls, data):
        return cls(
            data["name"], data["x"], data["y"],
            capacity=data.get("capacity", 4),
            fuel=data.get("fuel", 30),
            max_fuel=data.get("max_fuel", 30),
            fatigue=data.get("fatigue", 0),
            rescued=data.get("rescued", 0),
        )


class Game:
    """灯塔救援游戏主控。"""

    def __init__(self, seed=DEFAULT_SEED):
        self.seed = seed
        self.rng = random.Random(seed)
        self.turn = 0
        self.tide = TIDE_LEVELS[0]
        self.shoals = {(3, 3), (3, 4), (4, 3)}
        self.base = (0, 0)
        self.boats = {"boat1": Boat("boat1")}
        self.signals = {}
        self.missions = {}
        self.last_error = ""
        self._mission_seq = 0
        self._undo_stack = []

    # ---- 海况 ----

    def in_bounds(self, x, y):
        return 0 <= x < WIDTH and 0 <= y < HEIGHT

    def is_shoal(self, x, y):
        return (x, y) in self.shoals

    def is_passable(self, x, y):
        """浅滩在潮位 >= SHOAL_MIN_TIDE 时可通行（含边界）。"""
        if not self.in_bounds(x, y):
            return False
        if self.is_shoal(x, y):
            return self.tide >= SHOAL_MIN_TIDE
        return True

    def tick(self):
        """推进一个回合：更新潮位，可能触发迷雾事件。"""
        self.turn += 1
        self.tide = TIDE_LEVELS[self.rng.randrange(len(TIDE_LEVELS))]
        if self.rng.random() < FOG_CHANCE:
            self._apply_fog()

    def _apply_fog(self):
        """迷雾只偏移进行中的航线，已完成/已取消的航线不受影响。"""
        for mission in self.missions.values():
            if mission.status != MISSION_ACTIVE:
                continue
            mission.route = [
                [x, min(HEIGHT - 1, max(0, y + FOG_DRIFT))]
                for x, y in mission.route
            ]

    # ---- 信号与任务 ----

    def add_signal(self, sid, x, y, victims=1):
        """登记求救信号；同一 sid 重复登记幂等。"""
        existing = self.signals.get(sid)
        if existing is not None:
            return existing
        signal = Signal(sid, x, y, victims=victims)
        self.signals[sid] = signal
        return signal

    def spawn_mission(self, sid):
        """由信号生成任务；同一信号的未结任务不会重复生成。"""
        signal = self.signals.get(sid)
        if signal is None or not signal.active:
            return None
        for mission in self.missions.values():
            if mission.signal_id == sid and mission.status in (
                MISSION_PENDING, MISSION_ACTIVE,
            ):
                return mission
        self._mission_seq += 1
        mission = Mission("m%d" % self._mission_seq, sid, signal.victims)
        self.missions[mission.mid] = mission
        return mission

    # ---- 出航 / 取消 / 完成 ----

    def _route(self, start, end):
        """L 形航线：先横后纵，不含起点、含终点。"""
        cells = []
        x, y = start
        ex, ey = end
        step = 1 if ex >= x else -1
        while x != ex:
            x += step
            cells.append((x, y))
        step = 1 if ey >= y else -1
        while y != ey:
            y += step
            cells.append((x, y))
        return cells

    def depart(self, boat_name, mid):
        """出航。超载、燃料不足或航线穿越不可通行浅滩时拒绝且状态不变。"""
        boat = self.boats.get(boat_name)
        mission = self.missions.get(mid)
        if boat is None or mission is None:
            self.last_error = "not_found"
            return False
        if mission.status != MISSION_PENDING:
            self.last_error = "bad_status"
            return False
        signal = self.signals.get(mission.signal_id)
        if signal is None or not signal.active:
            self.last_error = "signal_closed"
            return False
        if boat.rescued + mission.victims > boat.capacity:
            self.last_error = "overload"
            return False
        route = self._route((boat.x, boat.y), (signal.x, signal.y))
        if any(not self.is_passable(cx, cy) for cx, cy in route):
            self.last_error = "shoal"
            return False
        cost = len(route)
        if cost > boat.fuel:
            self.last_error = "fuel"
            return False
        self._push_undo()
        boat.fuel -= cost
        boat.x, boat.y = signal.x, signal.y
        boat.fatigue = min(MAX_FATIGUE, boat.fatigue + FATIGUE_PER_SAIL)
        mission.route = [list(cell) for cell in route]
        mission.status = MISSION_ACTIVE
        mission.fuel_spent = cost
        mission.boat = boat_name
        return True

    def cancel_mission(self, mid):
        """取消任务：返还已耗燃料（仅一次）并让船返回基地。幂等。"""
        mission = self.missions.get(mid)
        if mission is None or mission.status not in (
            MISSION_PENDING, MISSION_ACTIVE,
        ):
            return False
        if mission.boat is not None:
            boat = self.boats[mission.boat]
            if mission.fuel_spent:
                boat.fuel = min(boat.max_fuel, boat.fuel + mission.fuel_spent)
                mission.fuel_spent = 0
            boat.x, boat.y = self.base
        mission.status = MISSION_CANCELLED
        return True

    def complete_rescue(self, mid):
        """完成救援：关闭对应信号并计入获救人数。幂等。"""
        mission = self.missions.get(mid)
        if mission is None or mission.status != MISSION_ACTIVE:
            return False
        signal = self.signals[mission.signal_id]
        boat = self.boats[mission.boat]
        boat.rescued += mission.victims
        mission.status = MISSION_DONE
        signal.active = False
        return True

    # ---- 疲劳 ----

    def rest(self, boat_name):
        """休息回合：疲劳只减不增。"""
        boat = self.boats.get(boat_name)
        if boat is None:
            return False
        boat.fatigue = max(0, boat.fatigue + FATIGUE_PER_REST)
        return True

    # ---- 撤销 ----

    def _push_undo(self):
        snapshot = {
            "boats": copy.deepcopy(self.boats),
            "missions": copy.deepcopy(self.missions),
            "signals": copy.deepcopy(self.signals),
        }
        self._undo_stack.append(snapshot)

    def undo(self):
        """撤销最近一次出航，恢复位置/燃料/疲劳与任务状态。空栈时为无操作。"""
        if not self._undo_stack:
            return False
        snapshot = self._undo_stack.pop()
        self.boats = snapshot["boats"]
        self.missions = snapshot["missions"]
        self.signals = snapshot["signals"]
        return True

    # ---- 存档 ----

    def to_dict(self):
        version, state, gauss = self.rng.getstate()
        return {
            "seed": self.seed,
            "turn": self.turn,
            "tide": self.tide,
            "rng_state": [version, list(state), gauss],
            "shoals": [list(cell) for cell in sorted(self.shoals)],
            "boats": {name: boat.to_dict() for name, boat in self.boats.items()},
            "signals": [s.to_dict() for s in self.signals.values()],
            "missions": [m.to_dict() for m in self.missions.values()],
            "mission_seq": self._mission_seq,
        }

    def save(self, path):
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.to_dict(), fh, ensure_ascii=False, indent=2)
        return path

    def load(self, path):
        """读档并恢复随机数状态，保证读档后海况序列不偏移。"""
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        self.seed = data["seed"]
        self.turn = data["turn"]
        self.tide = data["tide"]
        version, state, gauss = data["rng_state"]
        self.rng = random.Random(self.seed)
        self.rng.setstate((version, tuple(state), gauss))
        self.shoals = {tuple(cell) for cell in data["shoals"]}
        self.boats = {
            name: Boat.from_dict(b) for name, b in data["boats"].items()
        }
        self.signals = {s["sid"]: Signal.from_dict(s) for s in data["signals"]}
        self.missions = {m["mid"]: Mission.from_dict(m) for m in data["missions"]}
        self._mission_seq = data["mission_seq"]
        self._undo_stack = []
        return self

    # ---- 结算 ----

    def settle(self):
        return {
            "turns": self.turn,
            "rescued": sum(b.rescued for b in self.boats.values()),
            "missions_completed": sum(
                1 for m in self.missions.values() if m.status == MISSION_DONE
            ),
            "missions_cancelled": sum(
                1 for m in self.missions.values()
                if m.status == MISSION_CANCELLED
            ),
            "fuel_remaining": sum(b.fuel for b in self.boats.values()),
        }

    def report(self):
        s = self.settle()
        return (
            "结算: 回合={turns} 获救={rescued} "
            "完成={missions_completed} 取消={missions_cancelled} "
            "剩余燃料={fuel_remaining}"
        ).format(**s)

    # ---- CMD 命令 ----

    def execute(self, line):
        """执行 CMD 命令行，返回结果文本。"""
        parts = line.split()
        if len(parts) < 2 or parts[0] != "CMD":
            return "ERR 未知命令"
        cmd = parts[1].upper()
        args = parts[2:]
        try:
            if cmd == "TICK":
                self.tick()
                return "OK 潮位=%d" % self.tide
            if cmd == "SIGNAL" and len(args) >= 3:
                victims = int(args[3]) if len(args) > 3 else 1
                sig = self.add_signal(args[0], int(args[1]), int(args[2]), victims)
                return "OK 信号=%s" % sig.sid
            if cmd == "MISSION" and len(args) == 1:
                mission = self.spawn_mission(args[0])
                if mission is None:
                    return "ERR 无有效信号"
                return "OK 任务=%s" % mission.mid
            if cmd == "DEPART" and len(args) == 2:
                if self.depart(args[0], args[1]):
                    return "OK 出航"
                return "ERR %s" % self.last_error
            if cmd == "CANCEL" and len(args) == 1:
                if self.cancel_mission(args[0]):
                    return "OK 已取消"
                return "ERR 无法取消"
            if cmd == "COMPLETE" and len(args) == 1:
                if self.complete_rescue(args[0]):
                    return "OK 救援完成"
                return "ERR 无法完成"
            if cmd == "REST" and len(args) == 1:
                if self.rest(args[0]):
                    return "OK 休息"
                return "ERR 无此船"
            if cmd == "UNDO":
                if self.undo():
                    return "OK 已撤销"
                return "ERR 无可撤销"
            if cmd == "SAVE" and len(args) == 1:
                self.save(args[0])
                return "OK 已存档"
            if cmd == "LOAD" and len(args) == 1:
                self.load(args[0])
                return "OK 已读档"
            if cmd == "SETTLE":
                return self.report()
            if cmd == "PASSABLE" and len(args) == 2:
                ok = self.is_passable(int(args[0]), int(args[1]))
                return "OK %s" % ("可通行" if ok else "不可通行")
        except (ValueError, KeyError, OSError) as exc:
            return "ERR %s" % exc
        return "ERR 未知命令"
