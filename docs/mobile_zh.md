# 手机端（PWA）

> [English](mobile.md)

muselab 自带 Web App Manifest 和 apple-touch-icon。部署到用户自己的服务器后，可将其添加到手机主屏幕，启动体验接近原生应用。

- **单一代码库**同时服务 iOS / Android / 桌面端——无需打包 `.ipa` / `.apk`，无需经过 App Store 审核。
- **独立模式：** 无浏览器地址栏和标签栏，全屏应用外壳。
- **主题色感知：** iOS 状态栏跟随深色/浅色模式设置。
- **触屏优化：** 输入框字号不低于 16 px（避免 iOS 自动缩放）、禁用下拉刷新、键盘弹起时聊天界面自动跟随滚动。

## iPhone 安装方法

> **需要 HTTPS（安全上下文）。** iOS 只在页面通过 HTTPS 提供时才会注册 Service Worker，也只在此时才授予 Web Push 权限。纯 `http://` 的局域网 / IP 地址（如 `http://192.168.x.x:PORT`）在 iOS 上**不属于**安全上下文：SW 不会注册，「添加到主屏幕」只会得到一个降级版，推送权限也无法授予。两种获得安全上下文的方式：
> 1. **Tailscale**——通过设备的 `*.ts.net` MagicDNS 域名访问，自带 HTTPS（无需手动管理证书）。参见 [quickstart](quickstart_zh.md)。
> 2. **配真实证书的反向代理**——运行 [`scripts/setup-https.sh`](../scripts/setup-https.sh)，在 muselab 前面架一层 Caddy + Let's Encrypt，绑定你自己的域名。

用 **Safari** 打开页面（iOS Chrome 不暴露此菜单）→ **分享**菜单 → **添加到主屏幕** → 添加。

Android Chrome 地址栏会主动提示「安装」。

> 整个过程手机直接连接用户自己的服务器，链路中不涉及 Apple / Google 签名的二进制文件，也没有第三方分发渠道介入。

## Web Push 推送通知

在**设置 → 通知**中启用。后端暴露 `/api/push/{vapid-public,subscribe,unsubscribe}` 接口，VAPID 密钥通过 `.env` 注入；订阅信息按设备存储于浏览器本地。即使浏览器标签页已关闭，长时任务完成后也会向设备推送通知。

### iOS 限制

- **必须先「添加到主屏幕」。** iOS 只对已添加到主屏幕、并以独立模式启动的 PWA 开放 Web Push 权限——普通 Safari 标签页里无法启用推送。请先按上文安装应用，从主屏幕打开，再启用通知。
- **不支持震动。** muselab 会调用 `navigator.vibrate()` 触发震动反馈，但 iOS Safari 会忽略它；通知仍会正常弹出，只是没有震动。Android 支持震动。

## 下拉刷新

浏览器原生的下拉刷新已在全局禁用（误触下拉不应中断正在流式的会话）。不过，**文件树**有一套自己的自定义下拉刷新手势：在文件列表顶部下拉即可重新加载文件列表。这是 muselab 自己实现的，不是浏览器的——它只重新同步文件树，不会重载整个页面。
