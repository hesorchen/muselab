# archives/ — 原件归档

## 该放什么

不需要日常更新但要长期留底的**原始**文件：

- 学位证 / 学位认证 / 成绩单
- 出生证 / 身份证扫描件（**加密后**）
- 历史体检报告 PDF（已经在 `health/` 引用过的原件）
- 历史简历 / Offer letter
- 重要合同 / 协议
- 老的工作证明 / 离职证明
- 家族档案 / 老照片元数据

## 这里和其他目录的关系

`archives/` 是**原件柜**——只读、不常翻。
其他目录（`health/` / `career/` / ...）是**工作台**——常更新、Muse 常读。

工作台引用原件时用 markdown 链接：

```markdown
具体数值见 [2024-09 体检原件](archives/2024-09-checkup-xiehe.pdf)
```

## 重要提示

- 这个目录里很可能包含**身份证号 / 学号 / 合同金额**等高敏信息
- 强烈建议给整个 muselab archive 启用文件系统加密（macOS FileVault / Linux LUKS / Windows BitLocker）
- 绝对不要 sync 到公共云（OneDrive / Google Drive 等）
- 如果非要远程备份：用 [restic](https://restic.net) 或 [borg](https://borgbackup.org) + 端到端加密
