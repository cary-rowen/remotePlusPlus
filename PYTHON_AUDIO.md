# Python 音频实现与验证记录

## 当前实现：Opus

兼容基线为 NVDA 2026.1 / Python 3.13 / x64。音频继续在 NVDA 工作线程中运行，
使用 NVDA 自带 comtypes/pycaw 采集、WavePlayer 播放；新增随插件分发的 libopus 1.5.2 DLL。
没有外部音频进程，但当前插件包不再是完全不含二进制的源码包。

* `audio.py`：四项设置、旧偏好迁移、版本 2 控制信封和工作线程生命周期。
* `audioCodec.py`：标准库 ctypes 调用 libopus，每个工作线程独占会话编解码状态。
  固定 48 kHz PCM16，64/96/192 kbps CBR，单/双声道，10/20 ms 分包，
  使用 restricted-lowdelay，关闭 DTX/FEC。
* `audioRuntime.py`：先混音再编码、接收后解码，继续以 5 ms 块播放；保留 40 ms 采集上限、
  所选缓冲加 40 ms 播放队列、20 ms 设备待播上限。短缺口最多补偿 40 ms，长缺口重置解码器。
  无效包和丢包补偿不刷新有效媒体时间，500 ms 中断恢复 Remote 朗读。
* `audioTransport.py`：复用服务端版本 1 TCP/UDP 外壳及不透明载荷；协商 UDP 容量使用编码包大小，
  不使用解码后的 PCM 帧长度。保留会话校验、心跳、IPv4/IPv6 和序号回绕。
* `audioCapture.py` / `audioCom.py`：继续使用既有 WASAPI 采集、系统混音转换和 COM 资源管理。

默认 96 kbps、立体声、10 ms 分包、最小缓冲。播放缓冲只影响控制端，其他设置由控制端请求，
经被控端确认后使用。双方必须支持版本 2，不再回退到 PCM。取消不依赖编码参数，协商失败时
释放远端采集。格式切换使用新会话，保留代次保护和主线程回调注册。
静音仍解码以维护状态，但不排队播放；保留前导静音，远程音频不跟随本机提示音音量。

## 构建和验证命令

需要 Visual Studio 2022 C++ 工具及 Windows SDK。构建脚本通过 uv 获取 CMake 3.31.6，
校验固定上游源码的 SHA-256，构建 Release x64 DLL 并静态链接 C 运行库。
许可证、来源和构建选项见 `addon/globalPlugins/remotePlusPlus/lib/`。DLL 不提交到 Git。

```powershell
uv run python tools/build_opus.py
uv run python -m unittest discover -s tests
uv run python -m SCons
uv run python -m SCons pot
D:\git\nvda\.venv\Scripts\python.exe -m unittest discover -s tests
D:\git\nvda\.venv\Scripts\python.exe tools/audio_device_probe.py
D:\git\nvda\.venv\Scripts\python.exe tools/audio_probe.py --host 127.0.0.1 --port 16388 --seconds 10 --bitrate 96 --channels 2 --frame-ms 10
```

网络探针连接本机测试服务器，输出编码及含 IP/UDP/音频包头的流量、包间隔、丢包和 CPU 数据，
不保存原始音频。它不测量远程按键到扬声器的总延迟，不能替代双机收听。

## Opus 验证记录（2026-09-21）

* NVDA 开发环境的 76 项测试通过，包含 12 种真实编解码组合、设置面板、取消竞态、
  无效协商释放采集、格式校验、丢包补偿、静音期间解码和资源释放。
* 真实设备探针使用 Windows 10 和已安装 NVDA 2026.3beta2 的 x64 音频 DLL。
  单/双声道及 10/20 ms 四种组合的采集、播放、静音和停止通过；本机提示音音量为零、
  开启静音裁剪时仍验证了独立音量和保留前导静音。
* 原版音频服务端在本机运行，系统声音和麦克风混音，96 kbps 立体声各测试 8 秒。
  10 ms：801 包，编码流量 96.11 kbps，含协议头 148.97 kbps；
  20 ms：400 包，编码流量 95.99 kbps，含协议头 122.38 kbps；两组均无丢包，
  发布进程 CPU 均约为单核的 1.17%。数据不包括链路层及控制连接开销。
* 与 CI 相同的全文件 pre-commit 检查通过，按现有 CI 配置跳过分支保护和 Pyright；
  Ruff、编译、gettext、简体中文 msgfmt 及插件包内容检查通过。
* Pyright 单独执行。使用同一 NVDA 环境和规则对照当前提交：基线 446 条诊断，
  修改后 448 条；新增两条均来自新增控件调用处的 NVDA `addLabeledControl` 参数类型不完整。
  `audioCodec.py` 没有类型诊断。Pyright 不计为通过。
* 未进行双机实际收听、公网弱网验证或插件安装；版本号未改变。

## 并发修复与完整协商模拟（2026-09-21）

* 播放队列的条件变量与播放器状态共用同一把可重入锁，消除两把锁的顺序依赖。
  当前源码中的 Opus 解码原本就在条件变量锁外，未复现审查描述的原始死锁；继续保持
  锁外解码，并新增跨线程测试：大段丢包后的解码阻塞时，静音、取消静音和停止均可完成，
  解码完成后不重新加入已取消的音频。
* `tools/audio_session_probe.py` 使用 NVDA 源码的真实 RelayTransport、JSONSerializer、
  extensionPoints，以及两端真实 RemoteService、AudioService、AudioRuntime 和 Opus DLL。
  只替换 UI 环境、合成器状态、采集输入和播放设备；输入为合成音，输出验证非零 PCM，
  不采集或保存用户声音。模拟在同一台电脑的两个客户端间进行，不等同于双机实际收听。
* 经公网测试服务器的 6837 控制通道和 6838 TCP/UDP 音频通道，64/96/192 kbps、
  单/双声道、10/20 ms 共 12 种组合完成协商、解码播放、静音恢复和远程关闭。
  另测双源混音、150 ms 丢包后恢复、超过 500 ms 媒体中断后恢复，以及工作线程完全退出。
  使用随机独立频道，TLS 证书校验开启。所有参数组合均通过。
* 在用户提供的现有频道中，服务器正常转发音频控制请求；在线被控端没有返回音频响应，
  其连接提示音路径指向 TeleNVDA。Remote++ 当前只接入 NVDA 内置远程访问，安装相同版本
  不会使 TeleNVDA 连接接入音频控制。已补充超时提示和使用说明；现有被控端切换连接方式后
  的实际收听尚未验证。
* 77 项音频相关测试通过；真实设备探针在 NVDA 2026.3beta2 的四种声道/帧长组合下通过。

重现完整协商模拟（需要 NVDA 开发 Python 和 NVDA 源码）：

```powershell
D:\git\nvda\.venv\Scripts\python.exe tools/audio_session_probe.py --host <测试服务器> --nvda-source D:/git/nvda/source
```

## PCM 实现历史记录

以下为切换 Opus 前的验证及性能记录。其中“无 DLL”、四种 PCM 音质和 Rust 比较命令仅描述
旧实现，不代表当前版本，也不作为 Opus 的验收结果。

## 检查

* 使用标准 Python 运行单元测试时，真实 wx 面板及 comtypes/pycaw 测试按环境条件跳过。
  使用 NVDA 开发环境运行时，64 项测试全部通过（含真实 wx 控件）。
* Ruff 检查、格式检查、compileall、`git diff --check`。
* 资源释放修复后 `uv run python -m SCons` 构建成功，检查包内五个音频源文件与工作区完全一致，
  包内没有 EXE/DLL/PYD。`uv run python -m SCons pot` 提取成功。
  本次没有增加翻译字符串，因此没有批量合并或重新翻译 PO 文件。
* `tools/audio_device_probe.py` 使用真实 NVDA WavePlayer 和 Windows 音频设备，
  仅替换外围 UI/config 环境。系统/麦克风四种格式均读到 PCM，播放/静音/停止成功。
* 原版服务器在本机运行，完成三轮 TCP/UDP → 真实 NVDA 播放 → 静音/恢复 →
  发布方离开 → 500 ms 媒体回退 → 停止的验证。没有遗留工作线程。
* 严格 Pyright 已运行，尚有 11 项诊断：COM 动态声明/元数据、未完整标注的
  comtypes 接口，以及当前仓库的默认参数、重复运行时校验和 object.__init__ 规则。
  不将 Pyright 计为通过项，也没有关闭项目的严格规则。

本机直接调用 scons/pyright 启动器会报 `Failed to canonicalize script path`，
使用 `uv run python -m SCons` / `uv run python -m pyright` 可执行。
`[tool.uv] package = false` 声明这是由 SCons 打包的插件工程，避免复制仓库后
uv 将其误当作需要自动发现 Python 包的 wheel 工程。没有改依赖版本。

## 资源释放修复

* 设备 ID 查询保留 `IMMDevice::GetId` 的原始 LPWSTR 指针，复制字符串后在 finally 中
  调用 CoTaskMemFree。此处使用 Windows 标准 COM vtable 的第 5 槽，避免 pycaw 自动转成
  Python 字符串后丢失原始分配地址。首次查询和每秒轮询共用该封装。
* WavePlayer.close 抛错时也执行 COM 反初始化。公共 COM 上下文先清除异常链中已退出
  栈帧的局部引用，保证原生播放器销毁发生在所属线程的 COM 反初始化之前；保留回溯位置，
  跨线程保存格式化后的诊断文本，不保存持有设备对象的原异常。
* 音频服务保存当前及尚未退出的旧代次线程，卸载时禁止新启动，并在锁外等待清理完成。
  普通停止仍异步执行。回归测试经过完整 RemoteService.terminate 调用链，覆盖延迟设备释放、
  启动回调中卸载及线程启动失败。
* DNS 解析与 TCP 连接共用五秒启动期限，等待解析结果时每 50 ms 检查取消。
  同一模块最多运行一个独立 DNS 守护线程；系统 getaddrinfo 本身不能被 Python 强制取消，
  但它不持有音频运行时、设备或连接密钥，迟到结果被丢弃，音频线程退出不等待它。
  系统解析结束前的新查询仍受取消与期限限制，不会不断创建解析线程。

修复后，系统输出和麦克风各进行三轮、每轮 5 万次设备 ID 查询，返回值均一致，
各轮进程私有内存增量均为 0；修复前同类查询每轮增长约 6.6 MB。
四种格式的本机真实采集、NVDA WavePlayer 播放、静音和停止复测通过。
真实 NVDA WavePlayer 的原生停止异常注入也通过：正常和异常路径在 COM 退出时的
存活播放器数均为 0。DNS 阻塞期间卸载、超时、迟到结果和失败恢复均有回归测试，
localhost、IPv4 和 IPv6 的真实解析结果与标准库一致。
Ruff、格式和编译检查通过；COM 初始化集中后，Pyright 原有诊断由 12 项减少至 11 项。
这次修复重新构建了插件包；没有安装，也没有重跑公网或双机测试。

## 性能实测

原版服务端来自 https://github.com/haitun001/NVDARemoteAudioServer 。
双源同时采集，每种实现/音质测试 15 秒；CPU 是发布进程占单个逻辑核的比例。
下表的间隔是本机接收音频包的间隔，不是远程键盘到扬声器延迟。

| 音质 | Rust CPU | Python CPU | Rust 间隔 P95 | Python 间隔 P95 | 丢包 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 48 kHz 立体声 | 1.77% | 1.67% | 10.32 ms | 5.88 ms | 双方 0 |
| 48 kHz 单声道 | 3.02% | 1.87% | 10.17 ms | 5.95 ms | 双方 0 |
| 24 kHz 单声道 | 1.87% | 0.94% | 10.63 ms | 5.88 ms | 双方 0 |
| 16 kHz 单声道 | 1.25% | 2.19% | 10.60 ms | 5.84 ms | 双方 0 |

16 kHz 另做 25 秒复测：双方 CPU 均为 1.62%，双方零丢包，Rust/Python 间隔
P95 分别为 10.56/5.94 ms。数据存在调度波动，不能据此承诺所有设备和音质下 CPU
绝不增加。Windows 音频引擎进程的 CPU 不包含在上表内，转换由 Rust 移至 Windows
也会转移部分计算成本。没有测量完整 NVDA 的 GIL 竞争、长期运行或双机端到端延迟。

公网 www.zxrjy.net:6838 上已验证真实采集音频转发。公网测试双方均出现丢包、较长
到包间隔，部分握手超时，因此不用于判断实现快慢。提供的 6388 端口实测连接超时，
当前保留原插件和服务端源码默认的 6838。测试始终使用随机隔离房间，不加入现有
远程控制房间，不发送键盘操作，不保存捕获音频或连接密码。

## 重现

```powershell
uv run ruff check addon/globalPlugins/remotePlusPlus/audio*.py tests tools sconstruct
uv run python -m unittest discover -s tests
D:\git\nvda\.venv\Scripts\python.exe -m unittest discover -s tests
D:\git\nvda\.venv\Scripts\python.exe tools/audio_device_probe.py
uv run python -m SCons
```

对照脚本需使用已有 comtypes/pycaw 的 NVDA 开发 Python；提供音频服务器地址，
可选 `--rust` 指向旧版 EXE，脚本自动生成隔离房间：

```powershell
D:\git\nvda\.venv\Scripts\python.exe tools/audio_probe.py --host 127.0.0.1 --port 16388 --seconds 15 --rust .validation/rust-baseline/audioClient/target/x86_64-pc-windows-msvc/release/remotePlusPlusAudio.exe
```

`.validation/` 仅存放本地对照代码、旧草稿和原始验证输出，被 Git 和插件构建忽略。
没有修改或安装当前正在使用的插件。正式替换前仍需两台运行 NVDA 的机器实际试听，
并验证目标设备的拔插、默认设备切换和长时间运行。
