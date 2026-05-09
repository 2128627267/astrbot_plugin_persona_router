"""
persona-router: AstrBot 动态人格路由插件

人格基础信息（system_prompt / 名称等）→ AstrBot persona_manager（权威数据源）
路由元数据（唤醒词 / 关键词 / 提示等）→ _conf_schema.json template_list（附加配置）

插件自动校验 template_list 中的 persona_id 是否存在于 AstrBot，
不存在的会在日志警告，不会影响已存在人格的路由。
"""

import asyncio
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig
from astrbot.api.provider import ProviderRequest
from astrbot.api.message_components import At
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.message import UserMessageSegment, TextPart


# ─────────────────────────────────────────────
#  数据类
# ─────────────────────────────────────────────

@dataclass
class SessionState:
    session_id: str
    persona_id: str
    msg_count: int = 0
    switched_at: float = field(default_factory=time.time)

    def in_cooldown(self, cd: int) -> bool:
        return self.msg_count < cd

    def tick(self):
        self.msg_count += 1

    def reset(self, pid: str):
        self.persona_id = pid
        self.msg_count = 0
        self.switched_at = time.time()


# ─────────────────────────────────────────────
#  人格切换行为（Tool）
# ─────────────────────────────────────────────

@dataclass
class GetPersonaListTool(FunctionTool):
    name: str = "get_persona_list"
    description: str = "获取可用人格列表"
    parameters: dict = field(default_factory=lambda: {
        "type": "object",
        "properties": {},
        "required": []
    })

    async def call(self, context_wrapper: ContextWrapper, **kwargs) -> ToolExecResult:
        try:
            from astrbot.core.astr_agent_context import AstrAgentContext
            ctx = context_wrapper.context
            all_p = await ctx.persona_manager.get_all_personas()
            persona_list = [p.persona_id for p in all_p]
            return ToolExecResult("\n".join(persona_list))
        except Exception as e:
            logger.error(f"get_persona_list 失败: {e}")
            return ToolExecResult(f"获取失败: {e}")


@dataclass
class SwitchPersonaTool(FunctionTool):
    name: str = "switch_persona"
    description: str = "切换到指定人格"
    parameters: dict = field(default_factory=lambda: {
        "type": "object",
        "properties": {
            "persona_id": {
                "type": "string",
                "description": "目标人格 ID"
            }
        },
        "required": ["persona_id"]
    })

    async def call(self, context_wrapper: ContextWrapper, **kwargs) -> ToolExecResult:
        try:
            persona_id = kwargs.get("persona_id")
            if not persona_id:
                return ToolExecResult("请指定目标人格")

            from astrbot.core.astr_agent_context import AstrAgentContext
            ctx = context_wrapper.context
            event = context_wrapper.event
            umo = event.unified_msg_origin

            # 验证人格存在
            await ctx.persona_manager.get_persona(persona_id)

            # 执行切换（通过插件实例）
            plugin = ctx.plugin_manager.get_plugin("persona-router")
            if plugin and hasattr(plugin, "_do_switch_via_tool"):
                await plugin._do_switch_via_tool(umo, persona_id, event)
            
            return ToolExecResult(f"已切换到人格: {persona_id}")
        except ValueError:
            return ToolExecResult(f"人格不存在: {persona_id}")
        except Exception as e:
            logger.error(f"switch_persona 失败: {e}")
            return ToolExecResult(f"切换失败: {e}")


# ─────────────────────────────────────────────
#  关键词匹配引擎
# ─────────────────────────────────────────────

class KeywordEngine:

    def __init__(self, method: str, min_hits: int,
                 high_w: int, exclude_p: int):
        self.method = method
        self.min_hits = min_hits
        self.high_w = high_w
        self.exclude_p = exclude_p

    def match(self, text: str, rules: List[dict]) -> Optional[str]:
        if not text or not rules:
            return None

        msg = text.lower()

        if self.method == "any":
            for r in rules:
                if self._score(msg, r) > 0:
                    return r["persona_id"]
            return None

        if self.method == "all":
            for r in rules:
                if self._all_hit(msg, r):
                    return r["persona_id"]
            return None

        # score
        scores = {}
        for r in rules:
            s = self._score(msg, r)
            if s >= self.min_hits:
                scores[r["persona_id"]] = s

        if not scores:
            return None

        best = max(scores, key=scores.get)
        logger.debug(f"关键词匹配: {best} ({scores[best]}分), 全部: {scores}")
        return best

    def _score(self, msg: str, rule: dict) -> int:
        score = 0
        for w in rule.get("keywords_high", []) or []:
            if w and w.lower() in msg:
                score += self.high_w
        for w in rule.get("keywords_normal", []) or []:
            if w and w.lower() in msg:
                score += 1
        for w in rule.get("keywords_exclude", []) or []:
            if w and w.lower() in msg:
                score += self.exclude_p
        return max(score, 0)

    def _all_hit(self, msg: str, rule: dict) -> bool:
        all_w = (rule.get("keywords_high", []) or []) + \
                (rule.get("keywords_normal", []) or [])
        if not all_w:
            return False
        return all(w.lower() in msg for w in all_w)


# ─────────────────────────────────────────────
#  会话管理器
# ─────────────────────────────────────────────

class SessionManager:

    def __init__(self, default_pid: str):
        self._m: Dict[str, SessionState] = {}
        self._default = default_pid

    def get(self, sid: str) -> SessionState:
        if sid not in self._m:
            self._m[sid] = SessionState(sid, self._default)
        return self._m[sid]

    def switch(self, sid: str, pid: str):
        self.get(sid).reset(pid)

    def current(self, sid: str) -> str:
        return self.get(sid).persona_id

    def cleanup(self, max_age: float = 3600.0) -> int:
        now = time.time()
        stale = [k for k, v in self._m.items()
                 if now - v.switched_at > max_age]
        for k in stale:
            del self._m[k]
        return len(stale)


# ─────────────────────────────────────────────
#  插件主类
# ─────────────────────────────────────────────

@register("persona-router", "jian",
           "根据聊天内容自动切换 LLM 人格。人格基础数据由 AstrBot 管理，"
           "本插件仅附加路由元数据（唤醒词/关键词/切换提示）。",
           "1.0.0")
class PersonaRouterPlugin(Star):

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.cfg = config

        self._rules: List[dict] = []          # 从 template_list 解析
        self._rules_map: Dict[str, dict] = {} # persona_id → rule
        self._sessions: Optional[SessionManager] = None
        self._kw: Optional[KeywordEngine] = None
        self._cleanup: Optional[asyncio.Task] = None
        self._persona_cache: Dict[str, Any] = {}

    # ═══════════════ 生命周期 ═══════════════

    async def initialize(self):
        self._build_rules()
        await self._validate_personas()
        logger.info("初始化人格路由插件，加载规则数: %d", len(self._rules))

        default = self.cfg.get("default_persona_id", "default")
        self._sessions = SessionManager(default)
        self._kw = KeywordEngine(
            method=self.cfg.get("keyword_method", "score"),
            min_hits=self.cfg.get("min_hits", 1),
            high_w=self.cfg.get("keyword_high_weight", 3),
            exclude_p=self.cfg.get("keyword_exclude_penalty", -2),
        )
        self._cleanup = asyncio.create_task(self._periodic_cleanup())

        # 注册人格切换工具
        self.context.add_llm_tools(
            GetPersonaListTool(),
            SwitchPersonaTool(),
        )
        logger.info("已注册人格切换工具: get_persona_list, switch_persona")

        logger.info(
            f"persona-router 就绪: mode={self.cfg.get('router_mode')}, "
            f"default={default}, rules={len(self._rules)}"
        )

    async def terminate(self):
        if self._cleanup:
            self._cleanup.cancel()
            try:
                await self._cleanup
            except asyncio.CancelledError:
                pass
        logger.info("persona-router 已卸载")

    # ═══════════════ 核心钩子 ═══════════════
    # 注意：本钩子不限制平台，router_mode 为 hybrid 时自动适配群聊/私聊。
    # 如需限制为特定平台（如仅 aiocqhttp），可添加装饰器：
    # @filter.platform_adapter_type(PlatformAdapterType.AIOCQHTTP)

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        """核心路由逻辑：分析消息内容，决定是否切换人格"""
        # 确保引擎已初始化
        if not self._sessions or not self._kw:
            return
        
        # ── 获取基础信息 ──
        umo = event.unified_msg_origin
        message_text = event.message_str or ""
        if not umo:
            logger.warning("缺少 unified_msg_origin，跳过路由")
            return
        
        # ── 获取会话详情 ──
        msg_obj = event.message_obj
        is_group = bool(getattr(msg_obj, "group_id", ""))
        is_mentioned = self._check_at(event)
        
        # ── 调试日志 ──
        logger.debug(
            f"路由请求: session={umo}, msg='{message_text[:30]}...', "
            f"group={is_group}, mentioned={is_mentioned}"
        )
        
        # ── 步骤1: 检查手动切换命令 ──
        if self.cfg.get("manual_switch_enabled", True):
            manual_target = self._parse_manual(message_text)
            if manual_target:
                await self._do_switch(umo, manual_target, event, req)
                return
        
        # ── 步骤2: 获取当前会话状态 ──
        st = self._sessions.get(umo)
        
        # ── 步骤3: 冷却期检查 ──
        cooldown = self.cfg.get("cooldown_messages", 3)
        if st.in_cooldown(cooldown):
            st.tick()
            logger.debug(f"冷却中: {st.msg_count}/{cooldown}")
            return  # 保持当前人格
        
        # ── 步骤4: 路由判断 ──
        mode = self.cfg.get("router_mode", "hybrid")
        matched: Optional[str] = None
        
        # 4a. 关键词匹配（keyword / hybrid 模式）
        if mode in ("keyword", "hybrid"):
            matched = self._kw.match(message_text, self._rules)
        
        # 4b. 唤醒词兜底（trigger_only / hybrid 未命中时）
        if not matched and mode in ("trigger_only", "hybrid"):
            matched = self._match_wake(message_text, current_pid=st.persona_id)
        
        # ── 步骤5: 未匹配 → 保持当前人格 ──
        if not matched:
            logger.debug("未匹配到任何人格，保持当前人格")
            st.tick()
            return  # 不要阻止后续处理
        
        # ── 步骤6: 已是当前人格 → 无需切换 ──
        if matched == st.persona_id:
            logger.debug("已是当前人格，无需切换")
            st.tick()
            return
        
        # ── 步骤7: 群聊权限检查 ──
        rule = self._rules_map.get(matched, {})
        if is_group:
            # 1. 检查是否启用群聊
            if not rule.get("group_enabled", True):
                logger.debug(f"群聊中未启用人格 {matched}，跳过")
                st.tick()
                return
            
            # 2. 检查@要求
            require_mention = rule.get("group_require_mention", False)
            if require_mention and not is_mentioned:
                logger.debug(f"需要@但未被@，跳过 {matched}")
                st.tick()
                return
            
            # 3. 被@时优先触发（即使不要求@）
            if not require_mention and is_mentioned:
                logger.debug(f"未要求@但被@，优先触发 {matched}")
        
        # ── 步骤8: 拦截检查 ──
        if rule.get("action") == "block":
            logger.info(f"消息拦截: umo={umo}, persona={matched}")
            event.stop_event()  # 阻止 LLM 调用
            st.tick()
            return
        
        # ── 步骤9: 执行切换 ──
        logger.info(
            f"执行切换: {umo} → {matched} "
            f"(群聊状态: {is_group}/{'@' if is_mentioned else '未@'})"
        )
        await self._do_switch(umo, matched, event, req)


    # ═══════════════ 指令 ═══════════════

    @filter.command_group("persona")
    def persona(self):
        pass

    @persona.command("switch", alias={"人格"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_switch(self, event: AstrMessageEvent, target: Optional[str] = None):
        """手动切换人格 (需要管理员权限)"""
        if not target:
            yield event.plain_result("❌ 请指定要切换的人格 ID。用法：/persona switch <人格ID>")
            return

        try:
            persona = await self.context.persona_manager.get_persona(target)
        except ValueError:
            yield event.plain_result(
                f"❌ 人格「{target}」不存在。请先在 WebUI「人格设定」中创建。"
            )
            return

        umo = event.unified_msg_origin
        name = getattr(persona, "persona_id", target)
        notice = self._rules_map.get(target, {}).get("switch_notice", "") or \
                 self.cfg.get("global_switch_notice", "")
        if notice:
            notice = notice.format(persona_name=name, persona_id=target)

        # 先持久化，再更新内存状态
        await self._persist_persona(umo, target)
        self._sessions.switch(umo, target)

        if notice:
            yield event.plain_result(notice)
        else:
            yield event.plain_result(f"🐾 已切换至【{name}】")

    @persona.command("list", alias={"人格列表"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_list(self, event: AstrMessageEvent):
        """列出所有已配置路由规则的人格，以及 AstrBot 中的全部人格 (需要管理员权限)"""
        lines = ["📋 已配置路由规则的人格:"]
        if not self._rules:
            lines.append("  （无）")
        for r in self._rules:
            pid = r["persona_id"]
            wake = r.get("wake_words", []) or []
            wake_s = ",".join(wake[:3]) if wake else "—"
            act = " 🚫拦截" if r.get("action") == "block" else ""
            lines.append(f"  • {pid}{act}  唤醒:[{wake_s}]")

        lines.append("")
        lines.append("📋 AstrBot 中的全部人格:")

        try:
            all_p = await self.context.persona_manager.get_all_personas()
            for p in all_p:
                has_rule = "✅" if p.persona_id in self._rules_map else "⬜"
                lines.append(f"  {has_rule} {p.persona_id}")
        except Exception as e:
            lines.append(f"  （获取失败: {e}）")

        lines.append("")
        lines.append(f"默认人格: {self.cfg.get('default_persona_id', 'default')}")
        lines.append(f"路由模式: {self.cfg.get('router_mode', 'hybrid')}")
        lines.append(f"冷却: {self.cfg.get('cooldown_messages', 3)} 条消息")
        lines.append(f"人格工具: ✅ get_persona_list, switch_persona")

        yield event.plain_result("\n".join(lines))

    @persona.command("status")
    async def cmd_status(self, event: AstrMessageEvent):
        umo = event.unified_msg_origin
        st = self._sessions.get(umo)
        name = self._rules_map.get(st.persona_id, {}).get("persona_id", st.persona_id)
        cd = self.cfg.get("cooldown_messages", 3)
        remain = max(0, cd - st.msg_count)
        yield event.plain_result(
            f"📊 当前会话\n"
            f"  人格: {name}\n"
            f"  冷却: {remain}/{cd}\n"
            f"  会话: {umo}"
        )

    @persona.command("reload")
    async def cmd_reload(self, event: AstrMessageEvent):
        """重载路由规则（从 _conf_schema.json 重新读取 template_list）"""
        self._build_rules()
        await self._validate_personas()
        self._persona_cache.clear()
        yield event.plain_result(f"✅ 已重载 {len(self._rules)} 条路由规则")

    # ═══════════════ 内部：切换执行 ═══════════════

    async def _do_switch(self, umo: str, pid: str,
                         event: AstrMessageEvent,
                         req: Optional[ProviderRequest] = None):
        rule = self._rules_map.get(pid, {})

        # A. 获取人格信息（只调用一次，避免重复请求）
        persona = await self._get_persona(pid)
        persona_name = getattr(persona, "persona_id", pid) if persona else pid

        # B. 持久化 persona_id 到当前对话（先持久化再更新内存）
        await self._persist_persona(umo, pid)

        # C. 注入 system_prompt + hint_template
        if req is not None and persona and hasattr(persona, "system_prompt"):
            sp = persona.system_prompt
            hint = rule.get("hint_template", "")
            if hint:
                sp = sp.rstrip() + "\n\n" + hint.strip()
            req.system_prompt = sp
            logger.debug(f"注入 system_prompt: {pid} ({len(sp)} chars)")

        # D. 更新内存状态
        self._sessions.switch(umo, pid)

        # E. 切换提示（优先用人格自己的，否则用全局的）
        notice = rule.get("switch_notice", "") or self.cfg.get("global_switch_notice", "")
        if notice:
            try:
                await event.send(notice.format(
                    persona_name=persona_name,
                    persona_id=pid,
                ))
            except Exception as e:
                logger.warning(f"发送切换提示失败: {e}")

        logger.info(f"切换人格: {umo} → {pid}")

    async def _do_switch_via_tool(self, umo: str, pid: str, event: AstrMessageEvent):
        """供 SwitchPersonaTool 调用的简化版切换方法（无需 ProviderRequest）"""
        await self._do_switch(umo, pid, event, req=None)

    async def _persist_persona(self, umo: str, pid: str):
        """将 persona_id 写入当前对话"""
        try:
            cm = self.context.conversation_manager
            cid = await cm.get_curr_conversation_id(umo)
            if cid:
                await cm.update_conversation(umo, cid, persona_id=pid)
            else:
                await cm.new_conversation(umo, persona_id=pid)
        except Exception as e:
            logger.warning(f"持久化 persona_id 失败: {e}")

    async def _get_persona(self, pid: str) -> Optional[Any]:
        if pid in self._persona_cache:
            return self._persona_cache[pid]
        try:
            p = await self.context.persona_manager.get_persona(pid)
            if p:
                self._persona_cache[pid] = p
            return p
        except ValueError:
            logger.warning(f"人格不存在: {pid}")
            return None
        except Exception as e:
            logger.error(f"获取人格失败: {pid}, {e}")
            return None

    # ═══════════════ 内部：规则构建 ═══════════════

    def _build_rules(self):
        """从 _conf_schema.json 的 template_list 构建路由规则列表"""
        raw = self.cfg.get("persona_routing_rules", [])
        if not isinstance(raw, list):
            raw = []

        self._rules = []
        self._rules_map = {}

        for item in raw:
            pid = item.get("persona_id", "")
            if not pid:
                logger.warning(f"跳过空的 persona_id: {item}")
                continue

            rule = {
                "persona_id": pid,
                "wake_words": item.get("wake_words", []) or [],
                "wake_mode": item.get("wake_mode", "contains"),
                "keywords_high": item.get("keywords_high", []) or [],
                "keywords_normal": item.get("keywords_normal", []) or [],
                "keywords_exclude": item.get("keywords_exclude", []) or [],
                "switch_notice": item.get("switch_notice", ""),
                "hint_template": item.get("hint_template", ""),
                "group_enabled": item.get("group_enabled", True),
                "group_require_mention": item.get("group_require_mention", False),
                "action": item.get("action", "normal"),
            }

            if pid in self._rules_map:
                logger.warning(f"重复的 persona_id: {pid}，后出现的覆盖前者")

            self._rules.append(rule)
            self._rules_map[pid] = rule

        logger.debug(f"构建规则完成: {len(self._rules)} 条")

    async def _validate_personas(self):
        """校验规则中的 persona_id 是否在 AstrBot 中存在"""
        try:
            all_p = await self.context.persona_manager.get_all_personas()
            existing = {p.persona_id for p in all_p}
        except Exception as e:
            logger.warning(f"无法校验人格: {e}")
            return

        for r in self._rules:
            pid = r["persona_id"]
            if pid not in existing and r.get("action") != "block":
                logger.warning(
                    f"人格 '{pid}' 不在 AstrBot 中。请先在 WebUI"
                    f"「人格设定」中创建，或删除此路由规则。"
                )

    # ═══════════════ 内部：匹配 ═══════════════

    def _match_wake(self, msg: str, current_pid: str) -> Optional[str]:
        for r in self._rules:
            if r["persona_id"] == current_pid:
                continue
            mode = r.get("wake_mode", "contains")
            for w in (r.get("wake_words", []) or []):
                if not w:
                    continue
                if mode == "contains" and w in msg:
                    return r["persona_id"]
                if mode == "startswith" and msg.startswith(w):
                    return r["persona_id"]
                if mode == "regex":
                    try:
                        if re.search(w, msg):
                            return r["persona_id"]
                    except re.error as e:
                        logger.error(f"正则错误: {e}, '{w}'")
        return None

    def _parse_manual(self, msg: str) -> Optional[str]:
        s = msg.strip()
        for pfx in ("/人格 ", "/人格"):
            if s.startswith(pfx):
                t = s[len(pfx):].strip()
                if t:
                    return t
        return None

    def _check_at(self, event: AstrMessageEvent) -> bool:
        try:
            msgs = event.get_messages()
            sid = event.message_obj.self_id
            if not sid:
                return False
            sid = str(sid)
            for seg in msgs:
                if isinstance(seg, At):
                    at_id = str(getattr(seg, "qq", "") or
                                getattr(seg, "user_id", "") or
                                getattr(seg, "target", ""))
                    if at_id and at_id == sid:
                        return True
        except Exception as e:
            logger.debug(f"_check_at 异常: {e}")
        return False

    # ═══════════════ 后台 ═══════════════

    async def _periodic_cleanup(self, interval: int = 600):
        while True:
            try:
                await asyncio.sleep(interval)
                if self._sessions:
                    self._sessions.cleanup()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"清理异常: {e}")
