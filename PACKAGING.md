# PACKAGING.md

> 把 `poly_mm_pro_max.py` 打成 macOS `.dmg` 和 Windows `.exe` 的全流程。

---

## 谁应该读这份

- **想自己在本机出包**：A 节本机构建。
- **没 Windows 机器，但想出 .exe**：B 节 GitHub Actions。
- **想给最终用户分发**：C 节 签名/公证/Gatekeeper/SmartScreen。

---

## 0. 出包前必须先做的两件事（**本项目特有**）

### 0.1 跑测试，确认本机改动没坏

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest tests/ -v
```

期望：**全部 PASS**（67+ 测试）。出包前不绿不打。

### 0.2 本机用 source 模式启动一次

```bash
.venv/bin/python poly_mm_pro_max.py
```

GUI 弹出 + 能点"扫描短周期"按钮 + 日志面板有输出 = source 模式 OK。
**这一步不通过，PyInstaller 出来的包必然也不通过**——先排查源码再打包。

---

## A. 本机构建

### A.1 macOS（出 `.app` + `.dmg`）

要求：macOS 11+，Python 3.10+，repo 根目录有 `.venv`。

```bash
./packaging/build_macos.sh
# → dist/PolyMarketTrader.app
# → dist/PolyMarketTrader.dmg
```

脚本做了什么：
1. 预检：`.venv` 是否存在、`dist/` 是否非空（非空且未 FORCE=1 就拒绝）
2. `rm -rf build dist`
3. `pyinstaller packaging/poly_mm.spec --clean --noconfirm`
4. 可选 codesign（若 `CODESIGN_IDENTITY` 环境变量已设）
5. `hdiutil create ... PolyMarketTrader.dmg`

**首次运行预计耗时**：2–4 分钟（PyInstaller 装 hooks + 解析依赖）。

签名（可选）：

```bash
CODESIGN_IDENTITY="Developer ID Application: 你的名字 (TEAMID)" \
  ./packaging/build_macos.sh
```

没签名的 `.app` 第一次打开会被 Gatekeeper 拦，详见 C 节。

### A.2 Windows（出 `.exe`）

要求：Windows 10/11，Python 3.10+ on PATH，repo 根目录有 `.venv`。

PowerShell：

```powershell
.\packaging\build_windows.ps1
# → dist\PolyMarketTrader\PolyMarketTrader.exe
# → dist\PolyMarketTrader\        (整个 onedir 目录，里面有 DLL/lib 等)
```

要做单文件安装器（`.msi` / 单一 `setup.exe`）：用 Inno Setup 或 NSIS 把 `dist\PolyMarketTrader\` 整个目录打包。最简 Inno Setup 样板见本文末尾的"附录：Inno Setup .iss 模板"。

---

## B. GitHub Actions（不用本机有 Windows）

仓库已包含 `.github/workflows/build.yml`。触发方式：

```bash
# 方式 1：推 tag 触发 → 自动 build + attach release assets
git tag v0.1.0
git push origin v0.1.0

# 方式 2：从 GitHub Actions 页面手动 "Run workflow"
```

workflow 做了什么：
1. 起两个并行 job：`build-macos`（macos-latest）+ `build-windows`（windows-latest）
2. 每个 job 装 Python 3.11 → `pip install -r requirements-dev.txt pyinstaller` → `pytest tests/` → 跑对应平台的 build 脚本
3. 上传两个 artifact：`PolyMarketTrader-macos.dmg` + `PolyMarketTrader-windows.zip`
4. **tag 触发时**额外把这两个文件 attach 到对应的 Release（用 softprops/action-gh-release）

> macOS runner 没有 Developer ID 证书，所以 CI 出的 `.dmg` 是**未签名**的。
> 公开分发请走 C 节自己签名。

---

## C. 给最终用户分发（签名 / Gatekeeper / SmartScreen）

无签名的二进制在两个平台首次启动都会被拦。这是给个人用 OK，公开分发会让大量用户安装率清零。

### C.1 macOS：Developer ID + Notarization

成本：Apple Developer 账号 **$99/年**。

获得签名身份后：

```bash
# 1. 用 Developer ID Application 证书签名（build_macos.sh 内置了）
CODESIGN_IDENTITY="Developer ID Application: 你的名字 (TEAMID)" \
  ./packaging/build_macos.sh

# 2. 提交公证（apple notary service）
xcrun notarytool submit dist/PolyMarketTrader.dmg \
  --apple-id "your@apple.id" \
  --team-id "TEAMID" \
  --password "@keychain:AC_PASSWORD" \
  --wait

# 3. 把公证票钉到 .dmg
xcrun stapler staple dist/PolyMarketTrader.dmg
```

**用户侧体验**：双击 `.dmg`，拖 `.app` 到 Applications，正常打开，无任何警告。

**临时绕过（自用 / 测试）**：

```bash
# 第一次打开被拦后，右键 > 打开 一次即可
# 或：
xattr -dr com.apple.quarantine /Applications/PolyMarketTrader.app
```

### C.2 Windows：Code Signing Certificate

成本：EV Code Signing Cert **$300–700/年**（DigiCert / SSL.com / Sectigo）。

获得证书后，在 build 脚本最后加（PowerShell）：

```powershell
& signtool.exe sign /tr http://timestamp.digicert.com /td sha256 /fd sha256 `
  /a "dist\PolyMarketTrader\PolyMarketTrader.exe"
```

**SmartScreen reputation**：即便签了名，新签名也需要"刷声誉"（被一定数量用户安装后才不再警告）。EV 证书可直接获得高 reputation。

**用户侧绕过（无签名时）**：点 "More info" → "Run anyway"。

---

## 附录 1：常见问题

| 症状 | 原因 | 修法 |
|---|---|---|
| PyInstaller 完成但 `.app` 启动后立刻闪退 | hiddenimports 漏 | 在 `packaging/poly_mm.spec` 的 `hidden` 列表里加缺失的 module 名（用 `dist/PolyMarketTrader/PolyMarketTrader` 终端直接跑看 traceback） |
| Windows 启动报 "Failed to execute script" 闪退 | console=False 把 traceback 藏了 | 临时把 spec 里 `console=False` 改成 `console=True` 重打，看真正错误 |
| `.app` 启动后 GUI 不出现，但 Activity Monitor 有进程 | tkinter 的 root.mainloop() 在某些线程上下文里没 attach | 检查 `__main__` 块 vs PyInstaller 的 entry-point；通常不会发生 |
| Gatekeeper：「无法验证开发者」 | 未签名 | 见 C.1 |
| SmartScreen：「未识别应用」 | 未签名/无 reputation | 见 C.2 |
| `dist/` 体积大（200+ MB） | py_clob_client_v2 拉了 web3/eth-account，正常 | 接受，或在 spec 的 `excludes` 里裁 |
| 同一台机器跑两次 GUI 第二次报"already running" | 这是预期行为（单实例锁） | 真要强开就 `pkill -f PolyMarketTrader` / 任务管理器结束 |

## 附录 2：本项目跨平台改动

为了能在 Windows 上跑，源码做了这些修改（详见 commit `optimization/packaging`）：

- 移除顶层 `import fcntl`（Unix-only），改为 `acquire_single_instance_lock` 内按平台 lazy-import (`msvcrt` on Windows, `fcntl` on Unix)
- 配置/日志/锁文件路径从硬编码（`/tmp/poly_mm_pro_max.lock`、CWD-relative `poly_config_pro.json`）改为 `user_data_dir()` + `tempfile.gettempdir()`：

  | 平台 | 配置/日志路径 |
  |---|---|
  | macOS | `~/Library/Application Support/PolyMarketTrader/` |
  | Windows | `%APPDATA%\PolyMarketTrader\` |
  | Linux | `$XDG_DATA_HOME/PolyMarketTrader/` |

- `load_config_from_local` 增加一次性迁移：旧版 CWD 里的 `poly_config_pro.json` 会被自动移到新位置
- `os.chmod(path, 0o600)` 包了 try/except（Windows 上 chmod 是 no-op，不会真的限制权限）

## 附录 3：Inno Setup .iss 模板（Windows 单文件安装器）

如果你想给 Windows 用户一个 `setup.exe` 而不是 zip：

1. 下载 Inno Setup 6 (https://jrsoftware.org/isinfo.php)
2. 在 `packaging/installer.iss` 放：

```ini
[Setup]
AppName=PolyMarketTrader
AppVersion=0.1.0
DefaultDirName={autopf}\PolyMarketTrader
DefaultGroupName=PolyMarketTrader
OutputBaseFilename=PolyMarketTrader-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern

[Files]
Source: "..\dist\PolyMarketTrader\*"; DestDir: "{app}"; Flags: recursesubdirs

[Icons]
Name: "{group}\PolyMarketTrader"; Filename: "{app}\PolyMarketTrader.exe"
Name: "{commondesktop}\PolyMarketTrader"; Filename: "{app}\PolyMarketTrader.exe"
```

3. 跑：`iscc packaging\installer.iss` → 出 `packaging\Output\PolyMarketTrader-Setup.exe`

---

*最后更新：2026-05-22*
