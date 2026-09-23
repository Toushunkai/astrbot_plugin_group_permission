"""分发顺序实验：效果到底会不会受加载顺序影响？

结论先说
========
**会，但只影响「优先级相同」的那些 handler。**
AstrBot 的 ``StarHandlerRegistry.append`` 是这么排的::

    self._handlers.append(handler)
    self._handlers.sort(key=lambda h: -h.extras_configs["priority"])

Python 的 list.sort 是**稳定排序**，所以：

- priority 不同 → 严格按 priority 从大到小，跟加载顺序无关；
- priority 相同 → 保持插入顺序，而插入顺序 = 插件加载顺序
  （``_get_plugin_modules()`` 先扫 ``data/plugins``、再扫内置 ``astrbot/builtin_stars``，
  目录本身是 ``os.listdir`` 的顺序，同一台机器上通常稳定，但不保证跨机器一致）。

然后是 ``StarRequestSubStage.process``::

    for handler in activated_handlers:
        if event.is_stopped():
            break

也就是说，排在前面的 handler 一旦 ``stop_event()``，后面的全都不会执行——
所以「本插件能不能拦住」= 「本插件有没有排在前面」。

为什么本插件默认 priority = sys.maxsize 就安全
=============================================
1. 内置插件 ``astrbot`` 的 ``handle_empty_mention`` 用 ``priority=maxsize - 1``，
   我们比它大，一定排在它前面（未授权成员只发一个 ``@机器人`` 时不会被它接管）；
2. 普通插件默认 priority = 0，我们一定排在它们前面；
3. 万一有插件也用了 ``maxsize``，那就是平手、按加载顺序——本插件没有任何办法
   保证赢过「同样顶格的别人」，这也是为什么插件配置页里把优先级做成可调、
   并且提供「分发顺序」页签让你直接看现场顺序。

跑法： python3 _tests/test_dispatch_order.py
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parent))

import _fake_astrbot  # noqa: E402

_fake_astrbot.install()

from _fake_astrbot import HandlerMeta, HandlerRegistry, make_event, run_handler_chain  # noqa: E402

MAX = sys.maxsize


def stop_and_reply(plugin_name: str, text: str, stop: bool = True):
    """造一个「会抢答」的插件 handler。"""

    async def handler(event):
        yield event.plain_result(text)
        if stop:
            event.stop_event()

    return handler


def silent_stop():
    """造一个「不吭声但中断事件」的 handler（跟本插件 silent 模式一样）。"""

    async def handler(event):
        event.stop_event()

    return handler


def order(registry):
    return [(h.plugin, h.priority) for h in registry]


class TestDispatchOrder(unittest.TestCase):
    def _run(self, registry, event=None):
        event = event or make_event(user_id="10001", role="member", message="你好")
        return asyncio.run(run_handler_chain(event, list(registry)))

    # ---------------------------------------------------------------- 平手 = 看加载顺序
    def test_same_priority_earlier_loaded_wins(self):
        """priority 相同时，先加载的那个先执行——这就是「受加载顺序影响」。"""
        registry = HandlerRegistry()
        registry.append(HandlerMeta("gate", "本插件(优先级0)", silent_stop(), priority=0))
        registry.append(HandlerMeta("noisy", "抢答插件(优先级0)", stop_and_reply("抢答", "我先说"), priority=0))

        result = self._run(registry)
        self.assertEqual(result["executed"], ["本插件(优先级0)"])
        self.assertEqual(result["replies"], [])

        # 把加载顺序反过来，结果就完全相反
        registry2 = HandlerRegistry()
        registry2.append(HandlerMeta("noisy", "抢答插件(优先级0)", stop_and_reply("抢答", "我先说"), priority=0))
        registry2.append(HandlerMeta("gate", "本插件(优先级0)", silent_stop(), priority=0))
        result2 = self._run(registry2)
        self.assertEqual(result2["executed"], ["抢答插件(优先级0)"])
        self.assertEqual(result2["replies"], ["我先说"], "加载顺序反了，未授权成员就被抢答插件回复了")

    def test_priority_beats_load_order(self):
        """优先级不同时，加载顺序不再影响结果：顶格的那位永远先跑。"""
        # 最坏情况：其它插件先加载，且都抢答
        registry = HandlerRegistry()
        registry.append(HandlerMeta("a", "插件A", stop_and_reply("A", "A说话"), priority=0))
        registry.append(HandlerMeta("b", "插件B", stop_and_reply("B", "B说话"), priority=0))
        registry.append(HandlerMeta("builtin", "内置空@处理", stop_and_reply("B", "想要问什么呢？😄"), priority=MAX - 1))
        # 本插件最后加载，但优先级顶格
        registry.append(HandlerMeta("gate", "本插件(maxsize)", silent_stop(), priority=MAX))

        result = self._run(registry)
        self.assertEqual(result["executed"], ["本插件(maxsize)"], "顶格优先级应该第一个执行")
        self.assertTrue(result["stopped"])
        self.assertEqual(result["replies"], [], "未授权成员不应该收到任何回复")
        self.assertFalse(result["llm_called"])

    def test_default_priority_loses_to_builtin_empty_mention(self):
        """如果把优先级调成默认的 0，内置的 handle_empty_mention 就会抢先接管。"""
        registry = HandlerRegistry()
        registry.append(HandlerMeta("builtin", "内置空@处理", stop_and_reply("B", "想要问什么呢？😄"), priority=MAX - 1))
        registry.append(HandlerMeta("gate", "本插件(优先级0)", silent_stop(), priority=0))
        result = self._run(registry)
        self.assertEqual(result["executed"], ["内置空@处理"])
        self.assertEqual(result["replies"], ["想要问什么呢？😄"])

    # ---------------------------------------------------------------- 顶格平手
    def test_tie_at_maxsize_goes_to_earlier_loaded(self):
        """顶格也会平手：同样用 maxsize 的插件，谁先加载谁先跑。"""
        registry = HandlerRegistry()
        registry.append(HandlerMeta("other", "另一个 maxsize 插件", stop_and_reply("X", "X说话"), priority=MAX))
        registry.append(HandlerMeta("gate", "本插件(maxsize)", silent_stop(), priority=MAX))
        result = self._run(registry)
        self.assertEqual(result["executed"], ["另一个 maxsize 插件"])

    def test_real_load_order_favours_user_plugins_on_tie(self):
        """真实的加载顺序：先扫 data/plugins，再扫内置 builtin_stars。

        ``_get_plugin_modules()`` 先把用户插件加进列表、再把内置插件追加进去，
        所以在 maxsize 平手时，用户插件（本插件）会排在内置插件前面。
        """
        registry = HandlerRegistry()
        # 模拟真实加载顺序：用户插件先、内置插件后
        registry.append(HandlerMeta("gate", "本插件(maxsize)", silent_stop(), priority=MAX))
        registry.append(HandlerMeta("builtin", "内置会话控制(maxsize)", stop_and_reply("B", "内置说话"), priority=MAX))
        result = self._run(registry)
        self.assertEqual(result["executed"], ["本插件(maxsize)"])
        self.assertEqual(result["replies"], [])

    # ---------------------------------------------------------------- 放行不受影响
    def test_authorized_user_is_not_stopped_by_gate(self):
        """授权用户：本插件什么都不做，后面的插件照常工作。"""

        async def gate(event):
            return  # 授权 → 直接返回

        registry = HandlerRegistry()
        registry.append(HandlerMeta("gate", "本插件(maxsize)", gate, priority=MAX))
        registry.append(HandlerMeta("chat", "普通聊天插件", stop_and_reply("C", "正常回复"), priority=0))
        result = self._run(registry)
        self.assertEqual(result["executed"], ["本插件(maxsize)", "普通聊天插件"])
        self.assertEqual(result["replies"], ["正常回复"])


class TestRegistryOrdering(unittest.TestCase):
    def test_sort_is_stable_and_descending(self):
        registry = HandlerRegistry()
        registry.append(HandlerMeta("p1", "先加载的0", None, priority=0))
        registry.append(HandlerMeta("p2", "后加载的0", None, priority=0))
        registry.append(HandlerMeta("p3", "顶格的", None, priority=MAX))
        registry.append(HandlerMeta("p4", "中间的", None, priority=5))
        self.assertEqual(
            [h.plugin for h in registry],
            ["顶格的", "中间的", "先加载的0", "后加载的0"],
        )

    def test_remove_and_reappend_reorders(self):
        """本插件改优先级走的就是 remove + append 这条路。"""
        registry = HandlerRegistry()
        gate = HandlerMeta("gate", "本插件", None, priority=0)
        registry.append(HandlerMeta("other", "别的插件", None, priority=5))
        registry.append(gate)
        self.assertEqual([h.plugin for h in registry], ["别的插件", "本插件"])

        gate.priority = MAX
        registry.remove(gate)
        registry.append(gate)
        self.assertEqual([h.plugin for h in registry], ["本插件", "别的插件"])


def _demo() -> None:
    print("=" * 78)
    print("演示 1：本插件 priority = maxsize（默认）")
    registry = HandlerRegistry()
    registry.append(HandlerMeta("a", "普通插件A(priority=0)", stop_and_reply("A", "A 抢答"), priority=0))
    registry.append(HandlerMeta("b", "内置空@处理(maxsize-1)", stop_and_reply("B", "想要问什么呢？😄"), priority=MAX - 1))
    registry.append(HandlerMeta("c", "本插件(maxsize)", silent_stop(), priority=MAX))
    for plugin, prio in order(registry):
        print(f"  {prio if prio != MAX else 'MAX':>4}  {plugin}")
    result = asyncio.run(run_handler_chain(make_event(message="@机器人"), list(registry)))
    print(f"  → 实际执行：{result['executed']}，回复：{result['replies']}，AI：{result['llm_called']}")

    print("=" * 78)
    print("演示 2：把本插件 priority 调成 0（等于放弃排队优势）")
    registry = HandlerRegistry()
    registry.append(HandlerMeta("a", "普通插件A(priority=0)", stop_and_reply("A", "A 抢答"), priority=0))
    registry.append(HandlerMeta("c", "本插件(priority=0)", silent_stop(), priority=0))
    registry.append(HandlerMeta("b", "内置空@处理(maxsize-1)", stop_and_reply("B", "想要问什么呢？😄"), priority=MAX - 1))
    for plugin, prio in order(registry):
        print(f"  {prio if prio != MAX else 'MAX':>4}  {plugin}")
    result = asyncio.run(run_handler_chain(make_event(message="@机器人"), list(registry)))
    print(f"  → 实际执行：{result['executed']}，回复：{result['replies']}，AI：{result['llm_called']}")
    print("=" * 78)


if __name__ == "__main__":
    _demo()
    unittest.main(verbosity=2, argv=[sys.argv[0]])
