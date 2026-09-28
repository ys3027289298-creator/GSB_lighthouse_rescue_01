"""灯塔救援核心逻辑测试：9 个红态用例 + 浅滩/满载边界 + 幂等性。"""
import os
import tempfile
import unittest

from rescue.core import (
    FATIGUE_PER_SAIL,
    MISSION_ACTIVE,
    MISSION_CANCELLED,
    MISSION_DONE,
    MISSION_PENDING,
    SHOAL_MIN_TIDE,
    Game,
)


def make_game():
    game = Game(seed=7)
    game.tide = 2  # 默认高潮，浅滩用例会显式调整
    return game


class TideShoalBoundaryTest(unittest.TestCase):
    """bug1: 潮位边界错误导致船只穿过浅滩。"""

    def test_shoal_blocked_below_min_tide(self):
        game = make_game()
        game.tide = SHOAL_MIN_TIDE - 1
        self.assertFalse(game.is_passable(3, 3))

    def test_shoal_passable_at_boundary_tide(self):
        game = make_game()
        game.tide = SHOAL_MIN_TIDE
        self.assertTrue(game.is_passable(3, 3))

    def test_depart_through_shoal_rejected_at_low_tide(self):
        game = make_game()
        game.tide = SHOAL_MIN_TIDE - 1
        game.add_signal("s1", 3, 6, victims=1)  # L 形航线必经浅滩 (3,3)
        mission = game.spawn_mission("s1")
        boat = game.boats["boat1"]
        self.assertFalse(game.depart("boat1", mission.mid))
        self.assertEqual(game.last_error, "shoal")
        self.assertEqual((boat.x, boat.y), game.base)
        self.assertEqual(boat.fuel, boat.max_fuel)
        self.assertEqual(mission.status, MISSION_PENDING)

    def test_depart_through_shoal_allowed_at_boundary_tide(self):
        game = make_game()
        game.tide = SHOAL_MIN_TIDE
        game.add_signal("s1", 3, 6, victims=1)
        mission = game.spawn_mission("s1")
        self.assertTrue(game.depart("boat1", mission.mid))


class DuplicateMissionTest(unittest.TestCase):
    """bug2: 同一求救信号重复生成任务。"""

    def test_same_signal_spawns_single_mission(self):
        game = make_game()
        game.add_signal("s1", 5, 0, victims=1)
        first = game.spawn_mission("s1")
        second = game.spawn_mission("s1")
        self.assertIs(first, second)
        self.assertEqual(len(game.missions), 1)

    def test_closed_signal_spawns_no_mission(self):
        game = make_game()
        game.add_signal("s1", 5, 0, victims=1)
        game.signals["s1"].active = False
        self.assertIsNone(game.spawn_mission("s1"))
        self.assertEqual(len(game.missions), 0)


class CapacityBoundaryTest(unittest.TestCase):
    """bug3: 救生艇超载仍可出航。"""

    def test_overload_depart_rejected(self):
        game = make_game()
        boat = game.boats["boat1"]
        game.add_signal("s1", 5, 0, victims=boat.capacity + 1)
        mission = game.spawn_mission("s1")
        self.assertFalse(game.depart("boat1", mission.mid))
        self.assertEqual(game.last_error, "overload")
        self.assertEqual((boat.x, boat.y), game.base)
        self.assertEqual(boat.fuel, boat.max_fuel)
        self.assertEqual(mission.status, MISSION_PENDING)

    def test_full_load_boundary_allowed(self):
        game = make_game()
        boat = game.boats["boat1"]
        game.add_signal("s1", 5, 0, victims=boat.capacity)  # 恰好满载
        mission = game.spawn_mission("s1")
        self.assertTrue(game.depart("boat1", mission.mid))
        self.assertEqual(mission.status, MISSION_ACTIVE)


class FuelRefundTest(unittest.TestCase):
    """bug4: 燃料在取消任务后未返还。"""

    def test_cancel_refunds_fuel_once(self):
        game = make_game()
        game.add_signal("s1", 5, 0, victims=1)
        mission = game.spawn_mission("s1")
        boat = game.boats["boat1"]
        self.assertTrue(game.depart("boat1", mission.mid))
        spent = boat.max_fuel - boat.fuel
        self.assertGreater(spent, 0)
        self.assertTrue(game.cancel_mission(mission.mid))
        self.assertEqual(boat.fuel, boat.max_fuel)
        self.assertEqual(mission.status, MISSION_CANCELLED)
        # 幂等：重复取消不重复返还
        self.assertFalse(game.cancel_mission(mission.mid))
        self.assertEqual(boat.fuel, boat.max_fuel)


class FogEventTest(unittest.TestCase):
    """bug5: 迷雾事件修改了已完成航线。"""

    def test_fog_skips_completed_routes(self):
        game = make_game()
        game.add_signal("s1", 5, 0, victims=1)
        game.add_signal("s2", 6, 0, victims=1)
        done_mission = game.spawn_mission("s1")
        game.boats["boat2"] = type(game.boats["boat1"])("boat2")
        active_mission = game.spawn_mission("s2")
        self.assertTrue(game.depart("boat1", done_mission.mid))
        self.assertTrue(game.depart("boat2", active_mission.mid))
        self.assertTrue(game.complete_rescue(done_mission.mid))
        done_route = [list(c) for c in done_mission.route]
        active_route = [list(c) for c in active_mission.route]
        game._apply_fog()
        self.assertEqual(done_mission.route, done_route)
        self.assertNotEqual(active_mission.route, active_route)


class FatigueTest(unittest.TestCase):
    """bug6: 船员疲劳在休息回合仍增加。"""

    def test_rest_never_increases_fatigue(self):
        game = make_game()
        boat = game.boats["boat1"]
        boat.fatigue = 50
        game.rest("boat1")
        self.assertLess(boat.fatigue, 50)

    def test_rest_at_zero_stays_zero(self):
        game = make_game()
        boat = game.boats["boat1"]
        boat.fatigue = 0
        game.rest("boat1")
        self.assertEqual(boat.fatigue, 0)

    def test_sail_increases_fatigue_once(self):
        game = make_game()
        game.add_signal("s1", 5, 0, victims=1)
        mission = game.spawn_mission("s1")
        boat = game.boats["boat1"]
        game.depart("boat1", mission.mid)
        self.assertEqual(boat.fatigue, FATIGUE_PER_SAIL)


class SignalCloseTest(unittest.TestCase):
    """bug7: 救援完成后信号未关闭。"""

    def test_signal_closed_after_rescue(self):
        game = make_game()
        game.add_signal("s1", 5, 0, victims=2)
        mission = game.spawn_mission("s1")
        game.depart("boat1", mission.mid)
        self.assertTrue(game.complete_rescue(mission.mid))
        self.assertFalse(game.signals["s1"].active)
        self.assertEqual(mission.status, MISSION_DONE)
        self.assertEqual(game.boats["boat1"].rescued, 2)
        # 幂等：重复完成不重复计数
        self.assertFalse(game.complete_rescue(mission.mid))
        self.assertEqual(game.boats["boat1"].rescued, 2)


class UndoTest(unittest.TestCase):
    """bug8: 撤销出航没有恢复位置。"""

    def test_undo_restores_position_fuel_and_status(self):
        game = make_game()
        game.add_signal("s1", 5, 0, victims=1)
        mission = game.spawn_mission("s1")
        boat = game.boats["boat1"]
        game.depart("boat1", mission.mid)
        self.assertEqual((boat.x, boat.y), (5, 0))
        self.assertTrue(game.undo())
        boat = game.boats["boat1"]
        self.assertEqual((boat.x, boat.y), game.base)
        self.assertEqual(boat.fuel, boat.max_fuel)
        self.assertEqual(boat.fatigue, 0)
        self.assertEqual(game.missions[mission.mid].status, MISSION_PENDING)

    def test_undo_on_empty_stack_is_noop(self):
        game = make_game()
        self.assertFalse(game.undo())
        self.assertEqual((game.boats["boat1"].x, game.boats["boat1"].y), game.base)

    def test_double_undo_does_not_corrupt(self):
        game = make_game()
        game.add_signal("s1", 5, 0, victims=1)
        mission = game.spawn_mission("s1")
        game.depart("boat1", mission.mid)
        self.assertTrue(game.undo())
        self.assertFalse(game.undo())
        self.assertEqual((game.boats["boat1"].x, game.boats["boat1"].y), game.base)


class SaveLoadRngTest(unittest.TestCase):
    """bug9: 读档后海况随机数偏移。"""

    def test_load_preserves_sea_state_sequence(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "save.json")
            game = Game(seed=42)
            for _ in range(3):
                game.tick()
            game.save(path)
            expected = []
            for _ in range(5):
                game.tick()
                expected.append(game.tide)

            loaded = Game(seed=999)
            loaded.load(path)
            actual = []
            for _ in range(5):
                loaded.tick()
                actual.append(loaded.tide)
            self.assertEqual(actual, expected)

    def test_save_load_roundtrip_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "save.json")
            game = make_game()
            game.add_signal("s1", 5, 0, victims=2)
            mission = game.spawn_mission("s1")
            game.depart("boat1", mission.mid)
            game.tick()
            game.save(path)

            loaded = Game(seed=1)
            loaded.load(path)
            self.assertEqual(loaded.turn, game.turn)
            self.assertEqual(loaded.tide, game.tide)
            self.assertEqual(
                (loaded.boats["boat1"].x, loaded.boats["boat1"].y), (5, 0)
            )
            self.assertEqual(loaded.missions[mission.mid].status, MISSION_ACTIVE)
            self.assertEqual(loaded.signals["s1"].victims, 2)


class CommandAndSettleTest(unittest.TestCase):
    """CMD 命令与结算格式回归。"""

    def test_cmd_flow_and_settle_format(self):
        game = make_game()
        self.assertEqual(game.execute("CMD SIGNAL s1 5 0 2"), "OK 信号=s1")
        self.assertEqual(game.execute("CMD MISSION s1"), "OK 任务=m1")
        self.assertEqual(game.execute("CMD DEPART boat1 m1"), "OK 出航")
        self.assertEqual(game.execute("CMD COMPLETE m1"), "OK 救援完成")
        report = game.execute("CMD SETTLE")
        self.assertEqual(
            report, "结算: 回合=0 获救=2 完成=1 取消=0 剩余燃料=25"
        )

    def test_cmd_rejects_overload(self):
        game = make_game()
        game.execute("CMD SIGNAL s1 5 0 99")
        game.execute("CMD MISSION s1")
        self.assertEqual(game.execute("CMD DEPART boat1 m1"), "ERR overload")


if __name__ == "__main__":
    unittest.main()
