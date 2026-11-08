"""彩蛋物理：重力、地面反弹、侧墙与入睡阈值。"""

import unittest

from offcraft.ui.physics import Body, PhysicsWorld


def _world(**kw):
    return PhysicsWorld(width=300.0, height=200.0, **kw)


def _body(**kw):
    defaults = dict(x=150.0, y=100.0, width=20.0, height=20.0)
    defaults.update(kw)
    return Body(**defaults)


class PhysicsTest(unittest.TestCase):
    def test_gravity_and_floor_bounce(self):
        world = _world(gravity=900.0, restitution=0.5)
        body = _body(y=150.0, vy=120.0)
        world.step(body, 0.02)
        self.assertGreater(body.vy, 120.0)          # 重力加速
        self.assertGreater(body.y, 150.0)           # 下落
        body.y = 190.0
        body.vy = 300.0
        world.step(body, 0.01)
        self.assertLess(body.vy, 0.0)               # 撞地反弹
        self.assertAlmostEqual(body.y, 190.0)       # 归位到贴地
        self.assertFalse(body.on_floor)             # 反弹后离地

    def test_ground_friction_and_sleep(self):
        world = _world()
        body = _body(x=150.0, y=190.0, vx=50.0, spin=3.0, on_floor=True)
        for _ in range(120):
            world.step(body, 0.016)
        self.assertLess(abs(body.vx), world.settle_speed + 1)
        self.assertTrue(body.asleep)

    def test_left_and_right_walls(self):
        world = _world(restitution=0.4)
        body = _body(x=8.0, y=80.0, vx=-200.0)
        world.step(body, 0.02)
        self.assertEqual(body.x, 10.0)
        self.assertGreater(body.vx, 0.0)

        body = _body(x=292.0, y=80.0, vx=200.0)
        world.step(body, 0.02)
        self.assertEqual(body.x, 290.0)
        self.assertLess(body.vx, 0.0)

    def test_step_all_reports_settled(self):
        world = _world()
        bodies = [Body(x=100.0, y=50.0, width=20, height=20, vy=100),
                  Body(x=200.0, y=190.0, width=20, height=20, on_floor=True)]
        self.assertFalse(world.step_all(bodies, 0.016))
        bodies[0].y = 190.0
        bodies[0].vy = 0.0
        bodies[0].on_floor = True
        bodies[0].vx = 0.0
        bodies[0].spin = 0.0
        self.assertTrue(world.step_all(bodies, 0.016))


if __name__ == "__main__":
    unittest.main()
