# muselab 文档

> [English](README.md) · [← 返回项目 README](../README.md)

## 安装与运行

- [快速入门](quickstart_zh.md) —— 环境要求、Docker、开发模式、各平台说明
- [Linux 安装](install-linux_zh.md)
- [macOS 安装](install-macos_zh.md)
- [升级](upgrade_zh.md) —— 升级 SDK + CLI 而不丢数据

## 使用

- [定制你的 CLAUDE.md](personalize-claude-md_zh.md) —— 让 Muse 了解你
- [Skills](skills_zh.md) —— 开箱即用的 skill 清单，以及如何添加自己的
- [手机端 PWA](mobile_zh.md) —— 加到主屏、推送通知、HTTPS
- [定时任务](scheduler_zh.md) —— 按节奏运行保存的 prompt

## 模型

- [模型 / Providers](providers_zh.md) —— 内置目录（Claude、DeepSeek、GLM、MiniMax、Kimi、Qwen、小米 MiMo、百度 ERNIE）
- [接入新 provider](add-provider_zh.md) —— 接任意 Anthropic 兼容端点
- [模型路由与 chat 循环](routing_zh.md) —— 模型如何被选择、池化，以及费用如何计入正确账户

## 架构与内部机制

深度溯源解读——从 [架构](architecture_zh.md) 开始看全局。

- [架构](architecture_zh.md) —— 目录地图 + 一个请求的完整链路
- [会话内部机制](backend-sessions_zh.md) —— 索引、sidecar、队列、fork、崩溃恢复
- [Files API](backend-files_zh.md) —— 每个 `/api/files/*` 端点 + `safe_resolve`
- [安全模型](backend-security_zh.md) —— 鉴权、账单隔离与已知局限
- [前端内部机制](frontend_zh.md) —— 无构建 SPA、渲染流水线、SSE 客户端、service worker
- [MCP 架构](mcp-architecture_zh.md) —— 连接器策略与三层模型
- [基础设施](infrastructure_zh.md) —— 脚本、服务、Docker、测试、CI/CD

## 参考

- [配置参考](configuration_zh.md) —— 每个 `.env` 变量 + 默认值
- [数据与备份](data-and-backup_zh.md) —— 备份什么、如何恢复
- [排错](troubleshooting_zh.md) —— 常见故障与修法
- [词汇表](glossary_zh.md) —— muselab 的专有术语，统一定义

## 概念

- [同类对比](comparison_zh.md) —— muselab vs 其他自托管 AI 工作台
- [九位缪斯](muses_zh.md) —— 名字背后的世界观

## 项目

- [安全策略](../SECURITY.md)
- [贡献指南](../CONTRIBUTING.md)
- [第三方授权](../THIRD_PARTY_LICENSES.md)
