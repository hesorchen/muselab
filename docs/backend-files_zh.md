# 文件 API

> [English](backend-files.md)

文件 API 是浏览器文件管理与预览的受限接口。它不是 muselab 唯一的文件系统入口：Agent 工具和真实终端拥有各自的权限边界，详见[后端安全模型](backend-security_zh.md)。

## 工作目录选择

每个请求都绑定一个已登记工作目录：

- 默认使用 `MUSELAB_ROOT`；
- 可用 `workspace=<绝对路径>` 查询参数选择；
- 普通 fetch 也可使用 URL 编码后的 `X-Muselab-Workspace` header。

未登记、已移除或不允许的目录会被拒绝。返回的文件路径始终相对于当前工作目录。

## 端点

当前共 19 个文件端点，全部需要 token。

### 读取与预览

| 方法与路径 | 用途 |
|---|---|
| `GET /api/files/list` | 列目录，最多 500 项 |
| `GET /api/files/stat` | 获取单个路径的类型、大小和修改时间 |
| `GET /api/files/read` | 读取文本，最大 2 MiB |
| `GET /api/files/raw` | 内联预览受支持的图片、媒体、PDF、HTML 与 SVG，其余强制下载 |
| `GET /api/files/download` | 作为附件下载 |
| `GET /api/files/xlsx` | 工作簿结构化预览 |
| `GET /api/files/csv` | CSV/TSV 分页预览 |
| `GET /api/files/grep` | 有界全文搜索 |
| `GET /api/files/search` | 文件名和目录名搜索 |

`raw` 与 `download` 用于 iframe、图片或浏览器导航，因此接受查询参数 token。HTML/SVG 预览使用 sandbox CSP；HTML 预览桥仅对不超过 12 MiB 的文件注入滚动和图片交互支持。

预览限制：

- XLSX：最多 20 个 sheet、每个 500 行、50 列，单元格最多 500 字符。
- CSV：默认每页 200 行、最多 1,000 行、50 列，单元格最多 500 字符；总行数按文件签名缓存。
- grep：查询至少 2 字符、单文件最多 1 MB、单次约 8 秒、最多两个并发扫描。

### 写入与组织

| 方法与路径 | 用途 |
|---|---|
| `PUT /api/files/write` | 原子覆盖或创建文本文件，最大 10 MiB |
| `POST /api/files/upload` | Multipart 上传，默认每文件最大 100 MiB |
| `POST /api/files/mkdir` | 创建目录 |
| `POST /api/files/rename` | 移动或重命名 |
| `POST /api/files/copy-bak` | 创建 `.bak`、`.bak.2` 等备份副本 |
| `DELETE /api/files/delete` | 默认软删除；`permanent=true` 永久删除 |

同名上传会先把原文件移入回收站，再原子替换，因而可以恢复。危险可执行扩展名和敏感文件名默认禁止上传。

### 回收站

| 方法与路径 | 用途 |
|---|---|
| `GET /api/files/trash/list` | 列出当前工作目录的回收站 |
| `POST /api/files/trash/restore` | 恢复一个条目 |
| `DELETE /api/files/trash/purge` | 永久删除一个条目 |
| `DELETE /api/files/trash/empty` | 清空当前工作目录的回收站 |

每个工作目录有自己的 `.muselab-dustbin/`：

```text
<workspace>/.muselab-dustbin/
├── <timestamp>_<8hex>
└── <timestamp>_<8hex>.json
```

载荷与 manifest 使用同一个不可猜测 ID。默认保留 30 天，可用 `MUSELAB_TRASH_TTL_DAYS` 调整；设为 `0` 表示不自动清理。

## 路径与敏感文件防护

所有路径都经过规范化：

1. 拒绝 NUL 字节和非法路径；
2. 将相对路径解析到选中的工作目录；
3. 跟随符号链接后再次检查真实路径仍在目录内；
4. 对文件名和解析后的目标执行敏感文件规则。

被屏蔽的典型内容包括 `.env`、私钥、证书、SSH 凭据、云凭据和常见 token 文件。写入、上传、重命名和备份不能直接修改 `.muselab-dustbin/`；回收站只能通过专用端点管理。

这些规则保护 Web 文件接口，但不能代替操作系统级隔离。
