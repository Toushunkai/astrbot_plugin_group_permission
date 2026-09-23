# astrbot_plugin_group_permission（群聊权限门禁）

在群里**只让群主 / 群管理（有单独开关）和白名单成员和机器人对话**；其他群友的消息按你选的模式处理，
而且他们**即使 @ 机器人或发唤醒词也不会唤起 AI**。

- **三道闸门**：消息入口拦截（抢优先级）+ LLM 请求取消 + 发送前拦截，单点失效也不会漏。
- **自检 + 自愈**：handler 因为热重载从注册表里消失时自动补注册，并在日志和插件页面报警。
- 按群独立配置：启用开关 / 模式 / 群主管理放行 / 白名单 / 命令白名单 / 固定文案。
- 自带 WebUI 插件配置页：可视化按群配置 + **判定模拟** + **运行时分发顺序**。
- 无第三方依赖，只用标准库。

> 开发环境对照的 AstrBot 版本：**4.28.1**（commit `95e98b8`）。下面引用的源码路径都按这个版本。

---

## 一、模块划分

`main.py` 只保留 AstrBot 的 handler 定义 —— 带 `@filter` 装饰器的方法必须与插件类处于同一模块，
AstrBot 是按 `handler_module_path` 给 handler 绑定插件实例的。其余逻辑按功能拆成 mixin：

| 模块 | 职责 |
| --- | --- |
| `main.py` | 插件类：三个闸门 handler 与 `gperm` 指令的薄壳（`@filter` 注册必须在这里） |
| `gate.py` | 判定核心（纯函数，不依赖 AstrBot） |
| `constants.py` | 共享常量、配置键白名单、日志器 |
| `plugin_config.py` | 全局配置读取 / 收敛 / 落盘 |
| `selfcheck.py` | 启动自检与 handler 自愈 |
| `roles.py` | 发送者身份识别（sender.role → 群对象 → 平台接口） |
| `enforcement.py` | 拦截判定、兜底闸门逻辑、文案组装与冷却 |
| `commands.py` | `gperm` 指令的实现 |
| `page_api.py` | 插件 Pages 的后端 API |
| `store.py` | 按群配置持久化（平台:群号 键） |
| `pages/settings/*.js` | 页面脚本（common / global / render / groups / simulate / app 六个 ES module） |

## 二、需求与实现对应

| 需求 | 实现 |
| --- | --- |
| 群内只影响群主和管理对话 | `allow_owner_admin` 单独开关；群主/群管理放行，关掉后他们也要靠白名单 |
| 以及白名单内的成员 | 全局白名单 + 按群白名单（按群优先） |
| 对非管理员和群友响应我指定的插件命令 | 模式 `command_only` + `command_whitelist`（默认 `help`、`sid`） |
| 配置写一个插件页面 | `pages/settings/`（自定义 WebUI 页）+ `_conf_schema.json`（官方配置面板） |
| 非群主/管理、非白名单的群友，唤醒词也不响应 | 三道闸门：入口拦截 + `OnLLMRequestEvent` 取消请求 + 发送前拦截 |
| 确认效果是否受加载顺序影响 | 见 [第六节](#六加载顺序会不会影响效果附源码论证) 与 `_tests/test_dispatch_order.py` |

## 三、判定流程

一条群消息进来时：

```
群消息
  │
  ├─ 插件总开关关 / 本群未启用 ──────────────────────► 放行（什么都不做）
  │
  ├─ 是 AstrBot 全局管理员（allow_global_admin）──────► 放行
  ├─ 在白名单（全局或本群）──────────────────────────► 放行
  ├─ 是群主 / 群管理 且 allow_owner_admin 开 ─────────► 放行
  │
  └─ 未授权 ──► event.should_call_llm(True)   ← 关键：@ 和唤醒词都不会有 AI 回复
                 │
                 └─ 按模式：
                      chat_only     → 就此结束（不打断事件，其它插件照常工作）
                      command_only  → 命中命令白名单 → 放行该指令（仍然禁止 AI）
                                      没命中        → stop_event（静默丢弃）
                      silent        → stop_event（静默丢弃）
                      reply         → 先 yield 固定文案，再 stop_event
```

角色是从哪来的（按「便宜 → 贵」的顺序，命中即止）：

1. **平台原始事件**：OneBot v11（aiocqhttp / NapCat / Lagrange / go-cqhttp 等）的群消息自带
   `sender.role`（`owner` / `admin` / `member`），直接读，零额外请求。
2. **AstrBot 已填好的群对象**：`event.message_obj.group.group_owner` / `group_admins`（部分适配器会填）。
3. **调用平台接口**：`await event.get_group()`（aiocqhttp 会走 `get_group_info` + `get_group_member_list`），
   按 `group_info_cache_ttl` 缓存，可用 `auto_query_group_info` 关掉。
4. 全都拿不到时按 `unknown_role_action` 处理，默认 `block`（保持“只有授权的人能对话”）。

## 四、四种模式

| 模式 | 未授权成员的消息 | 是否中断事件 | 触发 AI |
| --- | --- | --- | --- |
| `chat_only` 仅拦截 AI 对话 | 其它插件照常处理，但**不会**有 AI 回复 | 否 | 否 |
| `command_only` 仅放行指定命令（默认） | 只有命令白名单里的指令能触发，其余静默丢弃 | 命中时不中断 / 未命中中断 | 否 |
| `silent` 静默忽略 | 什么都不做，连其它插件也不响应 | 是 | 否 |
| `reply` 回复固定文案 | 回一条可配置文案后丢弃（带冷却防刷屏） | 是 | 是（只有我们的提示） |

`command_whitelist` 里写 `*` 表示放行全部命令。默认值 `["help", "sid"]`——`sid` 能让群友自己查到
用户 ID 再来找你加白名单。

> 「仅放行指定命令」模式下命中的命令**仍然禁止默认 AI**，但指令本身完全正常
> （插件自己 `yield event.request_llm(...)` 发起的请求也不受影响）。

## 五、三道闸门

单靠「抢优先级」是不够的：只要有任何一环让入口拦截器没被调用（`plugin_set`、会话级插件禁用、
插件热重载把注册表清空、别的插件先 `stop_event`），拦截就会静默失效。所以做了三层：

| 层 | 位置 | 作用 | 依赖「排前面」吗 |
| --- | --- | --- | --- |
| **L1** 入口拦截 | `AdapterMessageEvent`，priority = maxsize | 判定 + 按模式处理（静默 / 文案 / 放行命令） | 是 |
| **L2** LLM 请求取消 | `OnLLMRequestEvent`，priority = maxsize | 未授权成员的 LLM 请求直接取消 | 否 |
| **L3** 发送前拦截 | `OnDecoratingResultEvent`，priority = maxsize | 未授权成员的待发送内容直接清掉 | 否 |

L2 / L3 不参与「谁先谁后」的竞争，它们是管线在固定位置主动回调的：

- `astrbot/core/pipeline/process_stage/method/agent_sub_stages/internal.py`
  ```python
  if await call_event_hook(event, EventType.OnLLMRequestEvent, req):
      return          # ← 我们在这里 stop_event()，这次 LLM 请求就彻底不发生
  ```
  它拦的是**所有** LLM 请求：默认对话、插件里的 `request_llm`、主动回复（active_reply）都会经过这里。
- `astrbot/core/pipeline/result_decorate/stage.py`
  ```python
  await handler.handler(event)
  if (result := event.get_result()) is None or not result.chain:
      ...             # ← result 被清掉了，RespondStage 就不会发送
  ```

**安全性**：L2 的兜底判定只对「真实入站群消息」生效（`platform != cron`、`sender_id` 非空、
且不等于 `self_id` / 群号、`raw_message` 存在），所以**不会误伤定时任务和主动推送**；
L3 只在 L1 已明确判定为拦截的事件上生效，并且给「我们自己发的那条权限提示」留了标记。
这两点都有测试覆盖（`_tests/test_hardening.py`）。

**自检 / 自愈**：插件实例化时会检查自己是否在 `star_handlers_registry` 里、优先级对不对、排第几，
不在就手动补注册，并在日志里分别输出：

```
群聊权限门禁自检：拦截器已就位（优先级=9223372036854775807，消息分发顺序第 1/12 个）
```
或者
```
群聊权限门禁自检：拦截器前面还有 2 个 handler：xxx、yyy。...
群聊权限门禁：AstrBot 配置里的 plugin_set=[...] 不包含本插件，所有 handler 都会被直接跳过...
群聊权限门禁：发现自己的拦截器不在 AstrBot 的 handler 注册表里（通常是插件热重载/注册表被清空导致的），已自动补注册。
```

## 六、加载顺序会不会影响效果（附源码论证）

**结论：会，但只影响「优先级相同」的 handler。本插件默认用 `sys.maxsize`，所以正常情况下不受影响；
即使被绕过，L2 / L3 仍然兜得住。**

### 1. handler 是怎么排序的

`astrbot/core/star/star_handler.py`：

```python
def append(self, handler: StarHandlerMetadata) -> None:
    if "priority" not in handler.extras_configs:
        handler.extras_configs["priority"] = 0
    self.star_handlers_map[handler.handler_full_name] = handler
    self._handlers.append(handler)
    self._handlers.sort(key=lambda h: -h.extras_configs["priority"])
```

- priority **不同** → 严格按从大到小，跟加载顺序无关；
- priority **相同** → Python 的 `list.sort` 是稳定排序 → 保持插入顺序，而插入顺序就是
  **插件加载顺序**（`star_manager._get_plugin_modules()` 先扫 `data/plugins`，再扫内置
  `astrbot/builtin_stars`；每个目录内部是 `os.listdir()` 的顺序，同一台机器通常稳定，但不保证跨机器一致）。

`priority` 由装饰器的关键字参数写进 `extras_configs`（`register/*.py` 里的 `**kwargs`），默认 `0`。
本插件用的是：

```python
@filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=sys.maxsize)
```

### 2. 排前面意味着什么

`astrbot/core/pipeline/waking_check/stage.py` 先把所有 filter 通过且已激活的 handler 收进
`activated_handlers`，然后 `astrbot/core/pipeline/process_stage/method/star_request.py` 按顺序调用：

```python
for handler in activated_handlers:
    if event.is_stopped():
        break
```

**排在前面的 handler 一旦 `stop_event()`，后面的全都不会执行。**

### 3. 为什么必须顶格，而不是随便给个大数

内置插件 `astrbot/builtin_stars/astrbot/main.py` 里有：

| handler | priority | 干什么 |
| --- | --- | --- |
| `handle_session_control_agent` | `maxsize` | 会话控制（SessionWaiter） |
| `handle_empty_mention` | `maxsize - 1` | 只 @ 一下 / 只发唤醒前缀时，**回复「想要问什么呢？😄」或直接发 LLM 请求，并等待下一条消息** |
| `persist_group_message` | `maxsize - 2` | 群聊上下文落库 |

`handle_empty_mention` 就是最危险的那个：未授权成员只发一个 `@机器人`，它就会接管并回复。

实测（`_tests/test_dispatch_order.py`）：

```
priority = maxsize（本插件默认）
   MAX  本插件(maxsize)
  9223372036854775806  内置空@处理(maxsize-1)
     0  普通插件A(priority=0)
  → 实际执行：['本插件(maxsize)']，回复：[]，AI：False      ✅ 拦住了

priority 改成 0（等于放弃排队优势）
  9223372036854775806  内置空@处理(maxsize-1)
     0  普通插件A(priority=0)
     0  本插件(priority=0)
  → 实际执行：['内置空@处理(maxsize-1)']，回复：['想要问什么呢？😄']   ❌ 没拦住
```

### 4. 什么情况下仍然可能被抢 / 被跳过

- **别的插件也用了 `maxsize`**：平手 → 按加载顺序，谁先加载谁先跑。
  （真实加载顺序对用户插件有利：用户插件先于内置插件加入列表。）
- **`plugin_set` 不是 `["*"]` 且不包含本插件**：本插件所有 handler 连同 L2/L3 一起被跳过
  （注意：**插件页面不受影响**，因为页面走的是另一套路由）。这是最隐蔽的一种，启动日志和
  `gperm debug` 都会明确报出来。
- **会话级插件管理里禁用了本插件**：`SessionPluginManager` 只过滤 `AdapterMessageEvent` handler，
  所以 L1 会被跳过，但 **L2/L3 仍然生效**（钩子不走这个过滤）。
- **插件热重载丢 handler**：v1.0.1 已自愈 + 报警。
- **有人把本插件的 `priority` 调小**：插件页面「全局默认 → 分发顺序」里改，改完立刻重排。

### 5. 怎么在你自己的部署里确认

- 插件页面 →「**分发顺序**」页签：实时读出 `star_handlers_registry` 里所有
  `AdapterMessageEvent` handler 的顺序，本插件那一行会高亮。
- 群里发 `gperm debug`：一次性打印拦截器注册状态、分发位置、`plugin_set`、会话插件禁用状态、
  本群生效配置，以及「你这条消息会被怎么判定」。

## 七、⚠️ 一个反直觉的 AstrBot API

`event.should_call_llm(x)` 的参数**不是**「要不要调用 LLM」，而是「要不要**禁止**默认 LLM 请求」：

- `AstrMessageEvent.call_llm` 的默认值是 **`False`**（`astr_message_event.py:94`，
  字段注释是「是否在此消息事件中禁止默认的 LLM 请求」）；
- `ProcessStage` 里的判断是
  `if not event._has_send_oper and event.is_at_or_wake_command and not event.call_llm:`
  （`process_stage/stage.py:55-66`）——`call_llm` 为 `False` 时才调用 LLM。

所以**禁止默认 AI 对话要写 `event.should_call_llm(True)`**；写 `False` 等于什么都没做
（插件照样能装上、日志也不报错，只是 AI 依旧会回复）。本插件已按正确语义调用，
`_tests/test_plugin.py` 里有对应的回归测试。

## 八、排障清单：明明配了却没拦住

> **最常见的原因：`plugin_set`。** AstrBot 的全局配置里有一项 `plugin_set`（WebUI 里是
> 「使用哪些插件」的多选，默认 `["*"]`）。只要它被改成了显式列表，**不在列表里的插件所有
> handler 都会被跳过**——`WakingCheckStage` 会把 `event.plugins_name` 设成这个列表，
> `get_handlers_by_event_type(..., plugins_name=...)` 再据此过滤。后果就是
> **插件页面一切正常（页面不走这套过滤），但群里完全没反应，而且 AstrBot 不会有任何报错**。
>
> 典型日志（插件 v1.0.1+ 会自己报出来）：
>
> ```
> 群聊权限门禁：AstrBot 配置里的 plugin_set=['builtin_commands', 'astrbot', ...]
> 不包含本插件，所有 handler（拦截器、LLM 兜底闸门、gperm 指令）都会被直接跳过，
> 表现为「插件页面正常但群里完全不生效」。
> ```
>
> 两种修法：
> 1. 插件页面「总览」页签点 **「一键把本插件加进 plugin_set」**（只往列表里追加本插件名，不动别人）；
> 2. 手工把 `data/cmd_config.json` 里的 `plugin_set` 改成 `["*"]`，或把
>    `astrbot_plugin_group_permission` 加进那个列表，然后重载配置。

按这个顺序查（每一步都能定位到具体原因）：

1. **启动日志**里有没有这几行：
   - `群聊权限门禁已加载：...` —— 没有它 = 插件根本没被实例化（被禁用 / 加载失败）。
   - `群聊权限门禁：AstrBot 配置里的 plugin_set=[...] 不包含本插件` —— 见上面那段，**这是最容易被忽略的一条**。
   - `群聊权限门禁自检：拦截器已就位（...第 1/N 个）` —— 正常。
   - `群聊权限门禁自检：拦截器前面还有 N 个 handler：...` —— 前面有抢答的插件。
     注意 v1.0.2 之前有个 bug：自检里的无条件重排会把本插件挤到同优先级的内置 handler 后面
     （实测会报 `前面还有 1 个 handler：handle_session_control_agent`），v1.0.2 已修。
   - `群聊权限门禁自检：...已自动补注册` —— handler 曾因热重载丢失，本次已自愈。
2. **群里发 `gperm debug`**（群主/群管理/AstrBot 管理员可用）。没反应的话，基本可以断定
   本插件的 handler 被全局过滤了 —— 依次检查：`plugin_set`、插件管理里的启用状态（`activated`）、
   「会话插件管理」里是否禁用了本插件。
3. **让那个未授权群友再发一条 @消息**，然后在日志里搜 `群聊权限门禁`：
   - `拦截 群=... 动作=block_silent ...` → L1 生效了（没收到回复才对）。
   - `放行 群=... 角色=owner/admin(...)` → 角色被识别成群主/管理了（`allow_owner_admin` 默认是开的），
     把那个开关关掉，或把 `unknown_role_action` 设为 `block`。
   - `判定失败，本条消息已放行` → 判定过程抛异常了，把完整 traceback 发出来。
   - 只有 `已取消未授权成员的 LLM 请求` → L1 没跑但 L2 兜住了（说明 handler 被过滤掉了，回到第 2 步）。
   - **一条都没有** → handler 没被执行（回到第 1、2 步）。
4. **插件页面 →「判定模拟」**：确认配置项本身是对的（角色、模式、白名单）。
5. **插件页面 →「分发顺序」**：确认本插件排在所有会抢答的 handler 之前。

## 九、安装

1. 把整个 `astrbot_plugin_group_permission` 目录放到 AstrBot 的 `data/plugins/` 下
   （或在 WebUI 插件管理里直接上传 zip）。
   > ⚠️ 如果你的 AstrBot 配置里 `plugin_set` 不是 `["*"]`（WebUI 的「使用哪些插件」多选），
   > **新装的插件不会自动进入这个名单**，会导致插件装了却完全不生效。见第七节。
2. 重载插件；打开插件卡片 →「群聊权限门禁」页面开始配置。
3. 建议重载后看一眼启动日志里的「群聊权限门禁自检」那几行。

数据文件：`data/plugin_data/astrbot_plugin_group_permission/group_settings.json`
（只存「按群覆盖」，全局默认存在 AstrBot 的插件配置里）。写入是「先写临时文件再原子替换」。

## 十、指令

群主 / 群管理 / AstrBot 全局管理员可用：

| 指令 | 说明 |
| --- | --- |
| `gperm status` | 查看本群生效规则 + 你自己会被怎么判定 |
| `gperm debug` | 排查用：拦截器注册状态、分发位置、`plugin_set`、会话插件禁用、本群配置 |
| `gperm on` / `gperm off` | 启用 / 停用本群门禁 |
| `gperm mode <模式>` | 切换模式：`chat_only` / `command_only` / `silent` / `reply` |
| `gperm add @某人` / `gperm add 123456` | 加入本群白名单（自动跳过 @机器人 自己） |
| `gperm del @某人` / `gperm del 123456` | 移出白名单 |
| `gperm list` | 查看本群白名单与命令白名单 |
| `gperm cmd add 签到` / `gperm cmd del 签到` | 维护命令白名单 |
| `gperm reset` | 清除本群覆盖，恢复跟随全局 |

## 十一、插件页面

`pages/settings/`：

1. **总览**：告警条（拦截器状态异常时会红黄提示）+ 规则说明 + 当前生效配置 + 四种模式 + 判定动作含义。
2. **全局默认**：总开关、默认启用、群主管理放行、模式、固定文案、白名单、命令白名单、
   角色识别（是否调接口 / 缓存 / 超时 / 识别失败策略）、优先级、日志。
3. **按群配置**：群列表（自动记录机器人见过的群）+ 每群独立覆盖（留空 = 跟随全局）。
   行内三个操作：**编辑** / **恢复全局**（只清本群配置，这一行因为"机器人见过这个群"会保留）/
   **删除记录**（把这一行连同"见过的群"记录一起删掉）。顶部还有「清理未配置的群记录」批量清。
4. **判定模拟**：填群号/用户/角色/消息，直接看会被判定成什么、会不会发文案。
5. **分发顺序**：运行时 handler 顺序表（见第五节）。

## 十二、测试

```bash
cd astrbot_plugin_group_permission
python3 _tests/run_all.py                # 全部 96 个用例
python3 _tests/test_gate.py              # 纯判定逻辑（23）
python3 _tests/test_plugin.py            # 拦截器集成（28）
python3 _tests/test_hardening.py         # 自检/自愈 + plugin_set + L2/L3（22）
python3 _tests/test_page_config.py       # 平台键 + 回复方式 + 成员获取 + 删除（15）
python3 _tests/test_dispatch_order.py    # 分发顺序实验（8）+ 顺序演示
```

`_tests/` 下是一个**最小 AstrBot 桩**，按 AstrBot v4.28.1 的源码复刻了
`StarHandlerRegistry` 的排序、`call_handler` 的 yield 语义、调度器在 yield 点的 stop 判断、
`ProcessStage` 的 LLM 判定，以及 `star_handlers_registry` 的注册/自愈路径：

| 文件 | 职责 |
| --- | --- |
| `_fake_astrbot.py` | 安装桩到 `sys.modules` + 再导出 |
| `_stub_core.py` | 核心：`EventType` / `StarHandlerMetadata` / 注册表 / 事件类型过滤器 |
| `_stub_platform.py` | 平台对象：事件、消息组件、装饰器、`Context`、web 桩 |
| `_sim_pipeline.py` | 管线仿真与事件工厂（`run_handler_chain` / `make_event`） |
| `_harness.py` | 测试脚手架（造插件实例、跑闸门、收集输出） |

桩不是 AstrBot 本身，关键处都标了源码位置，真实行为请以 AstrBot 源码为准。


## 十三、同类实现对比

参考了 [Akinana22/astrbot_plugin_llmallowlist](https://github.com/Akinana22/astrbot_plugin_llmallowlist)
（93 行，只做「框架默认 LLM 回复白名单」）。它的核心思路很值得学：**不抢优先级、不 `stop_event()`**，
只调 `event.should_call_llm(True)`，因此天然不受加载顺序影响、也不影响其它插件。

**已经借鉴过来的三点：**

| 借鉴点 | 说明 |
| --- | --- |
| 按平台区分 | 它的白名单是 `平台名[UID,...]`；本插件的按群配置键改成 `平台:群号`（旧的裸群号数据仍可读，双向兼容） |
| 引用 → @ → 普通 三级降级 | 它的固定文案依次尝试引用回复、@回复、普通回复；本插件加了 `reply_style`（quote / at / plain），发送失败自动降级 |
| 群成员接口只调一次 | 它用 `bot.api.call_action("get_group_member_list")`；本插件优先走这条快路径，失败再退回 `event.get_group()`（在 aiocqhttp 上能少一次 `get_group_info` 调用） |

**比它多出来的保障（它没有的部分）：**

1. **堵住了「只 @ 一下」的洞**。`should_call_llm(True)` 只拦「框架默认 LLM 请求链路」，
   拦不住插件自己发起的 LLM 请求（AstrBot 源码注释原话）。内置插件 `handle_empty_mention`
   （`priority=maxsize-1`）在用户只发一个 `@机器人` 或只发唤醒前缀时，会自己
   `yield event.request_llm(...)` 或回复「想要问什么呢？😄」——只靠 `should_call_llm` 是拦不住的。
   本插件的 **L2 闸门**（`OnLLMRequestEvent` 里 `stop_event()`）把这条路径也堵死了，
   因为插件发起的请求同样要过 `internal.py` 的 `call_event_hook`。
2. **四档模式**：`chat_only`（≈它的做法）/ `command_only`（只放行指定命令）/ `silent` / `reply`。
3. **群主与群管理分开**处理（它就是「owner 或 admin 一起算」），且有**单独开关**。
4. **超管兜底**：它靠 `get_group_member_list` 判断管理员，接口不通时就没人能过；
   本插件有多级降级 + `unknown_role_action` 策略 + 全局管理员防自锁开关。
5. **按群配置 + WebUI 插件页面**（判定模拟、分发顺序、一键修 `plugin_set`），而不是手写文本格式。
6. **自检 / 自愈**：handler 丢了会自己补回来并报警。

**它的做法更适合的场景**：只想「不让非白名单用户触发 AI 回复」、完全不希望影响其它插件，
并且不介意「只 @ 一下时可能被内置插件回复」。

## 十四、已知限制

- **只想「不影响其它插件」就用 `chat_only` 模式**：其余三种模式会 `stop_event`，
  这意味着本插件之后的所有 handler 都被跳过。
- **跨平台角色识别**：非 OneBot 系适配器如果既不提供 `sender.role`、`get_group()` 也拿不到群成员，
  就只能选 `unknown_role_action = allow`，否则连群主都会被拦。
- **`unique_session`**：开启后 `session_id` 会被改写，但本插件只依赖群号与发送者 ID，不受影响。
- **`event.get_group()` 是网络调用**：已加超时与 TTL 缓存，失败按「识别失败」处理。
- **多机器人/多账号**：本插件按群号存配置，不区分平台实例；同一群号在不同平台会共用一份配置。
- **`plugin_set` 把本插件排除时，L2/L3 也会一起被跳过**（AstrBot 的钩子同样受 `plugins_name` 过滤），
  这种情况只能改 AstrBot 配置；插件会在启动日志、`gperm debug` 和插件页面里明确报出来，
  并提供一个「一键把本插件加进 plugin_set」的按钮。

## 十五、版本记录

| 版本 | 变更 |
| --- | --- |
| v1.0.0 | 首发：四档处理模式（仅拦 AI / 仅放行指定命令 / 静默忽略 / 回复固定文案）、按群配置、`gperm` 管理指令、WebUI 插件页面（按群配置 + 判定模拟 + 分发顺序） |
| v1.0.1 | 新增自检与自愈（handler 从注册表消失时自动补注册并报警）；新增 L2 / L3 兜底闸门（取消未授权成员的 LLM 请求、拦下待发送内容） |
| v1.0.2 | 修：`plugin_set` 不含本插件时所有 handler 被静默跳过（自检报出 + 页面一键修复）；修：自检重排把自己挤到同优先级内置 handler 之后 |
| v1.0.3 | 借鉴 astrbot_plugin_llmallowlist：按群配置键改为 `平台:群号`（兼容旧数据）、固定文案支持 `reply_style`（引用 → @ → 普通，失败自动降级）、群成员改用 `get_group_member_list` 快路径 |
| v1.0.4 | 修：页面添加的群配置无法真正删除（新增「删除记录」与「清理未配置的群记录」，`forget_group` / `prune_seen`）；按功能拆分模块（`main.py` 只留 `@filter` handler，其余拆到 6 个 mixin），页面脚本拆成 6 个 ES module |
