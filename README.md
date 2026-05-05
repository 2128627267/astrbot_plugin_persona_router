# 🐾 动态人格路由 (persona-router)

> AstrBot 插件 · 根据聊天内容自动切换 LLM 人格

[![AstrBot](https://img.shields.io/badge/AstrBot-Plugin-blue)](https://github.com/Soulter/AstrBot)
[![Version](https://img.shields.io/badge/version-v1.0.0-green)](https://github.com/2128627267/astrbot_plugin_persona_router)
[![License](https://img.shields.io/badge/license-GPL%20v3-blue)](https://www.gnu.org/licenses/gpl-3.0.html)

---

## ✨ 功能

- 🔑 关键词匹配 — 三级权重（high/normal/exclude），自动打分命中
- 📢 唤醒词触发 — 消息中含指定词立即切换，支持 contains/startswith/regex
- 🧊 冷却锁定 — 切换后 N 条消息不重新判断，防止频繁跳变
- 💬 手动切换 — /persona switch <人格ID> 一键切换
- 🔇 消息拦截 — 可配置拦截规则，匹配后不调用 LLM
- 👥 群聊控制 — 按人格设置群聊启禁用 + @ 要求
- 🎨 可视化配置 — 全部配置项支持 WebUI 面板操作
- 🔄 自动发现 — 人格基础数据从 AstrBot 自动读取，不重复配置

---

## ⚙️ 配置说明

安装后，在 WebUI 插件配置面板中设置。

### 全局配置

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| 路由模式 | keyword / trigger_only / hybrid | hybrid |
| 默认人格 | 无法匹配时使用的人格 | 下拉选择 |
| 冷却消息数 | 切换后锁定的消息条数 | 3 |
| 匹配策略 | score / any / all | score |
| 最低命中分 | 加权打分模式的最低阈值 | 1 |
| 手动切换 | 是否允许 /persona 命令 | 开启 |
| 全局切换提示 | 未设置独立提示时的默认模板 | 🐾 已切换至【{persona_name}】 |

### 人格路由规则（可添加多条）

| 配置项 | 说明 |
|--------|------|
| 目标人格 | 从 AstrBot 已有列表中选择 |
| 唤醒词 | 触发路由判断的关键词列表 |
| 匹配方式 | contains / startswith / regex |
| 高权重关键词 | 命中得 3 分 |
| 普通关键词 | 命中得 1 分 |
| 排除词 | 命中扣 2 分 |
| 切换提示 | 切换到该人格时的提示语（空则用全局） |
| AI 提示模板 | 追加到 system_prompt 末尾的上下文 |
| 群聊启用 / 需@ | 群聊中的权限控制 |
| 特殊行为 | 正常切换 / 拦截消息 |

---

## 📋 指令列表

| 指令 | 说明 |
|------|------|
| /persona list | 列出所有已配置路由规则的人格 |
| /persona switch <ID> | 手动切换到指定人格 |
| /persona status | 查看当前会话的人格状态 |
| /persona reload | 重载路由规则配置 |
| /人格 <ID> | 手动切换的快捷方式 |
| /人格列表 | 查看可用人格的快捷方式 |

---

## 📁 项目结构

astrbot_plugin_persona_router/
├── main.py              # 插件主文件
├── metadata.yaml        # 插件元数据
├── _conf_schema.json    # WebUI 配置定义
└── README.md            # 本文件
---

## 🛠 依赖

- AstrBot >= v4.23.1

## 📝 许可

GNU General Public License v3.0 © 2025 qf

本项目采用 GPL v3 许可证。

---

## 🙋 作者

- qf — GitHub: https://github.com/2128627267
