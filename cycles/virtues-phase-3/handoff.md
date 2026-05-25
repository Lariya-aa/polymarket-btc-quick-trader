# cycles/virtues-phase-3/handoff.md

> Commit `1c891f9`. Phase 3 = 修复 `acquire_single_instance_lock` 的 truncate-before-flock race + 顺手加固 OSError catch（Codex 同 cycle warn 的 inline patch）。

## 干了啥

`acquire_single_instance_lock` 之前 `open(LOCK_FILE, "w")` 会**立刻 truncate** 文件，**然后**才 `fcntl.flock()` 试拿锁。如果另一个实例正持有锁：
1. 新实例 open("w") → 旧实例的 PID 文件被清空
2. 新实例 flock → BlockingIOError
3. 新实例 print error 退出
4. 文件留空，`cat /tmp/poly_mm_pro_max.lock` 看不到 holder

修法（顺序倒过来）：
- `open(LOCK_FILE, "a+")` — 不 truncate
- `fcntl.flock(LOCK_EX | LOCK_NB)`
- 失败：close fd + return None  
- 成功：`seek(0) + truncate(0) + write PID + flush`

顺便：Codex 在 review 里 flag 了一个 plan-level warn —— except 只接 `BlockingIOError`，其他 `OSError` 子类（如 `PermissionError`、`OSError(ENOLCK)`）不会被接住，fd 泄漏 + 异常向上跑。Inline patch：`except OSError`（BlockingIOError 是其子类，向上兼容）+ 加 `test_acquire_lock_handles_non_blocking_oserror` 测试。

## 你看到的差异

正常 path 行为不变。

唯一可观察差异是**双开场景**：
- 之前：第二实例退出后 `cat /tmp/poly_mm_pro_max.lock` 是空文件
- 现在：仍能看到第一实例的 PID

## 你要做的 live verification

```bash
./PolyMarketMaker.command &       # 启动 A
./PolyMarketMaker.command         # 启动 B
# 期望：B 立即退出，stderr 显示 "PolyQuickTrader is already running."
cat /tmp/poly_mm_pro_max.lock     # 应显示 A 的 PID（不是空）
kill <A's PID>
./PolyMarketMaker.command         # 应正常启动
```

## Codex BLOCK 又一次是 false positive

跟 Phase 1 redo 一样：`tests/test_lock.py` 是新建 untracked file，`git diff --name-only` 看不到。Kimi 不允许 `git add`。Codex 算法漏看 `git status`。

我 override BLOCK + inline 加 OSError patch + commit。Lessons 已记入 `cycles/_lessons.md` Lesson 5。

## 不在本 cycle 范围

- `mainloop()` 没 try-finally 包装确保 lock 退出时被释放（next phase 可加）
- LOCK_FILE 路径常量（`/tmp/poly_mm_pro_max.lock`）跨平台问题（macOS 重启清空 /tmp，跨用户冲突）— 不修
