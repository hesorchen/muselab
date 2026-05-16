# Assets

放截图、demo gif 等媒体文件。

## 推荐录制工具

| 工具 | 平台 | 输出 | 推荐度 |
|------|------|------|--------|
| [vhs](https://github.com/charmbracelet/vhs) | 跨平台 | gif/mp4，可脚本化 | ⭐⭐⭐⭐⭐ |
| [LICEcap](https://www.cockos.com/licecap/) | Win/Mac | gif，简单 | ⭐⭐⭐⭐ |
| GIPHY Capture | Mac | gif | ⭐⭐⭐⭐ |
| ScreenToGif | Win | gif，编辑功能强 | ⭐⭐⭐⭐ |
| OBS Studio | 跨平台 | mp4，要后期转 gif | ⭐⭐⭐ |
| Kap | Mac | mp4/gif，简洁 | ⭐⭐⭐⭐ |

## 期望演示流程（demo.gif）

参考脚本（约 30-45 秒）：

1. (0-3s)  打开浏览器，看到三栏 UI + 大字 `muse·lab` 品牌空状态
2. (3-8s)  左侧文件树点开 `archives/health/`，点一个 md 文件 → 中栏渲染
3. (8-12s) 再点 2-3 个文件 → 顶部 tab 累积，点 tab 切换
4. (12-18s) 右栏 chat 输入 `@README.md 帮我总结这个项目`，看 SDK 调 Read 工具
5. (18-24s) 切模型下拉到 DeepSeek V4，再问一句 → 看不同模型 tag
6. (24-30s) 右上角齿轮 → Settings modal 展开 → 关掉

## 期望的截图（screenshot.png）

主截图：三栏 + 一个 md 文件渲染好 + chat 区有几条对话 + 顶部 tabs 有 3 个 +
深色主题。1280x800 或更高分辨率。

## 文件命名约定

- `demo.gif` — README 顶部主 gif（< 5MB）
- `screenshot.png` — 主截图（保存原图 + 1280x800 缩略图）
- `settings.png` — Settings modal 截图
- `chat.png` — chat 工具调用展示截图
