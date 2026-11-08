"""轻量 2D 刚体物理：彩蛋「千万别点」把界面组件变成会掉落的方块。

纯 Python 数据类 + 固定步长积分，不依赖 Qt，方便单元测试。坐标以方块中心为原点。
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["Body", "PhysicsWorld"]


@dataclass
class Body:
    """一个带旋转的矩形刚体。``x`` / ``y`` 是中心点。"""

    x: float
    y: float
    width: float
    height: float
    vx: float = 0.0
    vy: float = 0.0
    angle: float = 0.0       # 弧度
    spin: float = 0.0        # 角速度（弧度/秒）
    on_floor: bool = False
    asleep: bool = False


@dataclass
class PhysicsWorld:
    """重力 + 地面/侧墙碰撞 + 摩擦；所有参数按像素/秒。"""

    width: float
    height: float
    gravity: float = 980.0
    restitution: float = 0.42     # 反弹保留比例
    ground_friction: float = 0.86
    spin_decay: float = 0.82
    settle_speed: float = 24.0    # 水平速度低于此值且贴地 → 入睡
    settle_spin: float = 2.5

    def step(self, body: Body, dt: float) -> None:
        if body.asleep:
            return
        dt = max(0.0, min(float(dt), 0.05))     # 防止掉帧时穿透
        if body.on_floor:
            body.vy = 0.0
            body.y = self.height - body.height / 2
            body.vx *= self.ground_friction
            body.spin *= self.spin_decay
        else:
            body.vy += self.gravity * dt
            body.y += body.vy * dt

        body.x += body.vx * dt
        body.angle += body.spin * dt

        half_w = body.width / 2
        if body.x - half_w < 0:
            body.x = half_w
            body.vx = abs(body.vx) * self.restitution
            body.spin *= -0.6
        elif body.x + half_w > self.width:
            body.x = self.width - half_w
            body.vx = -abs(body.vx) * self.restitution
            body.spin *= -0.6

        # 贴地：从上方越过地面时反弹；速度太小就停住。
        floor_y = self.height - body.height / 2
        if body.y >= floor_y:
            body.y = floor_y
            if abs(body.vy) > 40:
                body.vy = -abs(body.vy) * self.restitution
                body.vx *= self.ground_friction
                body.spin *= -0.6
            else:
                body.vy = 0.0
                body.on_floor = True
        if body.on_floor and abs(body.vx) < self.settle_speed and abs(body.spin) < self.settle_spin:
            body.asleep = True

    def step_all(self, bodies: list[Body], dt: float) -> bool:
        """推进一帧；全部入睡时返回 True。"""
        for body in bodies:
            self.step(body, dt)
        return all(body.asleep for body in bodies)
