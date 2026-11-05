# Vivado SSH Build

从 Windows 一条命令提交 Linux Vivado 构建：增量上传源码、后台运行综合与实现、实时显示阶段，完成后自动下载 bitstream、调试探针和报告。

```powershell
.\remote-build.ps1 submit_start
```

```text
vivado-build · xcku040-ffva1156-2-i

✓ Synthesis                          02:35
✓ Implementation: opt_design         00:18
⠋ Implementation: place_design       00:22
○ Implementation: route_design     PENDING
○ Generate Bitstream               PENDING
```

上图仅为显示效果示例。实际阶段读取 Vivado 的运行标记与构建脚本事件；没有可靠百分比时不显示百分比。

## 功能

- 按路径、大小和修改时间比较输入，只上传新增或变化文件。
- 每次构建在服务器创建独立源码快照，后续上传不会修改正在编译的输入。
- 通过 SSH 启动独立后台任务，断开本地终端后任务仍可运行。
- Rich 终端界面显示旋转动画、实际实现阶段及耗时。
- 支持现有 SSH 密钥/agent，或命令文件中配置的密码。
- 成功后下载同次构建的 `.bit`、`.ltx` 和报告；失败也下载诊断日志。
- 结果文件夹使用香港时间（UTC+8），格式为 `YYYY-MM-DD_HH-MM-SS_microseconds`。
- 不调用 RTL 仿真，不下载或编程 FPGA，不计算源码/配置内容摘要。

## 安装

本地建议使用 Python 3.11 或更新版本、PowerShell 和 Windows OpenSSH。Linux 服务器需要 Python 3.8+、bash、Vivado、目标器件支持及适用许可证。构建脚本面向 Vivado 2023.2。

```powershell
git clone https://github.com/Harrison311/vivado-ssh-build.git
cd vivado-ssh-build
python -m pip install -r requirements.txt
```

依赖也可安装到本工具专用的 `.deps` 目录，程序会优先从这里加载：

```powershell
python -m pip install --target .deps -r requirements.txt
```

更换 Python 版本时，请重新安装匹配的二进制依赖，不要复用其他解释器的 `.deps`。

## 每个工程一个命令文件

将 [project-command.ps1](templates/project-command.ps1) 复制到自己的工程，改名为 `remote-build.ps1`，编辑 `$ToolRoot`、`$Python` 和 `$Config`。

| 配置 | 用途 |
| --- | --- |
| `SshTarget` / `SshPort` | SSH 别名或 `user@host`，以及端口 |
| `Password` | 可选密码；留空使用现有 OpenSSH 认证 |
| `IdentityFile` | 空密码模式下可选的本地私钥路径 |
| `RemoteProjectRoot` | Linux 上专用于本工程的新目录或空目录 |
| `LocalProjectRoot` | 本地源码根目录 |
| `LocalResultRoot` | 拉取结果的本地根目录 |
| `VivadoSettings` | 服务器的 `settings64.sh` 路径 |
| `Part` / `TopModule` | FPGA 器件型号与顶层模块 |
| `SourceFiles` | 全部 HDL 编译输入，相对源码根目录 |
| `ConstraintFiles` | 项目 XDC 文件，相对源码根目录 |
| `IpSetupTcl` | 在新工程中创建、配置 IP 的 Tcl 脚本 |
| `IncludeDirs` | 头文件搜索目录；目录内容也会上传 |
| `ExtraUploadPaths` | ROM、COE、Tcl 辅助文件等额外输入 |
| `Jobs` | Vivado 并行 run 数，不是总 CPU 线程上限 |
| `PollSeconds` | 实时监控的查询间隔，默认 3 秒 |

所有工程输入须位于 `LocalProjectRoot` 内，不允许 `..` 或目录外引用。共用源码时，请选择共同上级目录为根目录。上传某个目录不会自动把其中全部文件加入 Vivado 编译，仍需准确填写 `SourceFiles`。

本工具不直接打开 Windows XPR。服务器依据清单重建工程，避免 Windows 盘符和本地缓存路径问题。IP 配置脚本应能在 Linux 新工程中执行，不依赖固定 Windows 路径。

## 使用

```powershell
# 默认动作是 check，不会启动编译。
.\remote-build.ps1 check

# 推荐：上传、后台构建、实时显示、自动下载。
.\remote-build.ps1 submit_start

# 也可分步操作。
.\remote-build.ps1 submit
.\remote-build.ps1 status
.\remote-build.ps1 fetch

# 操作指定历史任务。
.\remote-build.ps1 status -JobId '2026-11-05_16-30-25_123456'
.\remote-build.ps1 fetch -JobId '2026-11-05_16-30-25_123456'

# 大小和修改时间都没有变化、但内容可能被修改时，强制重新上传。
.\remote-build.ps1 submit_start -ForceUpload
```

`check` 只读检查本地输入、远程 Python、Vivado 环境脚本及版本，不验证实际许可证 checkout。`submit_start` 在失败时也自动拉取诊断结果，并返回非零退出状态。

Ctrl+C 停止本地监控，不取消服务器任务。之后可使用 `status`、`fetch` 继续操作。连续三次状态查询失败会退出监控，服务器任务可能仍在运行。

## SSH 与密码

第一次连接应先用普通 SSH 核验服务器主机密钥：

```powershell
ssh user@server
```

密码模式使用 Paramiko 读取本机 `~/.ssh/known_hosts`，拒绝未知或变化的主机密钥。支持直接连接和 SSH 配置中的 `ProxyCommand`，暂不支持 `ProxyJump`。

如选择在命令文件中保存密码：

```powershell
Password = 'your-password'
```

这是本地明文保存。密码中的单引号应写成两个单引号，例如 `ab'cd` 写作 `'ab''cd'`。不要将填过密码的文件提交到 Git；本仓库只提供空白模板。

程序将当前命令文件排除在上传清单之外，并仅将白名单内的构建参数传给服务器。密码不会写入服务器任务配置、进程命令行参数或日志。密码模式在一次命令内复用 SSH 连接；空密码模式建议使用密钥/agent，避免轮询时反复输入密码。

## 同步与目录结构

增量比较依赖相对路径、字节数和纳秒修改时间，不计算源码 SHA、MD5 或 CRC。无法识别“内容变化但大小和修改时间都保持不变”的情况，此时使用 `-ForceUpload`。SSH 协议自身的加密完整性检查不属于源码摘要计算。

```text
RemoteProjectRoot/
  .vivado-ssh-owner
  .tool/remote_agent.py
  source/                    # 持续更新的输入副本
  manifest.json              # 文件元数据
  jobs/
    YYYY-MM-DD_HH-MM-SS_microseconds/
      src/                   # 本次固定输入
      work/                  # Vivado 工作目录
      results/               # 可下载结果
      status.json
```

同步先暂存变化文件，完整收齐后才提交。正常删除仅针对此前同步清单管理的文件。服务器用锁隔离同步和快照；并发输入变化会触发重试提示。异常中断可能留下未跟踪旧文件，但快照仅复制当前清单所列文件。

本地结果放在 `LocalResultRoot/任务编号/`。重复拉取同一任务不会另建日期目录。下载使用 `.partial` 临时文件，收齐后再替换；不支持断点续传。正在运行的任务拒绝下载，以免拉取仍在变化的产物。

## 构建检查与结果

流程为创建工程、执行 IP 配置脚本、添加 HDL/XDC、综合、实现到 `route_design`，然后检查时序及 DRC，再生成 bitstream。有调试核时生成匹配的 `.ltx`。

检查包括 setup/hold slack、时序报告的全部约束满足声明、无时钟和内部未约束端点，以及 DRC Error/Critical Warning。检查失败或报告格式无法识别时标记失败，不继续生成 bitstream。只有 Vivado 返回 0 且 `build_status.txt` 为 `PASS` 时任务才显示 `PASSED`。

结果包括可能生成的 `design.bit`、`design.ltx`、时序/利用率/DRC 报告、控制台日志、Vivado 日志、各 run 的 `runme.log` 及 `build_info.json`。失败任务不保证存在 bit/ltx。

当前面向普通 RTL 与 standalone IP 工程。BD、额外 HDL 库及自定义 run hook 等需求需要扩展构建脚本。`IpSetupTcl` 会作为用户代码执行，使用前应审阅。构建通过不等于功能或板上验证通过。

本版不提供取消、调度队列或历史任务自动清理；多任务会并行占用资源，磁盘空间需自行管理。

## 测试与已知边界

```powershell
python -B tests/test_transport.py
```

本地 15 项工具测试覆盖增量判断、上传中断、受管理文件删除、路径保护、快照隔离、凭据排除、Tcl 兼容编码、阶段标记解析及自动下载等逻辑。测试不启动 SSH 远程构建、Vivado 或 RTL 仿真；Windows 下替代了 Linux `fcntl` 锁接口，因此未验证真实并发锁行为。

测试需要 tkinter 的 Tcl 解释器；正常使用工具不需要 tkinter。测试样本生成于被 Git 忽略的 `tests/.runs/`。

已根据 Vivado 2023.2 的实际报错，将参数恢复方式从不受支持的 `binary decode base64` 改为旧版兼容的 `binary format H*`。密码登录、Rich 界面和完整远程构建仍需在目标环境做端到端验证；这里不宣称已有完整板级验证结果。
