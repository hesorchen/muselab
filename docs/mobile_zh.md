# 手机端（PWA）

> [English](mobile.md)

muselab 自带 Web App Manifest + apple-touch-icon。部署到你自己的服务器后，
可以把它添加到手机主屏，启动后体验接近原生 app。

- **一份代码** 同时服务 iOS / Android / desktop——不打包 `.ipa` / `.apk`，
  不过 App Store 审核。
- **Standalone 模式**：无浏览器地址栏 / 标签栏，全屏 app shell。
- **主题色感知**：iOS 状态栏跟随 light / dark mode。
- **触屏适配**：输入框 ≥ 16 px（避免 iOS 自动放大）、禁用下拉刷新、键盘弹起
  时聊天自动跟随滚动。

## iPhone 安装方法

Chrome → ⋮ 菜单 → **分享** → **添加到主屏幕** → 添加。

Android Chrome 地址栏会主动提示「安装」。

> self-host 立场在这一步也保持一致：手机直连你自己的服务器，链路中没有
> Apple / Google 签名的 binary，也没有第三方分发渠道。**自托管的初衷
> 不被安装路径破坏。**

## Web Push 推送通知

**设置 → 通知** 里开启。后端暴露 `/api/push/{vapid-public,subscribe,unsubscribe}`，
VAPID 密钥通过 `.env` 注入；订阅信息按设备持久化在浏览器本地。即使浏览器标签
关闭，长时任务跑完也会推一条通知到设备。

## Roadmap

Service Worker 离线 UI 缓存——见 [TODO.md](../TODO.md)。
