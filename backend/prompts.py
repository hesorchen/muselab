"""Self-contained starter messages for muselab's on-demand workflows."""

CURATOR_INITIAL_MESSAGE = {
    "zh": (
        "请整理当前工作区，并严格按以下流程进行：把当前 workspace 作为不可越过的边界，"
        "不要访问父目录，也不要跟随指向边界外的符号链接；先只读扫描现有文件和目录，"
        "默认排除 .git、.venv、node_modules、缓存及其他隐藏运行时目录，除非我明确点名，"
        "并且不得创建、编辑、移动或删除任何内容；然后根据实际内容提出小批次整理方案，"
        "说明每项拟议变更、理由、影响和可恢复方式，不强加任何预设目录结构；每批文件变更"
        "都要等待我明确确认，未经确认不得执行；任何删除或覆盖必须从其他变更中单独列出，"
        "并再次取得明确确认，优先采用回收站、备份或其他可恢复方式；确认后只执行获批的变更，"
        "核对结果并汇报。全程不要收集或推断个人画像，不要创建或编辑 CLAUDE.md。"
    ),
    "en": (
        "Organize the current workspace using this exact workflow. Treat the current "
        "workspace as a hard boundary: do not inspect parent directories or follow "
        "symlinks outside it. First perform a read-only scan of existing files and "
        "directories, excluding .git, .venv, node_modules, caches, and other hidden "
        "runtime directories unless I explicitly name them, without creating, editing, "
        "moving, or deleting anything. Then propose small batches based on the actual "
        "contents, listing each change, its rationale, impact, and recovery path without "
        "imposing a preset directory structure. Wait for my explicit confirmation before "
        "every batch. List any deletion or overwrite separately from other changes and "
        "obtain a second explicit confirmation, preferring trash, backup, or another "
        "recoverable method. Execute only approved changes, verify the result, and report "
        "it. Do not collect or infer a personal profile, and do not create or edit "
        "CLAUDE.md."
    ),
}
